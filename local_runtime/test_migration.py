import io
import json
from pathlib import Path
import sqlite3
import tarfile
from types import SimpleNamespace
import uuid

import pytest
from qdrant_client import QdrantClient, models

from . import migration as m
from .access import Access
from .chat import Chat
from .portable_paths import LEGACY_MATERIALS
from .test_dashboard import HEADERS, login

pytest_plugins = ("local_runtime.test_dashboard",)


def settings(root):
    return SimpleNamespace(data_dir=root / "data", collection="test", embedding_dims=3,
                           embedding=SimpleNamespace(provider="openai", base_url="http://fake/v1", model="fake"))


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "old"
    (root / "data/dashboard").mkdir(parents=True)
    (root / "materials/中文").mkdir(parents=True)
    (root / "materials/中文/a.txt").write_text("原始资料")
    (root / "config").mkdir()
    (root / "config/qwen.env").write_text("MEM0_PROVIDER_API_KEY='private-test-key'\nMEM0_PRESET=qwen\n")
    with sqlite3.connect(root / "data/dashboard/chat.sqlite") as db:
        db.executescript("""CREATE TABLE chat_sessions(id TEXT PRIMARY KEY,user_id TEXT,title TEXT,use_library INTEGER,created_at TEXT,updated_at TEXT);
            CREATE TABLE chat_turns(id TEXT PRIMARY KEY,session_id TEXT,request_id TEXT UNIQUE,user_text TEXT,answer TEXT,status TEXT,events TEXT,protocol TEXT,error TEXT,created_at TEXT);""")
        for i, user in enumerate(("alice", "knowin_public")):
            db.execute("INSERT INTO chat_sessions VALUES (?,?,?,?,?,?)", (f"s{i}", user, "旧对话", 1, "2026-09-22T01:00:00+00:00", "2026-09-22T01:00:00+00:00"))
            db.execute("INSERT INTO chat_turns VALUES (?,?,?,?,?,?,?,?,?,?)", (f"t{i}", f"s{i}", f"r{i}", "问题", "回答", "complete", "[]", "[]", None, "2026-09-22T01:00:00+00:00"))
    with sqlite3.connect(root / "data/test_history.db") as db:
        db.execute("CREATE TABLE history(memory_id TEXT,old_memory TEXT,new_memory TEXT)")
        db.execute("INSERT INTO history VALUES ('old-id','','原始记忆')")
        db.execute("CREATE TABLE messages(id TEXT,body TEXT)")
        db.execute("INSERT INTO messages VALUES ('old-message','旧消息')")
    client = QdrantClient(path=str(root / "data/qdrant"))
    client.create_collection("test", vectors_config={"dense": models.VectorParams(size=3, distance="Cosine")}, sparse_vectors_config={"bm25": models.SparseVectorParams()})
    point_id = str(uuid.uuid4())
    client.upsert("test", [models.PointStruct(id=point_id, vector={"dense": [1, 0, 0], "bm25": models.SparseVector(indices=[3], values=[0.5])},
                        payload={"data": "已保存的事实", "user_id": "alice", "source_path": str(root / "materials/中文/a.txt"), "created_at": "2026-09-22"})])
    client.close()
    with m.offline(root):
        package, manifest = m.export_bundle(root, settings(root))
    return root, package, manifest, point_id


