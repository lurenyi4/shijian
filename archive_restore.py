"""Verify local library archives and publish a restored copy in a new folder."""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tempfile
import zipfile

MAX_BYTES = 32 * 1024 ** 3
MAX_FILES = 100000


def _inventory(archive):
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    if len(names) != len({name.casefold() for name in names}) or len(names) > MAX_FILES:
        raise ValueError('备份成员重复或数量超过上限。')
    total = sum(entry.file_size for entry in entries)
    if total > MAX_BYTES:
        raise ValueError('备份解压后超过 32 GiB 上限。')
    for entry in entries:
        p = PurePosixPath(entry.filename)
        if (not p.parts or str(p)!=entry.filename or any(part.rstrip(' .')!=part for part in p.parts)
                or p.is_absolute() or '..' in p.parts or '\\' in entry.filename or ':' in entry.filename
                or entry.is_dir() or (entry.external_attr >> 16) & 0o170000 == 0o120000
                or not (entry.filename in ('manifest.json', 'library.sqlite3', '恢复说明.txt')
                        or p.parts[0] in ('originals', 'previews', 'results'))):
            raise ValueError('备份包含不支持的路径或成员。')
    if not {'manifest.json', 'library.sqlite3'}.issubset(names):
        raise ValueError('缺少诗库数据库或清单。')
    if archive.getinfo('manifest.json').file_size > 16 * 1024 ** 2:
        raise ValueError('备份清单过大。')
    manifest = json.loads(archive.read('manifest.json'))
    if not isinstance(manifest,dict) or manifest.get('app') != 'shijian' or manifest.get('version') not in (1, 2):
        raise ValueError('不支持此备份版本。')
    checks = manifest.get('files', {})
    content = set(names) - {'manifest.json', '恢复说明.txt'}
    if manifest['version'] == 2 and (not isinstance(checks, dict) or set(checks) != content):
        raise ValueError('备份校验清单与文件不一致。')
    if manifest['version']==2:
        for name,expected in checks.items():
            if (not isinstance(expected,dict) or type(expected.get('bytes')) is not int or
                    expected['bytes']!=archive.getinfo(name).file_size or
                    not isinstance(expected.get('sha256'),str) or not re.fullmatch('[a-f0-9]{64}',expected['sha256'])):
                raise ValueError('备份文件校验字段无效。')
    return entries, manifest, total


def _verify(archive, entries, manifest, destination=None):
    for entry in entries:
        digest = hashlib.sha256()
        target = destination / entry.filename if destination else None
        if target:
            target.parent.mkdir(parents=True, exist_ok=True)
        output = target.open('xb') if target else None
        try:
            with archive.open(entry) as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                    if output:
                        output.write(chunk)
        finally:
            if output:
                output.close()
        expected = manifest.get('files', {}).get(entry.filename)
        if expected and (expected.get('bytes') != entry.file_size or expected.get('sha256') != digest.hexdigest()):
            raise ValueError('备份文件校验失败：' + entry.filename)


def inspect_backup(source):
    try:
        with zipfile.ZipFile(source) as archive:
            entries, manifest, total = _inventory(archive)
            _verify(archive, entries, manifest)
            return {'files': len(entries), 'bytes': total, 'verified': manifest['version'] == 2,
                    'created_at': manifest.get('created_at'),
                    'skipped_tasks': len(manifest.get('skipped_active_results', []))}
    except (zipfile.BadZipFile, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('备份损坏或清单无效。') from exc


def restore_backup(source, destination):
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError('请选择已有父目录下尚不存在的新文件夹，当前诗库不会被覆盖。')
    with zipfile.ZipFile(source) as archive:
        entries, manifest, total = _inventory(archive)
        if shutil.disk_usage(destination.parent).free < total * 1.05 + 20 * 1024 ** 2:
            raise ValueError('恢复目标空间不足。')
        staging = Path(tempfile.mkdtemp(prefix='.shijian-restore-', dir=destination.parent))
        try:
            _verify(archive, entries, manifest, staging)
            db = sqlite3.connect((staging / 'library.sqlite3').as_uri() + '?mode=ro', uri=True)
            try:
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('诗库数据库完整性检查失败。')
                for source_path, preview, digest, demo in db.execute('SELECT source,preview,sha256,demo FROM documents'):
                    for name in (source_path, preview):
                        path = (staging / name).resolve()
                        if not path.is_relative_to(staging) or not path.is_file():
                            raise ValueError('诗库引用的原稿或预览缺失。')
                    if not demo:
                        with (staging/source_path).open('rb') as stream:
                            if hashlib.file_digest(stream,'sha256').hexdigest()!=digest:
                                raise ValueError('原稿与诗库保存的哈希不一致。')
            finally:
                db.close()
            staging.rename(destination)
        except Exception as exc:
            raise ValueError(f'恢复未发布，当前诗库未改动。检查用临时副本保留在 {staging}。原因：{exc}') from exc
    return destination
