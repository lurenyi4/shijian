from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image
import pypdfium2 as pdfium

from demo import POEMS, artwork, artwork_note
from imaging import manuscript_rgb

ALLOWED = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".pdf"}
MAX_FILE = 200 * 1024 * 1024
PDF_LOCK = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def default_data_dir():
    return Path(
        os.environ.get("SHIJIAN_DATA_DIR")
        or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Shijian" / "library")
    )


def safe_name(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(". ")[:90] or "无题"


class Library:
    def __init__(self, root: Path, seed=True):
        self.root = Path(root).resolve()
        self.lock = threading.RLock()
        self.preview_lock = threading.Lock()
        self.active_results: set[Path] = set()
        for part in ("originals", "previews", "results", "tmp", "exports"):
            (self.root / part).mkdir(parents=True, exist_ok=True)
        self.db = self.root / "library.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT DEFAULT '', era TEXT DEFAULT '',
                    collection TEXT DEFAULT '未分诗集', filename TEXT NOT NULL, source TEXT NOT NULL,
                    preview TEXT NOT NULL, sha256 TEXT UNIQUE NOT NULL, pages INTEGER DEFAULT 1,
                    text TEXT DEFAULT '', raw_text TEXT DEFAULT '', notes TEXT DEFAULT '',
                    status TEXT DEFAULT 'new', favorite INTEGER DEFAULT 0, reviewed INTEGER DEFAULT 0,
                    demo INTEGER DEFAULT 0, trashed INTEGER DEFAULT 0, error TEXT DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
                    text TEXT NOT NULL, title TEXT NOT NULL, author TEXT NOT NULL,
                    era TEXT NOT NULL, notes TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collections (name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS drafts (
                    document_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS page_notes (
                    document_id TEXT NOT NULL, page INTEGER NOT NULL, start_line INTEGER NOT NULL,
                    reviewed INTEGER NOT NULL, text_version TEXT NOT NULL,
                    PRIMARY KEY(document_id,page)
                );
                CREATE TABLE IF NOT EXISTS text_versions (
                    document_id TEXT PRIMARY KEY, revision INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS revisions_document ON revisions(document_id, id);
                CREATE INDEX IF NOT EXISTS documents_updated ON documents(updated_at);
            """)
            # An interrupted process is never misreported as still running.
            db.execute(
                "UPDATE documents SET status='interrupted',error='上次识别被中断，可重新加入队列。' WHERE status IN ('queued','running')"
            )
        if seed and not self.setting("seeded", False):
            self.seed()
            self.set_setting("seeded", True)
        if seed and self.setting("artwork_version", 0) < 2:
            self.migrate_demo_art()
            self.set_setting("artwork_version", 2)

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.db, timeout=30)
            db.row_factory = sqlite3.Row
            db.create_function(
                "casefold", 1, lambda value: (value or "").casefold(), deterministic=True
            )
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def preferences(self, values):
        with self.lock:
            merged = {**self.setting('preferences', {}), **values}
            self.set_setting('preferences', merged)
            return merged

    def page_notes(self, doc_id):
        with self.connect() as db:
            doc = self._get(db, doc_id)
            version = self._text_version(doc)
            rows = [dict(row) for row in db.execute(
                'SELECT * FROM page_notes WHERE document_id=? ORDER BY page', (doc_id,))]
        return {'text_version': version, 'text_revision': doc['text_revision'], 'position': self.setting('position:' + doc_id, 1), 'pages': [
            {**row, 'stale': row['text_version'] != version} for row in rows]}

    @staticmethod
    def _text_version(doc):
        return hashlib.sha256(f"{doc['text_revision']}\0{doc['text']}".encode('utf-8')).hexdigest()

    @staticmethod
    def _bump_text(db, doc_id):
        db.execute('INSERT INTO text_versions VALUES (?,1) ON CONFLICT(document_id) DO UPDATE SET revision=revision+1', (doc_id,))

    def save_page_note(self, doc_id, page, start_line, reviewed, text_version):
        with self.connect() as db:
            doc = self._get(db, doc_id)
            current = self._text_version(doc)
            if current != text_version:
                raise ValueError('正文已更新，请重新载入后设置分页位置。')
            if doc['status'] in ('running', 'queued'):
                raise ValueError('请在识别结束后设置分页位置。')
            if not doc['text'].strip() or not 1 <= page <= doc['pages'] or not 1 <= start_line <= len(doc['text'].split('\n')):
                raise ValueError('页码或正文起始行无效。')
            for row in db.execute('SELECT * FROM page_notes WHERE document_id=? AND text_version=? AND page!=?',
                                  (doc_id, current, page)):
                if (row['page'] < page and row['start_line'] >= start_line or
                        row['page'] > page and row['start_line'] <= start_line):
                    raise ValueError('各页起始行必须按页码递增，请先调整相邻页的定位。')
            db.execute('INSERT OR REPLACE INTO page_notes VALUES (?,?,?,?,?)',
                       (doc_id, page, start_line, bool(reviewed), current))
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', ('pages_revision',json.dumps(uuid.uuid4().hex)))
        return self.page_notes(doc_id)

    def rename_collection(self, source, target):
        target = target.strip()
        if not source or not 1 <= len(target) <= 100 or source == target:
            raise ValueError('请填写不同的有效诗集名称。')
        with self.connect() as db:
            if not db.execute('SELECT 1 FROM collections WHERE name=?', (source,)).fetchone():
                raise ValueError('原诗集不存在。')
            db.execute('INSERT OR IGNORE INTO collections VALUES (?)', (target,))
            db.execute('UPDATE documents SET collection=?,updated_at=? WHERE collection=?', (target, now(), source))
            for row in db.execute('SELECT * FROM drafts').fetchall():
                payload = json.loads(row['payload'])
                if payload.get('collection') == source:
                    payload.update(collection=target, draft_version=uuid.uuid4().hex)
                    db.execute('UPDATE drafts SET payload=?,updated_at=? WHERE document_id=?',
                               (json.dumps(payload, ensure_ascii=False), now(), row['document_id']))
            db.execute('DELETE FROM collections WHERE name=?', (source,))

    def remove_empty_collection(self, name):
        with self.connect() as db:
            if db.execute('SELECT 1 FROM documents WHERE collection=?', (name,)).fetchone():
                raise ValueError('诗集仍含诗稿（包括回收站），请先归档到其他诗集。')
            for row in db.execute('SELECT payload FROM drafts'):
                if json.loads(row['payload']).get('collection') == name:
                    raise ValueError('恢复草稿仍使用此诗集，请先处理草稿。')
            db.execute('DELETE FROM collections WHERE name=?', (name,))

    def storage_info(self):
        exports = sorted((self.root / 'exports').glob('*.zip'), key=lambda p: p.stat().st_mtime, reverse=True)
        backups = [p for p in exports if p.name.startswith('拾笺备份-')]
        return {'export_bytes': sum(p.stat().st_size for p in exports),
                'exports': [{'name': p.name, 'bytes': p.stat().st_size} for p in exports],
                'last_backup': backups[0].name if backups else None,
                'free_bytes': shutil.disk_usage(self.root).free, 'export_dir': str(self.root / 'exports')}

    def seed(self):
        for i, (title, author, era, text, collection, motif) in enumerate(POEMS):
            doc_id = "demo-" + str(i + 1)
            relative = f"originals/{doc_id}.jpg"
            image, attribution = artwork(motif)
            shutil.copyfile(image, self.root / relative)
            with self.connect() as db:
                db.execute(
                    """INSERT OR IGNORE INTO documents
                    (id,title,author,era,collection,filename,source,preview,sha256,text,raw_text,status,favorite,reviewed,demo,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        doc_id,
                        title,
                        author,
                        era,
                        collection,
                        title + "-赏读配图.jpg",
                        relative,
                        relative,
                        doc_id,
                        text,
                        text,
                        "done",
                        int(i in (0, 3)),
                        1,
                        1,
                        now(),
                        now(),
                    ),
                )
                db.execute("INSERT OR IGNORE INTO collections VALUES (?)", (collection,))

    def migrate_demo_art(self):
        for i, (title, _, _, _, _, key) in enumerate(POEMS):
            image, attribution = artwork(key)
            doc_id = "demo-" + str(i + 1)
            relative = f"originals/{doc_id}.jpg"
            shutil.copyfile(image, self.root / relative)
            with self.connect() as db:
                db.execute(
                    "UPDATE documents SET source=?,preview=?,filename=?,notes=? WHERE id=? AND demo=1",
                    (
                        relative,
                        relative,
                        title + "-赏读配图.jpg",
                        artwork_note(attribution),
                        doc_id,
                    ),
                )

    def get(self, doc_id):
        with self.connect() as db:
            return self._get(db, doc_id)

    @staticmethod
    def _get(db, doc_id):
        row = db.execute("SELECT d.*,COALESCE(v.revision,0) AS text_revision FROM documents d LEFT JOIN text_versions v ON v.document_id=d.id WHERE d.id=?", (doc_id,)).fetchone()
        if not row:
            raise KeyError("找不到这份诗稿")
        return dict(row)

    def all(self, include_trash=False):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM documents "
                + ("" if include_trash else "WHERE trashed=0 ")
                + "ORDER BY created_at DESC,id"
            ).fetchall()
        return [dict(r) for r in rows]

    def summaries(self, ids=None):
        # Keep long text and notes inside SQLite; list and polling responses are bounded.
        columns = """id,title,author,era,collection,filename,pages,status,favorite,
                     reviewed,demo,trashed,error,created_at,updated_at,
                     substr(text,1,180) AS excerpt,length(text)>0 AS has_text"""
        parameters = tuple(dict.fromkeys(ids)) if ids is not None else ()
        if ids is not None and not parameters:
            return []
        where = (
            " WHERE id IN (" + ",".join("?" for _ in parameters) + ")" if ids is not None else ""
        )
        with self.connect() as db:
            rows = db.execute(
                "SELECT " + columns + " FROM documents" + where + " ORDER BY created_at DESC,id",
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def change_token(self):
        with self.connect() as db:
            documents=tuple(db.execute('SELECT count(*),max(updated_at) FROM documents').fetchone())
            collections=[row[0] for row in db.execute('SELECT name FROM collections ORDER BY name')]
            settings=[tuple(row) for row in db.execute("SELECT key,value FROM settings WHERE key IN ('preferences','engine','pages_revision') ORDER BY key")]
        return hashlib.sha256(json.dumps([documents,collections,settings],ensure_ascii=False).encode('utf-8')).hexdigest()

    def search(self, query):
        fields = ("title", "author", "era", "text", "notes", "filename", "collection")
        clause = " OR ".join(f"instr(casefold({field}), ?) > 0" for field in fields)
        with self.connect() as db:
            return [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM documents WHERE " + clause, (query.casefold(),) * len(fields)
                )
            ]

    def update(self, doc_id, **values):
        allowed = {
            "title",
            "author",
            "era",
            "collection",
            "text",
            "raw_text",
            "notes",
            "status",
            "favorite",
            "reviewed",
            "trashed",
            "error",
        }
        if set(values) - allowed:
            raise ValueError("不支持的字段")
        values["updated_at"] = now()
        with self.connect() as db:
            if 'text' in values and self._get(db,doc_id)['text']!=values['text']:
                self._bump_text(db,doc_id)
            db.execute(
                "UPDATE documents SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                (*values.values(), doc_id),
            )

    def draft(self, doc_id):
        with self.connect() as db:
            self._get(db, doc_id)
            row = db.execute("SELECT payload FROM drafts WHERE document_id=?", (doc_id,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def save_draft(self, doc_id, values):
        with self.connect() as db:
            doc = self._get(db, doc_id)
            row = db.execute("SELECT payload FROM drafts WHERE document_id=?", (doc_id,)).fetchone()
            current = json.loads(row["payload"]).get("draft_version") if row else None
            if values.get("draft_version") != current:
                raise ValueError("另一窗口已更新恢复草稿。本窗口文字仍保留，请先复制留存，再重新打开校对。")
            if row is None and values.get("base_updated_at") != doc["updated_at"]:
                raise ValueError("正文已更新，恢复草稿尚未写入。请保留本窗口文字并核对当前正文。")
            values = {**values, "draft_version": uuid.uuid4().hex}
            db.execute("INSERT OR REPLACE INTO drafts VALUES (?,?,?)",
                       (doc_id, json.dumps(values, ensure_ascii=False), now()))
        return values

    def edit(self, doc_id, values, expected_updated_at=None, draft_version=None):
        allowed = {
            "title",
            "author",
            "era",
            "collection",
            "text",
            "notes",
            "favorite",
            "reviewed",
            "trashed",
        }
        if set(values) - allowed:
            raise ValueError("不支持的字段")
        with self.connect() as db:
            if expected_updated_at is not None and self._get(db, doc_id)["updated_at"] != expected_updated_at:
                raise ValueError("正文已在其他窗口或识别任务中更新。请保留本窗口文字，重新打开并核对后保存。")
            result = self._edit(db, doc_id, dict(values))
            if "text" in values and draft_version is not None:
                row = db.execute("SELECT payload FROM drafts WHERE document_id=?", (doc_id,)).fetchone()
                if row and json.loads(row["payload"]).get("draft_version") == draft_version:
                    db.execute("DELETE FROM drafts WHERE document_id=?", (doc_id,))
            return result

    def finish_recognition(self, doc_id, text):
        if not text.strip():
            raise ValueError("未识别到文字。请尝试更清晰的照片或更高的识别档位。")
        with self.connect() as db:
            return self._edit(
                db,
                doc_id,
                {
                    "text": text,
                    "raw_text": text,
                    "reviewed": 0,
                    "error": "",
                },
                recognizing=True,
            )

    def _edit(self, db, doc_id, values, recognizing=False):
        old = self._get(db, doc_id)
        if old["status"] in ("running", "queued") and not recognizing:
            raise ValueError("正在识别，请等待完成后校对。")
        text = values.get("text", old["text"])
        if values.get("reviewed") and not text.strip():
            raise ValueError("请先识别或填写文字，再标记为已校对。")
        if any(
            k in values and values[k] != old[k] for k in ("text", "title", "author", "era", "notes")
        ):
            db.execute(
                """INSERT INTO revisions(document_id,text,title,author,era,notes,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (doc_id, old["text"], old["title"], old["author"], old["era"], old["notes"], now()),
            )
            values.setdefault("reviewed", 0)
        if "text" in values:
            if text!=old['text']:self._bump_text(db,doc_id)
            values["status"] = "done" if text.strip() else "new"
            values["error"] = ""
        if not text.strip():
            values["reviewed"] = 0
        if "collection" in values:
            db.execute("INSERT OR IGNORE INTO collections VALUES (?)", (values["collection"],))
        values["updated_at"] = now()
        db.execute(
            "UPDATE documents SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
            (*values.values(), doc_id),
        )
        return self._get(db, doc_id)

    def import_file(self, path, filename, collection="未分诗集"):
        filename = filename.replace("\\", "/").split("/")[-1]
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED:
            raise ValueError("支持 JPG、PNG、WEBP、BMP 和 PDF 文件。")
        if not 0 < path.stat().st_size <= MAX_FILE:
            raise ValueError("单个文件需大于 0 字节且不超过 200 MB。")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        with self.connect() as db:
            existing = db.execute(
                "SELECT id,trashed FROM documents WHERE sha256=?", (digest,)
            ).fetchone()
            if existing:
                if existing["trashed"]:
                    self.update(existing["id"], trashed=0)
                return self.get(existing["id"]), True
        doc_id = uuid.uuid4().hex
        preview = self.root / "previews" / (doc_id + (".jpg" if ext == ".pdf" else "-v2.jpg"))
        try:
            if ext == ".pdf":
                with PDF_LOCK:
                    pdf = pdfium.PdfDocument(str(path))
                    try:
                        pages = len(pdf)
                        if not 1 <= pages <= 600:
                            raise ValueError("每份 PDF 最多支持 600 页，请先拆分。")
                        page = pdf[0]
                        scale = min(1.8, 1600 / max(page.get_size()))
                        bitmap = page.render(scale=scale)
                        bitmap.to_pil().convert("RGB").save(preview, quality=88)
                        bitmap.close()
                        page.close()
                    finally:
                        pdf.close()
            else:
                with Image.open(path) as image:
                    image.load()
                    pages = 1
                    thumb = manuscript_rgb(image)
                    thumb.thumbnail((1600, 1600))
                    thumb.save(preview, quality=88)
        except Exception as e:
            preview.unlink(missing_ok=True)
            raise ValueError("无法读取图片或 PDF；请检查文件是否损坏、加密或尺寸过大。") from e
        source = self.root / "originals" / (doc_id + ext)
        try:
            shutil.copyfile(path, source)
            with self.connect() as db:
                db.execute(
                    """INSERT INTO documents
                    (id,title,collection,filename,source,preview,sha256,pages,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        doc_id,
                        Path(filename).stem,
                        collection,
                        filename,
                        source.relative_to(self.root).as_posix(),
                        preview.relative_to(self.root).as_posix(),
                        digest,
                        pages,
                        now(),
                        now(),
                    ),
                )
                db.execute("INSERT OR IGNORE INTO collections VALUES (?)", (collection,))
        except Exception:
            source.unlink(missing_ok=True)
            preview.unlink(missing_ok=True)
            raise
        return self.get(doc_id), False

    def page_preview(self, doc_id, number, full=False):
        doc = self.get(doc_id)
        if not 1 <= number <= doc["pages"]:
            raise ValueError("页码超出范围")
        if full and Path(doc["source"]).suffix != ".pdf":
            return self.root / doc["source"]
        if number == 1 and not full:
            if not doc["demo"] and Path(doc["source"]).suffix != ".pdf":
                return self.image_preview(doc)
            return self.root / doc["preview"]
        destination = self.root / "previews" / f"{doc_id}-{number}{'-full' if full else ''}.jpg"
        with PDF_LOCK:
            if not destination.exists():
                pdf = pdfium.PdfDocument(str(self.root / doc["source"]))
                try:
                    page = pdf[number - 1]
                    bitmap = page.render(
                        scale=min(4 if full else 2, (3000 if full else 2200) / max(page.get_size()))
                    )
                    bitmap.to_pil().convert("RGB").save(destination, quality=92)
                    bitmap.close()
                    page.close()
                finally:
                    pdf.close()
        return destination

    def image_preview(self, doc):
        """Upgrade old thumbnails on demand, keeping previous snapshots' files intact."""
        destination = self.root / "previews" / (doc["id"] + "-v2.jpg")
        if self.root / doc["preview"] == destination:
            return destination
        with self.preview_lock:
            if not destination.exists():
                temporary = self.root / "tmp" / (uuid.uuid4().hex + ".jpg")
                try:
                    with Image.open(self.root / doc["source"]) as image:
                        thumb = manuscript_rgb(image)
                        thumb.thumbnail((1600, 1600))
                        thumb.save(temporary, quality=88)
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
            with self.connect() as db:
                db.execute(
                    "UPDATE documents SET preview=? WHERE id=?",
                    (destination.relative_to(self.root).as_posix(), doc["id"]),
                )
        return destination

    @contextmanager
    def result_attempt(self, doc_id):
        # Completed attempts are immutable. Backup must never copy files still being written.
        work = self.root / "results" / doc_id / uuid.uuid4().hex[:12]
        with self.lock:
            work.mkdir(parents=True)
            self.active_results.add(work)
        try:
            yield work
        finally:
            with self.lock:
                self.active_results.discard(work)

    def backup(self):
        filename = f"拾笺备份-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.zip"
        target = self.root / "exports" / filename
        snapshot = self.root / "tmp" / (uuid.uuid4().hex + ".sqlite3")
        try:
            with self.connect() as db:
                copy = sqlite3.connect(snapshot)
                try:
                    db.backup(copy)
                finally:
                    copy.close()
                documents = db.execute("SELECT id,source,preview FROM documents").fetchall()
                files = {self.root / doc[key] for doc in documents for key in ("source", "preview")}
                skipped = []
                for doc in documents:
                    result_dir = self.root / "results" / doc["id"]
                    if not result_dir.exists():
                        continue
                    for attempt in result_dir.iterdir():
                        if attempt in self.active_results:
                            skipped.append(attempt.relative_to(self.root).as_posix())
                        elif attempt.is_dir():
                            files.update(file for file in attempt.rglob("*") if file.is_file())
                created_at = now()
            required = snapshot.stat().st_size + sum(file.stat().st_size for file in files)
            from archive_restore import MAX_BYTES, MAX_FILES
            if required+20*1024**2>MAX_BYTES or len(files)+3>MAX_FILES:
                raise ValueError('完整备份超过恢复上限（32 GiB、100000 个文件），请先整理已结束的识别结果副本。')
            if shutil.disk_usage(self.root).free < required * 1.05 + 20 * 1024**2:
                raise ValueError('诗库所在磁盘空间不足以生成完整备份，请先将已有导出副本移至其他磁盘。')
            # Originals, first-page previews, and completed attempts are immutable.
            # Lazy PDF page previews can be rebuilt and need not hold up the snapshot.
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(snapshot, "library.sqlite3")
                checksums = {}
                for file in [snapshot, *sorted(files)]:
                    name = 'library.sqlite3' if file == snapshot else file.relative_to(self.root).as_posix()
                    with file.open('rb') as stream:
                        checksums[name] = {'sha256': hashlib.file_digest(stream, 'sha256').hexdigest(), 'bytes': file.stat().st_size}
                for file in sorted(files):
                    archive.write(file, file.relative_to(self.root).as_posix())
                archive.writestr(
                    "恢复说明.txt",
                    "关闭拾笺，将此压缩包完整解压至新的空文件夹。启动拾笺前设置 SHIJIAN_DATA_DIR 为该文件夹的绝对路径，即可恢复完整诗库。识别引擎需另行安装。备份中的运行任务恢复后显示已中断；正在写入的识别产物不纳入备份。PDF 分页预览会按需重新生成。\n",
                )
                archive.writestr(
                    "manifest.json",
                    json.dumps(
                        {
                            "app": "shijian",
                            "version": 2,
                            "files": checksums,
                            "created_at": created_at,
                            "skipped_active_results": skipped,
                        },
                        ensure_ascii=False,
                    ),
                )
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            snapshot.unlink(missing_ok=True)
        return target

    def export(self, ids, format="txt"):
        if format not in ("txt", "md", "archive"):
            raise ValueError("不支持的导出格式")
        # Capture documents and history together; compression never holds the library lock.
        snapshot = []
        with self.connect() as db:
            for doc_id in dict.fromkeys(ids):
                doc = self._get(db, doc_id)
                versions = [dict(r) for r in db.execute(
                    "SELECT * FROM revisions WHERE document_id=? ORDER BY id", (doc_id,)
                )] if format == "archive" else []
                snapshot.append((doc, versions))
        if not snapshot:
            raise ValueError("请先选择要导出的诗稿")
        target = self.root / "exports" / f"拾笺诗集-{uuid.uuid4().hex[:8]}.zip"
        temporary = self.root / "tmp" / (uuid.uuid4().hex + ".zip")
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                for doc, versions in snapshot:
                    doc_id = doc["id"]
                    name = safe_name(doc["title"]) + "-" + doc_id[:8]
                    content = f"{doc['title']}\n{doc['era']} · {doc['author']}\n\n{doc['text']}\n"
                    if format == "md":
                        content = "# " + content
                    archive.writestr(
                        name + (".md" if format == "md" else ".txt"),
                        content.encode("utf-8-sig" if format == "txt" else "utf-8"),
                    )
                    if format == "archive":
                        archive.write(
                            self.root / doc["source"], name + "/原稿" + Path(doc["source"]).suffix
                        )
                        archive.writestr(
                            name + "/档案.json", json.dumps(doc, ensure_ascii=False, indent=2)
                        )
                        archive.writestr(
                            name + "/校对历史.json", json.dumps(versions, ensure_ascii=False, indent=2)
                        )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target
