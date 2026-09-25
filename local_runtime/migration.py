"""Verified whole-system migration. No model calls; credentials excluded by default."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import uuid

import portalocker

FORMAT = "knowin-memory-migration"
VERSION = 1
MAX_BYTES = 50 * 1024**3
MAX_FILES = 100000
ROOT = Path(__file__).resolve().parent.parent
IDENTITY_TABLES = ("app_users", "app_families", "app_members", "app_devices")


class MigrationError(ValueError):
    pass


def disable_optional_telemetry():
    # qdrant-client may import fastembed/ONNX even though migration never runs it.
    # Disable its native telemetry worker (which can race process exit on macOS).
    ort = sys.modules.get("onnxruntime")
    if ort is not None:
        ort.disable_telemetry_events()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def workdir(root):
    path = Path(root) / "migrations"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def token(root):
    path = Path(root) / ".migration-key"
    try:
        with open(path, "x", opener=lambda p, f: os.open(p, f, 0o600)) as stream:
            stream.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    value = path.read_text().strip()
    if len(value) < 32:
        raise MigrationError("迁移管理码文件不完整，请检查本机 .migration-key")
    return value


@contextmanager
def offline(root):
    """Also detects older launchers that only hold Qdrant's process lock."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    handles = []
    try:
        paths = [root / ".runtime.lock"]
        if (root / "data/qdrant/.lock").exists():
            paths.append(root / "data/qdrant/.lock")
        for path in paths:
            handle = path.open("a+")
            handles.append(handle)
            try:
                portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
            except portalocker.exceptions.LockException:
                raise MigrationError("服务仍在运行，请先停止服务再执行迁移/恢复") from None
            if path.name == ".lock":
                # This is only a probe; Qdrant acquires this lock itself on export.
                portalocker.unlock(handle)
                handle.close()
                handles.pop()
        yield
    finally:
        for handle in reversed(handles):
            portalocker.unlock(handle)
            handle.close()


def embedding_identity(settings):
    return {"provider": settings.embedding.provider, "base_url": settings.embedding.base_url,
            "model": settings.embedding.model, "dimensions": settings.embedding_dims,
            "collection": settings.collection}


def safe_name(name):
    path = PurePosixPath(name)
    if (not name or "\\" in name or "\x00" in name or path.is_absolute()
            or any(part in {".", ".."} for part in name.split("/")) or str(path) != name):
        raise MigrationError("迁移包包含不安全的路径")
    return path


def permitted(name):
    path = safe_name(name)
    return (name == "manifest.json" or name == "vectors.jsonl" or name.startswith("materials/")
            or name.startswith("data/materials/") or name in {"data/dashboard/chat.sqlite", "data/dashboard/jobs.sqlite"}
            or (len(path.parts) == 2 and path.parts[0] == "data" and re.fullmatch(r"[A-Za-z0-9_-]+_history\.db", path.name))
            or (len(path.parts) == 2 and path.parts[0] == "config" and re.fullmatch(r"[A-Za-z0-9_-]+\.env", path.name)))


