"""System migration is local-operator access, separate from user memory permissions."""
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import secrets
import tempfile
import time
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from . import migration as m
from .access import viewer


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_api_keys: bool = False


class RestoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: uuid.UUID
    keep_local_identities: bool = False


class Migration:
    def __init__(self, service, chat):
        self.service, self.chat = service, chat
        self.root = service.settings.data_dir.parent
        self.directory = m.workdir(self.root)
        self.key = m.token(self.root)
        self.gate = asyncio.Lock()
        self.downloads = {}

    @property
    def pending(self):
        return (self.directory / "pending.json").exists()

    def authorize(self, request):
        if not secrets.compare_digest(request.headers.get("x-memory-migration-key", ""), self.key):
            raise HTTPException(403, "请输入本机迁移管理码；在项目终端执行 bash migrate.sh token 获取")

    @contextmanager
    def quiet(self):
        with self.chat.lock, self.service.memory_lock, self.service.state_lock:
            if self.chat.runs or any(j["status"] in {"queued", "parsing", "writing"} for j in self.service.job_list()):
                raise HTTPException(409, "请先等待对话回复和资料导入完成，再迁移")
            yield


def install(app, service):
    migration = Migration(service, app.state.chat)
    service.migration = migration

    @app.get("/api/migration/status")
    def status(request: Request):
        migration.authorize(request)
        last = migration.directory / "last-restore.json"
        return {"pending_restart": migration.pending, "last_restore": json.loads(last.read_text()) if last.exists() else None}

    @app.post("/api/migration/export")
    def export(body: ExportInput, request: Request):
        migration.authorize(request)
        try:
            with migration.quiet():
                package, manifest = m.export_bundle(migration.root, service.settings, client=service.memory.vector_store.client,
                                                    include_secrets=body.include_api_keys, materials=service.root)
        except m.MigrationError as exc:
            raise HTTPException(422, str(exc)) from exc
        key = secrets.token_urlsafe(32)
        migration.downloads = {k: v for k, v in migration.downloads.items() if v[2] > time.time()}
        migration.downloads[key] = (package, viewer(), time.time() + 600)
        return {"download_url": "/api/migration/files/" + key, "counts": manifest["counts"], "filename": package.name}

    @app.get("/api/migration/files/{ticket}")
    def download(ticket: str):
        entry = migration.downloads.get(ticket)
        if not entry or entry[1] != viewer() or entry[2] <= time.time():
            raise HTTPException(404, "下载链接无效或已过期，请重新导出")
        return FileResponse(entry[0], filename=entry[0].name, media_type="application/gzip")

    @app.post("/api/migration/imports")
    async def upload(request: Request):
        migration.authorize(request)
        directory = migration.directory / "uploads"
        directory.mkdir(mode=0o700, exist_ok=True)
        identity = str(uuid.uuid4())
        destination = directory / (identity + ".tar.gz")
        temp = tempfile.NamedTemporaryFile(dir=directory, delete=False)
        size = 0
        try:
            with temp:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 20 * 1024**3:
                        raise HTTPException(413, "迁移包超过 20 GB，请使用命令行恢复")
                    temp.write(chunk)
            manifest = await asyncio.to_thread(m.inspect_bundle, Path(temp.name), migration.root)
            m.check_compatibility(manifest, service.settings)
            Path(temp.name).replace(destination)
            return {"upload_id": identity, "counts": manifest["counts"], "embedding": manifest["embedding"],
                    "includes_api_keys": manifest["includes_api_keys"], "created_at": manifest["created_at"]}
        except (m.MigrationError, ValueError, KeyError) as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            Path(temp.name).unlink(missing_ok=True)

    @app.post("/api/migration/restore")
    def restore(body: RestoreInput, request: Request):
        migration.authorize(request)
        package = migration.directory / "uploads" / (str(body.upload_id) + ".tar.gz")
        if not package.is_file():
            raise HTTPException(404, "请先上传并校验迁移包")
        with migration.quiet():
            # The CLI checks the package again before changing any destination files.
            m.write_json(migration.directory / "pending.json", {"upload_id": str(body.upload_id), "sha256": m.digest(package),
                         "keep_local_identities": body.keep_local_identities})
        return {"pending_restart": True, "message": "恢复已准备；服务暂停写入。请停止并重新启动，届时自动备份并恢复。"}

    @app.post("/api/migration/cancel")
    def cancel(request: Request):
        migration.authorize(request)
        (migration.directory / "pending.json").unlink(missing_ok=True)
        return {"pending_restart": False}