def test_real_round_trip_preserves_vectors_ids_history_files_and_accounts(source, tmp_path):
    old, package, manifest, point_id = source
    assert manifest["counts"]["memories"] == 1
    assert manifest["counts"]["turns"] == 2
    target = tmp_path / "new"
    (target / "data/dashboard").mkdir(parents=True)
    access = Access(target / "data/dashboard/chat.sqlite")
    access.register("chat_default", "本地名称")
    access.create_family("chat_default", "home", "保留家庭")
    access.create_device("chat_default", "home", "robot", "保留设备")
    (target / "config").mkdir()
    (target / "config/qwen.env").write_text("MEM0_PROVIDER_API_KEY='target-key'\n")
    (target / "config/qwen.env.example").write_text("MEM0_PROVIDER_API_KEY=\n")
    with m.offline(target):
        result = m.restore_bundle(package, settings(target), target, keep_local_identities=True)
    assert Path(result["backup"], "data/dashboard/chat.sqlite").exists()
    assert (target / "materials/中文/a.txt").read_text() == "原始资料"
    assert "target-key" in (target / "config/qwen.env").read_text()
    assert (target / "config/qwen.env.example").read_text() == "MEM0_PROVIDER_API_KEY=\n"
    client = QdrantClient(path=str(target / "data/qdrant"))
    row = client.retrieve("test", [point_id], with_vectors=True)[0]
    assert row.vector["dense"] == [1, 0, 0]
    assert row.vector["bm25"].indices == [3]
    assert row.payload["source_path"] == str(LEGACY_MATERIALS / "中文/a.txt")
    client.close()
    with sqlite3.connect(target / "data/test_history.db") as db:
        assert db.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1
        assert db.execute("SELECT body FROM messages").fetchone()[0] == "旧消息"
    # Exercise the actual legacy schema/backfill path without any LLM calls.
    chat = Chat(SimpleNamespace(directory=target / "data/dashboard", settings=SimpleNamespace(diary_auto_summary=False)))
    access = Access(chat.db_path)
    access.bootstrap([])
    assert {u["user_id"] for u in access.users()} == {"alice", "chat_default", "knowin_public"}
    assert access.profile("knowin_public")["read_only"]
    assert access.profile("chat_default")["devices"][0]["device_id"] == "robot"
    assert len(chat.detail("s1")["turns"]) == 1
    with chat.db() as db:
        assert db.execute("SELECT COUNT(*) FROM daily_diary_entries").fetchone()[0] == 1  # public legacy chats are not private diaries
    chat.close()


def test_keys_and_login_tokens_not_exported_and_format_is_logical(source, tmp_path):
    root, _, _, _ = source
    access = Access(root / "data/dashboard/chat.sqlite")
    access.register("alice", "Alice")
    secret = access.login("alice")
    package, _ = m.export_bundle(root, settings(root))
    stage = tmp_path / "unpacked"
    stage.mkdir()
    m.extract_bundle(package, stage)
    assert "private-test-key" not in (stage / "config/qwen.env").read_text()
    assert not (stage / "data/qdrant").exists()  # no untrusted pickle import
    assert secret not in (stage / "data/dashboard/chat.sqlite").read_bytes().decode(errors="ignore")
    with sqlite3.connect(stage / "data/dashboard/chat.sqlite") as db:
        assert db.execute("SELECT COUNT(*) FROM app_logins").fetchone()[0] == 0
    keyed, _ = m.export_bundle(root, settings(root), include_secrets=True)
    keyed_stage = tmp_path / "keyed"
    keyed_stage.mkdir()
    m.extract_bundle(keyed, keyed_stage)
    assert "private-test-key" in (keyed_stage / "config/qwen.env").read_text()


@pytest.mark.parametrize("name,kind", [("../../escape", "file"), ("materials/link", "link"), ("data/qdrant/storage.sqlite", "file")])
def test_unsafe_archives_rejected(tmp_path, name, kind):
    package = tmp_path / "bad.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        info = tarfile.TarInfo(name)
        if kind == "link":
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            archive.addfile(info)
        else:
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(m.MigrationError):
        m.inspect_bundle(package, tmp_path)
    assert not (tmp_path.parent / "escape").exists()


def test_tamper_and_model_mismatch_do_not_change_destination(source, tmp_path):
    _, package, _, _ = source
    target = tmp_path / "target"
    (target / "data").mkdir(parents=True)
    marker = target / "data/keep"
    marker.write_text("keep")
    wrong = settings(target)
    wrong.embedding_dims = 1024
    with pytest.raises(m.MigrationError, match="Embedding"):
        m.restore_bundle(package, wrong, target)
    assert marker.read_text() == "keep"
    damaged = tmp_path / "tampered.tar.gz"
    with tarfile.open(package) as source_tar, tarfile.open(damaged, "w:gz") as archive:
        for entry in source_tar:
            body = source_tar.extractfile(entry).read()
            if entry.name == "materials/中文/a.txt":
                body = b"tampered"
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
    with pytest.raises(m.MigrationError, match="校验失败"):
        m.restore_bundle(damaged, settings(target), target)
    assert marker.read_text() == "keep"