def check_sqlite(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise MigrationError("SQLite 完整性校验失败")
        if db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view') OR lower(sql) LIKE '%virtual table%'").fetchone():
            raise MigrationError("不支持包含触发器、视图或虚拟表的迁移数据库")


def backup_sqlite(source, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as old, closing(sqlite3.connect(dest)) as new, new:
        old.backup(new)
        new.execute("PRAGMA journal_mode=DELETE")
        if new.execute("SELECT 1 FROM sqlite_master WHERE name='app_logins'").fetchone():
            new.execute("DELETE FROM app_logins")
            new.commit()
            new.execute("VACUUM")
    check_sqlite(dest)


def copy_files(source, dest):
    if not source.exists():
        return
    if source.is_symlink():
        raise MigrationError("迁移目录不能是符号链接")
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise MigrationError("迁移目录包含符号链接，请先将附件复制为普通文件")
        if path.is_file() and path.name != ".DS_Store":
            target = dest / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


def counts(stage):
    result = {"memories": 0, "users": 0, "sessions": 0, "turns": 0, "diary_entries": 0, "history": 0}
    users = set()
    with (stage / "vectors.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            result["memories"] += 1
            raw = (row["point"].get("payload") or {}).get("user_id")
            if raw:
                users.add(raw)
    chat = stage / "data/dashboard/chat.sqlite"
    if chat.exists():
        with closing(sqlite3.connect(chat)) as db, db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "app_users" in tables:
                users = {r[0] for r in db.execute("SELECT user_id FROM app_users")}
            if "chat_sessions" in tables:
                users.update(r[0] for r in db.execute("SELECT DISTINCT user_id FROM chat_sessions"))
            if "chat_memory_scopes" in tables:
                users.update(r[0] for r in db.execute("SELECT DISTINCT user_id FROM chat_memory_scopes"))
            for key, table in [("sessions", "chat_sessions"), ("turns", "chat_turns"), ("diary_entries", "daily_diary_entries")]:
                if table in tables:
                    result[key] = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    result["users"] = len(users)
    for path in (stage / "data").glob("*_history.db"):
        with closing(sqlite3.connect(path)) as db, db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='history'").fetchone():
                result["history"] += db.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    result["source_files"] = sum(p.is_file() for p in (stage / "materials").rglob("*"))
    return result


def validate_collections(manifest):
    collections = manifest.get("collections", [])
    active = [s for s in collections if s.get("name") == manifest["embedding"]["collection"]]
    if len(active) != 1:
        raise MigrationError("迁移包缺少正在使用的向量集合或集合重复")
    vectors = active[0]["vectors"]
    configs = [vectors] if "size" in vectors else list(vectors.values())
    if not configs or any(c.get("size") != manifest["embedding"]["dimensions"] for c in configs):
        raise MigrationError("实际向量集合维度与迁移清单不一致")


def export_bundle(root, settings, output=None, *, client=None, include_secrets=False, materials=None):
    """Caller must hold the offline lock or the live service's mutation/memory locks."""
    from qdrant_client import QdrantClient
    disable_optional_telemetry()
    from .portable_paths import map_metadata
    root = Path(root).resolve()
    dest = Path(output) if output else workdir(root) / ("memory-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8] + ".tar.gz")
    if dest.exists():
        raise MigrationError("导出文件已存在，请使用新的文件名")
    dest.parent.mkdir(parents=True, exist_ok=True)
    owned = client is None
    # Opening Qdrant loads trusted local storage only. Packages never carry pickle databases.
    if owned:
        client = QdrantClient(path=str(settings.data_dir / "qdrant"))
    try:
        with tempfile.TemporaryDirectory(prefix="export-", dir=workdir(root)) as directory:
            stage = Path(directory)
            (stage / "data").mkdir()
            for path in [settings.data_dir / "dashboard/chat.sqlite", settings.data_dir / "dashboard/jobs.sqlite", *settings.data_dir.glob("*_history.db")]:
                if path.exists():
                    backup_sqlite(path, stage / "data" / path.relative_to(settings.data_dir))
            copy_files(Path(materials) if materials else root / "materials", stage / "materials")
            copy_files(settings.data_dir / "materials", stage / "data/materials")
            collections = []
            with (stage / "vectors.jsonl").open("w") as stream:
                for item in client.get_collections().collections:
                    params = client.get_collection(item.name).config.params
                    spec = params.model_dump(mode="json")
                    collections.append({"name": item.name, "vectors": spec["vectors"], "sparse_vectors": spec.get("sparse_vectors")})
                    offset = None
                    while True:
                        points, offset = client.scroll(item.name, offset=offset, limit=128, with_payload=True, with_vectors=True)
                        for point in points:
                            body = point.model_dump(mode="json", include={"id", "payload", "vector"})
                            body["payload"] = map_metadata(body.get("payload"), store=True)
                            stream.write(json.dumps({"collection": item.name, "point": body}, ensure_ascii=False, allow_nan=False) + "\n")
                        if offset is None:
                            break
            from dotenv import dotenv_values
            for path in (root / "config").glob("*.env"):
                if path.is_symlink():
                    raise MigrationError("配置不能是符号链接")
                values = dotenv_values(path, interpolate=False)
                safe = {k: v or "" for k, v in values.items() if k.startswith("MEM0_") and k not in {"MEM0_DATA_DIR", "MEM0_DIR", "MEM0_PORTABLE_ROOT"}}
                if not include_secrets:
                    safe = {k: "" if any(word in k for word in ("KEY", "TOKEN", "PASSWORD", "SECRET")) else v for k, v in safe.items()}
                target = stage / "config" / path.name
                target.parent.mkdir(exist_ok=True)
                target.write_text("".join(k + "='" + v.replace("\\", "\\\\").replace("'", "\\'") + "'\n" for k, v in safe.items()))
            files = {}
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    name = path.relative_to(stage).as_posix()
                    if not permitted(name):
                        raise MigrationError("不支持的迁移文件：" + name)
                    files[name] = {"sha256": digest(path), "size": path.stat().st_size}
            manifest = {"format": FORMAT, "version": VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
                        "embedding": embedding_identity(settings), "collections": collections, "counts": counts(stage),
                        "includes_api_keys": include_secrets, "files": files, "source_paths": {
                            "data": str(settings.data_dir), "materials": str(materials or root / "materials")}}
            write_json(stage / "manifest.json", manifest)
            partial = dest.with_name(dest.name + ".partial")
            try:
                with open(partial, "xb", opener=lambda p, f: os.open(p, f, 0o600)) as raw, tarfile.open(fileobj=raw, mode="w:gz", compresslevel=1) as archive:
                    for name in ["manifest.json", *files]:
                        archive.add(stage / name, arcname=name, recursive=False)
                partial.replace(dest)
            finally:
                partial.unlink(missing_ok=True)
            return dest, manifest
    finally:
        if owned:
            client.close()


def extract_bundle(package, stage):
    try:
        return _extract_bundle(package, stage)
    except MigrationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, tarfile.TarError, sqlite3.Error) as exc:
        raise MigrationError("迁移包损坏或结构不完整，尚未替换当前数据") from exc


def _extract_bundle(package, stage):
    """Stream extraction with strict paths, sizes, types, duplicates and per-file hashes."""
    total, seen = 0, set()
    with tarfile.open(package, "r|gz") as archive:
        for member in archive:
            if not member.isfile() or not permitted(member.name) or member.name in seen:
                raise MigrationError("迁移包包含重复路径、链接或不支持的文件")
            total += member.size
            if len(seen) >= MAX_FILES or total > MAX_BYTES or member.size < 0:
                raise MigrationError("迁移包超过文件数量或解压容量上限")
            if member.name == "manifest.json" and member.size > 32 * 1024**2:
                raise MigrationError("迁移清单过大")
            seen.add(member.name)
            path = stage / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as src, path.open("xb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
    manifest = json.loads((stage / "manifest.json").read_text())
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise MigrationError("迁移包格式/版本不支持")
    files = manifest.get("files", {})
    if set(files) | {"manifest.json"} != seen or "vectors.jsonl" not in files:
        raise MigrationError("迁移包文件清单不完整")
    for name, info in files.items():
        if not permitted(name) or name == "manifest.json":
            raise MigrationError("迁移清单包含不支持的路径")
        path = stage / name
        if path.stat().st_size != info["size"] or digest(path) != info["sha256"]:
            raise MigrationError("迁移包校验失败：" + name)
        if name.endswith((".sqlite", ".db")) and not name.startswith(("materials/", "data/materials/")):
            check_sqlite(path)
    if counts(stage) != manifest.get("counts"):
        raise MigrationError("迁移包记录数量与清单不一致")
    validate_collections(manifest)
    return manifest


def inspect_bundle(package, root=ROOT):
    with tempfile.TemporaryDirectory(prefix="inspect-", dir=workdir(root)) as directory:
        return extract_bundle(package, Path(directory))


def check_compatibility(manifest, settings):
    if manifest.get("embedding") != embedding_identity(settings):
        raise MigrationError("Embedding 平台、地址、模型、维度或集合不一致；请先使用原配置，不能直接混用向量")


def preserve_identities(current, incoming):
    """Only merge account relationships into a replacement; never merge fact databases."""
    from .access import Access
    if not current.exists():
        return
    Access(incoming)
    with closing(sqlite3.connect(current)) as old, closing(sqlite3.connect(incoming)) as new, new:
        old.row_factory = sqlite3.Row
        for table in IDENTITY_TABLES:
            if not old.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                continue
            for row in old.execute(f"SELECT * FROM {table}"):
                keys = list(row.keys())
                primary = [c[1] for c in new.execute(f"PRAGMA table_info({table})") if c[5]]
                prior = new.execute(f"SELECT * FROM {table} WHERE " + " AND ".join(k + "=?" for k in primary), tuple(row[k] for k in primary)).fetchone()
                if prior and table in {"app_families", "app_devices"} and tuple(row) != prior:
                    raise MigrationError("家庭或设备 ID 冲突，不能自动合并：" + table)
                new.execute(f"INSERT OR IGNORE INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(row))


def prepare_restore(stage, manifest, root, keep_local_identities):
    from qdrant_client import QdrantClient, models
    disable_optional_telemetry()
    from .portable_paths import PATH_KEYS, LEGACY_DATA, LEGACY_MATERIALS
    from .access import Access
    for name in ("data", "materials", "config"):
        (stage / name).mkdir(parents=True, exist_ok=True)
    # Existing machine credentials/config are kept. On a clean machine use sanitized profiles.
    copy_files(root / "config", stage / "config")
    def canonical(value, key=None):
        if isinstance(value, dict):
            return {k: canonical(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [canonical(v, key) for v in value]
        if key in PATH_KEYS and isinstance(value, str):
            for name, dest in (("data", LEGACY_DATA), ("materials", LEGACY_MATERIALS)):
                old = manifest.get("source_paths", {}).get(name)
                if old:
                    try:
                        relative = Path(value).relative_to(old)
                    except ValueError:
                        continue
                    if ".." in relative.parts:
                        raise MigrationError("附件路径越界")
                    return str(dest / relative)
        return value
    client = QdrantClient(path=str(stage / "data/qdrant"))
    try:
        names = set()
        for spec in manifest["collections"]:
            name = spec["name"]
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", name) or name in names:
                raise MigrationError("向量集合名称无效或重复")
            names.add(name)
            config = spec["vectors"]
            vectors = models.VectorParams.model_validate(config) if "size" in config else {k: models.VectorParams.model_validate(v) for k, v in config.items()}
            sparse = {k: models.SparseVectorParams.model_validate(v) for k, v in (spec.get("sparse_vectors") or {}).items()} or None
            client.create_collection(name, vectors_config=vectors, sparse_vectors_config=sparse)
        seen = set()
        with (stage / "vectors.jsonl").open() as stream:
            for line in stream:
                record = json.loads(line)
                name, point = record["collection"], record["point"]
                identity = (name, str(point["id"]))
                if name not in names or identity in seen:
                    raise MigrationError("记忆 ID 重复或集合不存在")
                seen.add(identity)
                point["payload"] = canonical(point.get("payload"))
                client.upsert(name, [models.PointStruct.model_validate(point)])
        if sum(client.count(name).count for name in names) != manifest["counts"]["memories"]:
            raise MigrationError("向量写入数量校验失败")
    finally:
        client.close()
    if manifest["embedding"]["collection"] not in names:
        raise MigrationError("迁移包缺少正在使用的向量集合")
    # Normalize attachment paths inside caches and import jobs too.
    for path in (stage / "data/materials").rglob("*.json"):
        write_json(path, canonical(json.loads(path.read_text())))
    jobs = stage / "data/dashboard/jobs.sqlite"
    if jobs.exists():
        with closing(sqlite3.connect(jobs)) as db, db:
            for identity, body in db.execute("SELECT id,body FROM jobs").fetchall():
                db.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(canonical(json.loads(body)), ensure_ascii=False), identity))
    chat = stage / "data/dashboard/chat.sqlite"
    chat.parent.mkdir(parents=True, exist_ok=True)
    Access(chat)
    if keep_local_identities:
        preserve_identities(root / "data/dashboard/chat.sqlite", chat)
    with closing(sqlite3.connect(chat)) as db, db:
        db.execute("DELETE FROM app_logins")
        # Only pre-identity packages need migration on the first new-version startup.
        if not db.execute("SELECT 1 FROM app_users LIMIT 1").fetchone() or keep_local_identities:
            db.execute("DELETE FROM app_migrations WHERE name='identities-v1'")
    for path in (stage / "config").glob("*.env"):
        path.chmod(0o600)


def recover_restore(root):
    """Rollback an interrupted directory switch before opening any database."""
    root = Path(root)
    journal = workdir(root) / "restore-journal.json"
    if not journal.exists():
        return
    info = json.loads(journal.read_text())
    if info["phase"] != "committed":
        backup, stage = root / info["backup"], root / info["stage"]
        for name, existed in info["existed"].items():
            target, old = root / name, backup / name
            if old.exists():
                if target.exists():
                    target.rename(backup / ("failed-" + name))
                old.rename(target)
            elif not existed and not (stage / name).exists() and target.exists():
                target.rename(backup / ("failed-" + name))
    journal.unlink()


def restore_bundle(package, settings, root=ROOT, *, keep_local_identities=False):
    """Offline only. Stage and validate everything before touching destination data."""
    root = Path(root).resolve()
    recover_restore(root)
    with tempfile.TemporaryDirectory(prefix="restore-", dir=workdir(root)) as directory:
        stage = Path(directory)
        manifest = extract_bundle(package, stage)
        check_compatibility(manifest, settings)
        prepare_restore(stage, manifest, root, keep_local_identities)
        backup = workdir(root) / "backups" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        backup.mkdir(parents=True, mode=0o700)
        journal = workdir(root) / "restore-journal.json"
        info = {"phase": "applying", "backup": str(backup.relative_to(root)), "stage": str(stage.relative_to(root)),
                "existed": {name: (root / name).exists() for name in ("data", "materials", "config")}}
        write_json(journal, info)
        try:
            for name in info["existed"]:
                if (root / name).exists():
                    (root / name).rename(backup / name)
                (stage / name).rename(root / name)
            info["phase"] = "committed"
            write_json(journal, info)
        except BaseException:
            recover_restore(root)
            raise
        journal.unlink()
        return {"restored": True, "counts": manifest["counts"], "backup": str(backup)}


def apply_pending(root, settings):
    pending = workdir(root) / "pending.json"
    if not pending.exists():
        return None
    info = json.loads(pending.read_text())
    package = workdir(root) / "uploads" / (info["upload_id"] + ".tar.gz")
    if digest(package) != info["sha256"]:
        raise MigrationError("待恢复迁移包已变化，拒绝恢复")
    result = restore_bundle(package, settings, root, keep_local_identities=info.get("keep_local_identities", False))
    pending.unlink()
    write_json(workdir(root) / "last-restore.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--profile", choices=("qwen", "openai", "local", "ollama"), default="qwen")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("token", help="显示本机整库迁移管理码")
    sub.add_parser("cancel", help="停服后取消待恢复任务，不替换当前数据")
    exp = sub.add_parser("export", help="停服后导出完整迁移包")
    exp.add_argument("--output", type=Path)
    exp.add_argument("--include-secrets", action="store_true")
    read = sub.add_parser("inspect", help="校验包并显示数量，不修改数据")
    read.add_argument("package", type=Path)
    restore = sub.add_parser("restore", help="停服后校验、备份并恢复")
    restore.add_argument("package", type=Path)
    restore.add_argument("--keep-local-identities", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "token":
            print(token(root))
            return
        if args.command == "inspect":
            result = inspect_bundle(args.package, root)
            print(json.dumps({k: result[k] for k in ("format", "version", "created_at", "counts", "embedding", "includes_api_keys")}, ensure_ascii=False, indent=2))
            return
        if args.command == "cancel":
            with offline(root):
                recover_restore(root)
                (workdir(root) / "pending.json").unlink(missing_ok=True)
            print("已取消待恢复任务，当前数据未替换")
            return
        from .runtime import load_settings
        os.environ["MEM0_PORTABLE_ROOT"] = str(root)
        os.environ["MEM0_DATA_DIR"] = str(root / "data")
        profile = root / "config" / (args.profile + ".env")
        settings = load_settings(profile if profile.exists() else ROOT / "config" / (args.profile + ".env.example"))
        with offline(root):
            recover_restore(root)
            if args.command == "export":
                # Qdrant manages its own lock when the exporter opens the client.
                result, manifest = export_bundle(root, settings, args.output, include_secrets=args.include_secrets)
                result = {"package": str(result), "counts": manifest["counts"], "includes_api_keys": args.include_secrets}
            else:
                result = restore_bundle(args.package, settings, root, keep_local_identities=args.keep_local_identities)
                (workdir(root) / "pending.json").unlink(missing_ok=True)
                write_json(workdir(root) / "last-restore.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (MigrationError, OSError, ValueError, tarfile.TarError, KeyError, sqlite3.Error) as exc:
        parser.exit(1, "迁移失败：" + str(exc) + "\n")


if __name__ == "__main__":
    # This CLI transfers existing vectors only. Qdrant's optional inference
    # imports otherwise start an ONNX native telemetry thread that can abort
    # interpreter shutdown on macOS. Do not change the running web process.
    sys.modules.setdefault("fastembed", None)
    main()
