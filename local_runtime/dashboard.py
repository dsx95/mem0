"""Single-owner web console for the existing local Qdrant memory collection."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from .portable_paths import map_metadata, map_path

import json
import mimetypes
import os
import queue
import re
import sqlite3
import threading
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .material_errors import MaterialError
from .materials import DEFAULT_ROOT, chunk_records, digest, error_summary, existing_chunks, file_hash, write_json
from .runtime import PROJECT_ROOT, close_memory, create_memory, load_settings
from .rerank import Reranker
from .videos import VIDEO_EXTENSIONS
from .web_parsing import WEB_EXTENSIONS, WebParser

STATIC = Path(__file__).with_name("web")
ACTIVE = {"queued", "parsing", "writing"}


def now():
    return datetime.now(timezone.utc).isoformat()


def category(payload):
    extension = "." + payload.get("source_type", "")
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in {".png", ".jpg", ".jpeg", ".gif"}:
        return "image"
    if extension in {".pdf", ".doc", ".docx"}:
        return "document"
    if extension in {".json", ".csv"}:
        return "structured"
    return "text" if payload.get("source_file") else "chat"


def safe_filename(value):
    if not value or len(value.encode()) > 220 or any(c in value for c in ("/", "\\", "\x00")):
        raise HTTPException(400, "文件名无效，请勿包含目录")
    if value.startswith(".") or any(ord(c) < 32 for c in value):
        raise HTTPException(400, "不支持隐藏文件或控制字符")
    if Path(value).suffix.lower() not in WEB_EXTENSIONS:
        raise HTTPException(415, "暂不支持此格式；旧版 DOC 请先转换为 DOCX 或 PDF")
    return value


class Dashboard:
    def __init__(self, settings, root=DEFAULT_ROOT, memory_factory=create_memory, max_upload_mb=512):
        self.settings, self.root, self.memory_factory = settings, Path(root).resolve(), memory_factory
        self.directory = settings.data_dir / "dashboard"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.jobs_db = self.directory / "jobs.sqlite"
        self.limit = max_upload_mb * 1024 * 1024
        self.memory = None
        self.reranker = Reranker(getattr(settings, "rerank", None))
        self.memory_lock, self.state_lock = threading.RLock(), threading.RLock()
        self.items, self.jobs = [], {}
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.worker = None
        self.parser_factory = WebParser
        self.error = None
        self.upload_gate = asyncio.Lock()
        with sqlite3.connect(self.jobs_db) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            for (body,) in db.execute("SELECT body FROM jobs"):
                job = map_metadata(json.loads(body))
                self.jobs[job["id"]] = job

    def save_job(self, job_id, **updates):
        with self.state_lock:
            job = self.jobs[job_id]
            job.update(updates, updated_at=now())
            with sqlite3.connect(self.jobs_db, timeout=15) as db:
                db.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?)", (job_id, json.dumps(map_metadata(job, store=True), ensure_ascii=False)))
            return dict(job)

    def start(self):
        self.memory = self.memory_factory(self.settings)
        if getattr(self.memory, "runtime_reranker", None):
            self.reranker.close()
            self.reranker = self.memory.runtime_reranker
        self.refresh()
        for job in list(self.jobs.values()):
            if job["status"] in ACTIVE:
                self.save_job(job["id"], status="queued", message="服务重启，等待恢复导入", error=None)
                self.pending.put(job["id"])
        self.worker = threading.Thread(target=self.work, name="memory-import", daemon=True)
        self.worker.start()

    def stop(self):
        self.stopping.set()
        self.pending.put(None)
        if self.worker:
            self.worker.join()
        if self.memory is not None:
            close_memory(self.memory)
        self.reranker.close()

    def refresh(self):
        values, offset = [], None
        with self.memory_lock:
            while True:
                records, offset = self.memory.vector_store.client.scroll(
                    collection_name=self.settings.collection,
                    limit=256,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                for record in records:
                    payload = map_metadata(dict(record.payload or {}))
                    text = payload.pop("data", "")
                    payload.pop("text_lemmatized", None)
                    payload.pop("hash", None)
                    values.append(
                        {
                            "id": str(record.id),
                            "memory": text,
                            "category": category(payload),
                            "user_id": payload.pop("user_id", ""),
                            "created_at": payload.pop("created_at", None),
                            "updated_at": payload.pop("updated_at", None),
                            "metadata": payload,
                        }
                    )
                if offset is None:
                    break
        values.sort(key=lambda x: (x.get("created_at") or "", x["id"]), reverse=True)
        with self.state_lock:
            self.items = values

    def snapshot(self):
        with self.state_lock:
            return list(self.items)

    def job_list(self):
        with self.state_lock:
            return sorted((dict(j) for j in self.jobs.values()), key=lambda j: j["created_at"], reverse=True)

    def source_list(self):
        sources = {}
        for item in self.snapshot():
            meta = item["metadata"]
            if not meta.get("source_file"):
                continue
            key = (item["user_id"], meta.get("source_root"), meta["source_file"], meta.get("source_sha256"))
            source = sources.setdefault(
                key,
                {
                    "source_file": meta["source_file"],
                    "user_id": item["user_id"],
                    "category": item["category"],
                    "source_sha256": meta.get("source_sha256"),
                    "chunks": 0,
                    "status": "complete",
                    "memory_id": item["id"],
                    "updated_at": item.get("created_at"),
                },
            )
            source["chunks"] += 1
        # Surface previous CLI failures too, e.g. the incomplete X1 video.
        for path in (self.settings.data_dir / "materials").glob("import-*.json"):
            report = json.loads(path.read_text())
            if report.get("collection") != self.settings.collection:
                continue
            for filename, data in report.get("files", {}).items():
                if data.get("status") != "failed":
                    continue
                key = (report.get("user_id"), report.get("root"), filename, data.get("sha256"))
                sources.setdefault(
                    key,
                    {
                        "source_file": filename,
                        "user_id": report.get("user_id"),
                        "category": "video" if Path(filename).suffix.lower() in VIDEO_EXTENSIONS else "document",
                        "chunks": 0,
                        "status": "failed",
                        "error": data.get("error"),
                        "source_sha256": data.get("sha256"),
                    },
                )
        return sorted(sources.values(), key=lambda x: (x["status"] != "failed", x["source_file"]))

    def accept(self, path, name, sha, size, user_id):
        with self.state_lock:
            active = next(
                (
                    j
                    for j in self.jobs.values()
                    if j["sha256"] == sha and j["user_id"] == user_id and j["status"] in ACTIVE
                ),
                None,
            )
            if active:
                path.unlink(missing_ok=True)
                return {**active, "already_queued": True}
            # Preserve an existing source identity when uploading the same bytes under another name.
            known = next(
                (
                    i
                    for i in self.items
                    if i["user_id"] == user_id
                    and i["metadata"].get("source_sha256") == sha
                    and i["metadata"].get("source_root") == str(self.root)
                ),
                None,
            )
            destination = self.root / "网页上传" / sha / name
            if known:
                candidate = Path(known["metadata"]["source_path"]).resolve()
                if candidate.is_relative_to(self.root) and candidate.is_file() and file_hash(candidate) == sha:
                    destination = candidate
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if file_hash(destination) != sha:
                    raise MaterialError("目标文件内容冲突，请重试")
                path.unlink(missing_ok=True)
            else:
                path.replace(destination)
            job_id = str(uuid.uuid4())
            job = {
                "id": job_id,
                "filename": name,
                "path": str(destination),
                "sha256": sha,
                "size": size,
                "user_id": user_id,
                "status": "queued",
                "created_at": now(),
                "message": "等待导入",
                "added": 0,
                "skipped": 0,
                "chunks": None,
                "processed": 0,
            }
            self.jobs[job_id] = job
            saved = self.save_job(job_id)
            self.pending.put(job_id)
            return saved

    def work(self):
        while not self.stopping.is_set():
            job_id = self.pending.get()
            if job_id is None:
                return
            try:
                self.run_job(job_id)
            except Exception as exc:
                self.save_job(job_id, status="failed", message="导入未完成，可修正文件后重试", error=error_summary(exc))
                self.refresh()

    def run_job(self, job_id):
        job = self.jobs[job_id]
        path = Path(job["path"])
        if not path.resolve().is_relative_to(self.root) or file_hash(path) != job["sha256"]:
            raise MaterialError("文件已变动或不存在，请重新上传")
        self.save_job(job_id, status="parsing", message="提取文字、图片或视频信息", error=None)
        parser = self.parser_factory(self.settings, self.settings.data_dir / "materials", "qwen-vl-plus")
        try:
            units = parser.parse(path, job["sha256"])
        finally:
            parser.close()
        if file_hash(path) != job["sha256"]:
            raise MaterialError("解析时文件发生变化，请重新上传")
        records = list(chunk_records(path, self.root, job["sha256"], units, "qwen-vl-plus"))
        if not records or len(records) > 2000:
            raise MaterialError("文件无有效片段或超过 2000 片段，请拆分后上传")
        with self.memory_lock:
            existing = existing_chunks(self.memory, job["user_id"], self.root)
        added, skipped, ids = 0, 0, []
        self.save_job(job_id, status="writing", message="写入记忆", chunks=len(records), processed=0)
        for index, (text, meta) in enumerate(records, 1):
            if self.stopping.is_set():
                self.save_job(job_id, status="queued", message="服务停止，下次启动继续")
                return
            if meta["ingest_key"] in existing:
                skipped += 1
                ids.append(existing[meta["ingest_key"]])
            else:
                with self.memory_lock:
                    result = self.memory.add(text, user_id=job["user_id"], infer=False, metadata=meta)
                    replies = result.get("results", [])
                    if len(replies) != 1 or replies[0].get("event") != "ADD":
                        raise MaterialError("写入未确认，重试时会核对已有片段")
                    memory_id = str(replies[0]["id"])
                    stored = self.memory.get(memory_id)
                    if not stored or stored.get("metadata", {}).get("ingest_key") != meta["ingest_key"]:
                        raise MaterialError("写入校验未通过，请重试")
                existing[meta["ingest_key"]] = memory_id
                ids.append(memory_id)
                added += 1
            self.save_job(job_id, processed=index, added=added, skipped=skipped)
        scope = digest((str(self.root) + self.settings.collection + job["user_id"]).encode())[:20]
        report_path = self.settings.data_dir / "materials" / ("import-" + scope + ".json")
        report = json.loads(report_path.read_text()) if report_path.exists() else {"files": {}}
        report.update(
            root=str(self.root), user_id=job["user_id"], collection=self.settings.collection, updated_at=now()
        )
        report["files"][str(path.relative_to(self.root))] = {
            "status": "complete",
            "sha256": job["sha256"],
            "units": len(units),
            "chunks": len(records),
            "added": added,
            "skipped": skipped,
            "memory_ids": ids,
        }
        write_json(report_path, report)
        self.refresh()
        self.save_job(job_id, status="complete", message="已存在，跳过重复写入" if not added else "导入完成")

    def resolve_file(self, memory_id, asset, index):
        item = next((x for x in self.snapshot() if x["id"] == memory_id), None)
        if item is None:
            raise HTTPException(404, "记忆不存在")
        meta = item["metadata"]
        value = meta.get({"source": "source_path", "preview": "preview_file", "audio": "audio_file"}[asset])
        if asset == "preview" and meta.get("preview_files"):
            files = meta["preview_files"]
            value = files[min(index, len(files) - 1)]
        if asset == "preview" and not value:
            value = meta.get("rendered_file")
        if not value:
            raise HTTPException(404, "此记录没有对应文件")
        path = Path(map_path(value)).resolve()
        allowed = [self.root, (self.settings.data_dir / "materials").resolve()]
        if not any(path.is_relative_to(root) for root in allowed) or not path.is_file():
            raise HTTPException(404, "原文件不可用")
        return path


def create_app(settings=None, root=DEFAULT_ROOT, memory_factory=create_memory, max_upload_mb=512):
    service = Dashboard(
        settings or load_settings(PROJECT_ROOT / "local_runtime/qwen.env"), root, memory_factory, max_upload_mb
    )

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(service.start)
        try:
            yield
        finally:
            if hasattr(app.state, "chat"):
                await asyncio.to_thread(app.state.chat.close)
            await asyncio.to_thread(service.stop)

    app = FastAPI(title="Knowin Memory", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    from .chat import install as install_chat

    install_chat(app, service)

    @app.middleware("http")
    async def boundaries(request, call_next):
        # This is a local, single-owner console. Block DNS rebinding and cross-origin writes.
        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"detail": "仅允许本机或 SSH 转发访问"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "不允许跨站请求"}, status_code=403)
            if request.headers.get("x-memory-client") != "dashboard":
                return JSONResponse({"detail": "缺少页面请求标识"}, status_code=403)
        try:
            response = await call_next(request)
        except Exception as exc:
            response = JSONResponse({"detail": error_summary(exc)}, status_code=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self'; frame-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; connect-src 'self'"
        )
        return response

    @app.get("/api/overview")
    def overview():
        items, sources = service.snapshot(), service.source_list()
        return {
            "total": len(items),
            "categories": dict(Counter(i["category"] for i in items)),
            "users": dict(Counter(i["user_id"] for i in items)),
            "source_count": sum(s["status"] == "complete" for s in sources),
            "failed_sources": sum(s["status"] == "failed" for s in sources),
            "jobs_active": sum(j["status"] in ACTIVE for j in service.job_list()),
            "collection": service.settings.collection,
            "embedding": service.settings.embedding.model,
            "dimensions": service.settings.embedding_dims,
            "llm": service.settings.llm.model,
            "extensions": sorted(WEB_EXTENSIONS),
            "max_upload_mb": service.limit // (1024 * 1024),
            "status": "ready",
            "mode": "local",
            "updated_at": now(),
        }

    @app.get("/api/memories")
    def memories(
        q: str = "",
        user_id: str = "",
        kind: str = "",
        source: str = "",
        page: int = Query(1, ge=1),
        page_size: int = Query(12, ge=1, le=100),
    ):
        items = service.snapshot()
        if user_id:
            items = [i for i in items if i["user_id"] == user_id]
        if kind:
            items = [i for i in items if i["category"] == kind]
        if source:
            items = [i for i in items if i["metadata"].get("source_file") == source]
        if q:
            items = [
                i for i in items if q.casefold() in (i["memory"] + i["metadata"].get("source_file", "")).casefold()
            ]
        return {"items": items[(page - 1) * page_size : page * page_size], "total": len(items), "page": page}

    @app.get("/api/search")
    def search(
        q: str = Query(min_length=1, max_length=2000),
        user_id: str = "",
        kind: str = "",
        source: str = "",
        limit: int = Query(20, ge=1, le=100),
    ):
        from qdrant_client.models import Filter

        candidates = [
            i
            for i in service.snapshot()
            if (not user_id or i["user_id"] == user_id)
            and (not kind or i["category"] == kind)
            and (not source or i["metadata"].get("source_file") == source)
        ]
        if not candidates:
            return {"items": [], "total": 0}
        # Filter using point IDs so category/source restrictions are applied before top-k.
        from qdrant_client.models import HasIdCondition

        conditions = [HasIdCondition(has_id=[i["id"] for i in candidates])]
        with service.memory_lock:
            embedding = service.memory.embedding_model.embed(q, "search")
            result = service.memory.vector_store.client.query_points(
                collection_name=service.settings.collection,
                query=embedding,
                query_filter=Filter(must=conditions),
                limit=service.reranker.candidate_limit(limit),
                with_payload=False,
            )
        lookup = {i["id"]: i for i in candidates}
        items = [{**lookup[str(p.id)], "score": p.score} for p in result.points if p.score >= 0.1]
        items, rerank = service.reranker.rank(q, items, limit)
        return {"items": items, "total": len(items), "semantic": True, "rerank": rerank}

    @app.get("/api/memories/{memory_id}")
    def detail(memory_id: str):
        item = next((i for i in service.snapshot() if i["id"] == memory_id), None)
        if item is None:
            raise HTTPException(404, "记忆不存在")
        with service.memory_lock:
            history = service.memory.history(memory_id)
        return {**item, "history": history}

    @app.get("/api/memories/{memory_id}/file")
    def source_file(
        memory_id: str,
        asset: str = Query("source", pattern="^(source|preview|audio)$"),
        index: int = Query(0, ge=0, le=100),
        download: bool = False,
    ):
        path = service.resolve_file(memory_id, asset, index)
        media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        inline = media.startswith(("image/", "video/", "audio/")) or media == "application/pdf"
        return FileResponse(
            path,
            media_type=media if inline else "application/octet-stream",
            filename=path.name,
            content_disposition_type="inline" if inline and not download else "attachment",
        )

    @app.get("/api/sources")
    def sources():
        return {"items": service.source_list()}

    @app.get("/api/jobs")
    def jobs():
        return {"items": service.job_list()}

    @app.post("/api/uploads", status_code=202)
    async def upload(request: Request, filename: str, user_id: str = "knowin_public"):
        name = safe_filename(filename)
        if not re.fullmatch(r"[\w.\-]{1,100}", user_id):
            raise HTTPException(400, "用户标识仅支持字母、数字、中文、点、下划线和短横线，最多 100 字符")
        length = request.headers.get("content-length")
        if length and int(length) > service.limit:
            raise HTTPException(413, "文件超过上传大小限制")
        if sum(j["status"] in ACTIVE for j in service.job_list()) >= 20:
            raise HTTPException(429, "待导入文件较多，请等待当前任务完成")
        temp = service.directory / ("upload-" + uuid.uuid4().hex + ".part")
        sha, size = hashlib.sha256(), 0
        try:
            with temp.open("xb") as handle:
                os.chmod(temp, 0o600)
                async for block in request.stream():
                    size += len(block)
                    if size > service.limit:
                        raise HTTPException(413, "文件超过上传大小限制")
                    handle.write(block)
                    sha.update(block)
            if not size:
                raise HTTPException(400, "不能上传空文件")
            async with service.upload_gate:
                return await asyncio.to_thread(service.accept, temp, name, sha.hexdigest(), size, user_id)
        finally:
            temp.unlink(missing_ok=True)

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str):
        with service.state_lock:
            job = service.jobs.get(job_id)
            if job is None:
                raise HTTPException(404, "任务不存在")
            if job["status"] != "failed":
                raise HTTPException(409, "只有失败的任务可以重试")
            saved = service.save_job(job_id, status="queued", message="等待重试", error=None)
            service.pending.put(job_id)
            return saved

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--port", type=int, default=18580)
    cli.add_argument("--env-file", type=Path, default=PROJECT_ROOT / "local_runtime/qwen.env")
    cli.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    cli.add_argument("--max-upload-mb", type=int, default=512)
    args = cli.parse_args()
    if not 1 <= args.max_upload_mb <= 2048:
        cli.error("--max-upload-mb must be between 1 and 2048")
    import uvicorn

    uvicorn.run(
        create_app(load_settings(args.env_file), args.root, max_upload_mb=args.max_upload_mb),
        host="127.0.0.1",
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