def test_partial_directory_switch_rolls_back(source, tmp_path, monkeypatch):
    _, package, _, _ = source
    target = tmp_path / "target"
    for name in ("data", "materials", "config"):
        (target / name).mkdir(parents=True)
        (target / name / "marker").write_text(name)
    original = Path.rename
    failed = False
    def fail_once(self, dest):
        nonlocal failed
        if not failed and self.name == "materials" and self.parent.name.startswith("restore-"):
            failed = True
            raise OSError("simulated disk failure")
        return original(self, dest)
    monkeypatch.setattr(Path, "rename", fail_once)
    with pytest.raises(OSError, match="simulated"):
        m.restore_bundle(package, settings(target), target)
    for name in ("data", "materials", "config"):
        assert (target / name / "marker").read_text() == name
    assert not (target / "migrations/restore-journal.json").exists()


def test_offline_cli_rejects_live_qdrant(source):
    root, _, _, _ = source
    client = QdrantClient(path=str(root / "data/qdrant"))
    try:
        with pytest.raises(m.MigrationError, match="仍在运行"):
            with m.offline(root):
                pytest.fail("must not enter")
    finally:
        client.close()


def test_web_admin_gate_export_import_preview_pending_and_cancel(console):
    client, service = console
    service.settings.embedding.provider = "openai"
    service.settings.embedding.base_url = "http://fake/v1"
    assert client.get('/api/migration/status').status_code == 403
    assert client.post('/api/migration/export',headers=HEADERS,json={}).status_code == 403
    headers = {**HEADERS, "X-Memory-Migration-Key": service.migration.key}
    exported = client.post('/api/migration/export',headers=headers,json={})
    assert exported.status_code == 200, exported.text
    link = exported.json()['download_url']
    package = client.get(link).content
    login(client, 'bob')
    assert client.get(link).status_code == 404
    login(client, 'alice')
    preview = client.post('/api/migration/imports', headers=headers,content=package)
    assert preview.status_code == 200, preview.text
    assert preview.json()['counts']['memories'] == 25
    queued = client.post('/api/migration/restore',headers=headers,json={'upload_id':preview.json()['upload_id']})
    assert queued.status_code == 200, queued.text
    assert client.post('/api/manage/notes',headers=HEADERS,json={'text':'cannot write'}).status_code == 409
    assert client.get('/api/manage/memories').status_code == 200
    assert client.post('/api/migration/cancel',headers=headers).status_code == 200
    assert not service.migration.pending


def test_pending_archive_change_rejected(source, tmp_path):
    _, package, _, _ = source
    target = tmp_path / "target"
    incoming = m.workdir(target) / "uploads"
    incoming.mkdir()
    identity = str(uuid.uuid4())
    archive = incoming / (identity + '.tar.gz')
    archive.write_bytes(package.read_bytes())
    m.write_json(target / 'migrations/pending.json',{'upload_id':identity,'sha256':'wrong'})
    with pytest.raises(m.MigrationError,match='已变化'):
        m.apply_pending(target,settings(target))
    assert not (target / 'data').exists()


def test_pending_restore_commits_backup_and_revokes_old_login(source, tmp_path):
    _, package, _, _ = source
    target = tmp_path / 'pending-target'
    uploads = m.workdir(target) / 'uploads'
    uploads.mkdir()
    identity = str(uuid.uuid4())
    incoming = uploads / (identity + '.tar.gz')
    incoming.write_bytes(package.read_bytes())
    m.write_json(target / 'migrations/pending.json',{'upload_id':identity,'sha256':m.digest(incoming)})
    with m.offline(target):
        result = m.apply_pending(target,settings(target))
    assert result['restored']
    assert not (target / 'migrations/pending.json').exists()
    assert json.loads((target / 'migrations/last-restore.json').read_text())['counts']['turns'] == 2
    assert (target / 'materials/中文/a.txt').read_text() == '原始资料'


def test_actual_vector_dimension_must_match_manifest(source, tmp_path):
    _, package, _, _ = source
    damaged = tmp_path / 'wrong-dimension.tar.gz'
    with tarfile.open(package) as source_tar, tarfile.open(damaged,'w:gz') as archive:
        for entry in source_tar:
            body = source_tar.extractfile(entry).read()
            if entry.name == 'manifest.json':
                manifest = json.loads(body)
                manifest['collections'][0]['vectors']['dense']['size'] = 4
                body = json.dumps(manifest).encode()
            entry.size = len(body)
            archive.addfile(entry,io.BytesIO(body))
    with pytest.raises(m.MigrationError,match='实际向量集合维度'):
        m.inspect_bundle(damaged,tmp_path)
