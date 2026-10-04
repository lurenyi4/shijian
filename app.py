from __future__ import annotations

import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import LocalEngine
from storage import Library, MAX_FILE, default_data_dir

STATIC = Path(__file__).resolve().parent / "static"


class Edit(BaseModel):
    expected_updated_at: str | None = Field(None, max_length=100)
    draft_version: str | None = Field(None, max_length=100)
    title: str | None = Field(None, min_length=1, max_length=200)
    author: str | None = Field(None, max_length=100)
    era: str | None = Field(None, max_length=100)
    collection: str | None = Field(None, min_length=1, max_length=100)
    text: str | None = Field(None, max_length=5_000_000)
    notes: str | None = Field(None, max_length=20000)
    favorite: bool | None = None
    reviewed: bool | None = None
    trashed: bool | None = None


class Selection(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=10000)
    force: bool = False


class Draft(BaseModel):
    draft_version: str | None = Field(None, max_length=100)
    title: str = Field("", max_length=200)
    author: str = Field("", max_length=100)
    era: str = Field("", max_length=100)
    collection: str = Field("未分诗集", min_length=1, max_length=100)
    text: str = Field("", max_length=5_000_000)
    notes: str = Field("", max_length=20000)
    base_updated_at: str = Field(max_length=100)


class Preferences(BaseModel):
    large_text: bool = False


class Export(Selection):
    format: str = "txt"


class Settings(BaseModel):
    executable: str = Field("", max_length=1000)
    tier: str = "standard"
    model_source: str = "modelscope"


def create_app(data_dir=None, seed=True, run_worker=True):
    library = Library(Path(data_dir) if data_dir else default_data_dir(), seed=seed)
    engine = LocalEngine(library)
    session = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        if run_worker:
            engine.start()
        yield
        engine.stop()

    app = FastAPI(title="拾笺", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.library = library
    app.state.engine = engine
    app.state.session = session

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Host and Origin checks prevent malicious sites from driving a localhost service.
        if request.url.hostname not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "不允许跨站请求"}, status_code=403)
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD"):
            if not secrets.compare_digest(request.headers.get("x-shijian-session", ""), session):
                return JSONResponse({"detail": "会话已过期，请刷新窗口。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; object-src 'none'"
        )
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith("/api/") else "no-cache"
        )
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def key_error(request, exc):
        return JSONResponse({"detail": str(exc.args[0])}, status_code=404)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    def state():
        summaries = library.summaries()
        with library.connect() as db:
            collections = [
                r["name"] for r in db.execute("SELECT name FROM collections ORDER BY name")
            ]
        return {
            "documents": summaries,
            "collections": collections,
            "engine": engine.info(),
            "session": session,
            "preferences": library.setting("preferences", {"large_text": False}),
        }

    @app.get("/api/search")
    def search(q: str = Query(min_length=1, max_length=500)):
        return {"ids": library.search(q.strip())}

    @app.post("/api/tasks/status")
    def task_status(payload: Selection):
        return {"documents": library.summaries(payload.ids), "engine": engine.info()}

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: str):
        return library.get(doc_id)

    @app.patch("/api/documents/{doc_id}")
    def edit(doc_id: str, payload: Edit):
        values = payload.model_dump(exclude_none=True)
        expected = values.pop("expected_updated_at", None)
        draft_version = values.pop("draft_version", None)
        if values.get("trashed") and library.get(doc_id)["status"] in ("queued", "running"):
            engine.cancel([doc_id])
        return library.edit(doc_id, values, expected, draft_version)

    @app.get("/api/documents/{doc_id}/draft")
    def draft(doc_id: str):
        return library.draft(doc_id)

    @app.put("/api/documents/{doc_id}/draft")
    def save_draft(doc_id: str, payload: Draft):
        return library.save_draft(doc_id, payload.model_dump())

    @app.put("/api/preferences")
    def preferences(payload: Preferences):
        values = payload.model_dump()
        library.set_setting("preferences", values)
        return values

    @app.get("/api/documents/{doc_id}/revisions")
    def revisions(doc_id: str):
        library.get(doc_id)
        with library.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM revisions WHERE document_id=? ORDER BY id DESC", (doc_id,)
                )
            ]

    @app.get("/api/documents/{doc_id}/preview")
    def preview(doc_id: str, page: int = 1):
        return FileResponse(library.page_preview(doc_id, page))

    @app.get("/api/documents/{doc_id}/original")
    def original(doc_id: str):
        doc = library.get(doc_id)
        return FileResponse(library.root / doc["source"], filename=doc["filename"])

    @app.get("/api/documents/{doc_id}/image")
    def full_image(doc_id: str, page: int = 1):
        return FileResponse(library.page_preview(doc_id, page, full=True))

    @app.post("/api/import")
    def import_file(file: UploadFile, collection: str = "未分诗集"):
        if not 1 <= len(collection) <= 100:
            raise ValueError("诗集名称应为 1–100 个字")
        target = library.root / "tmp" / uuid.uuid4().hex
        total = 0
        try:
            with target.open("wb") as destination:
                while chunk := file.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_FILE:
                        raise ValueError("单个文件不能超过 200 MB")
                    destination.write(chunk)
            doc, duplicate = library.import_file(target, file.filename or "untitled", collection)
            return {"document": doc, "duplicate": duplicate}
        finally:
            target.unlink(missing_ok=True)
            file.file.close()

    @app.post("/api/collections")
    async def collection(request: Request):
        data = await request.json()
        name = str(data.get("name", "")).strip()
        if not 1 <= len(name) <= 100:
            raise ValueError("请填写 1–100 字的诗集名称")
        with library.connect() as db:
            db.execute("INSERT OR IGNORE INTO collections VALUES (?)", (name,))
        return {"name": name}

    @app.put("/api/settings")
    def settings(payload: Settings):
        if payload.tier not in ("flash", "basic", "standard", "advanced"):
            raise ValueError("不支持的识别档位")
        if payload.model_source not in ("modelscope", "huggingface", "local"):
            raise ValueError("不支持的模型来源")
        config = payload.model_dump()
        config["executable"] = config["executable"].strip().strip('"')
        library.set_setting("engine", config)
        return engine.info()

    @app.post("/api/recognize")
    def recognize(payload: Selection):
        return {"accepted": engine.enqueue(payload.ids, payload.force)}

    @app.post("/api/cancel")
    def cancel(payload: Selection):
        engine.cancel(payload.ids)
        return {"ok": True}

    @app.post("/api/export")
    def export(payload: Export):
        path = library.export(payload.ids, payload.format)
        return {"url": "/api/download/" + path.name, "filename": path.name}

    @app.post("/api/backup")
    def backup():
        path = library.backup()
        return {"url": "/api/download/" + path.name, "filename": path.name}

    @app.get("/api/download/{filename}")
    def download(filename: str):
        path = (library.root / "exports" / filename).resolve()
        if path.parent != (library.root / "exports").resolve() or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, filename=path.name, media_type="application/zip")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
