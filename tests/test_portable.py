import json
import shutil
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from qdrant_client import models

from local_runtime.dashboard import create_app
from local_runtime.materials import chunk_records, existing_chunks, write_json
from local_runtime.portable_paths import LEGACY_DATA, LEGACY_MATERIALS, map_metadata, map_path
from local_runtime.test_dashboard import TestMemory


def test_storage_identity_survives_move(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    units = [{"text": "测试资料", "metadata": {"preview_file": str(old / "data/materials/a.png")}}]
    monkeypatch.setenv("MEM0_PORTABLE_ROOT", str(old))
    old_record = list(chunk_records(old / "materials/a.txt", old / "materials", "sha", units, "qwen-vl-plus"))[0]
    cache = old / "data/materials/parsed/unit.json"
    write_json(cache, {"units": units})
    shutil.move(old, new)
    monkeypatch.setenv("MEM0_PORTABLE_ROOT", str(new))
    loaded = map_metadata(json.loads((new / "data/materials/parsed/unit.json").read_text()))["units"]
    new_record = list(chunk_records(new / "materials/a.txt", new / "materials", "sha", loaded, "qwen-vl-plus"))[0]
    assert new_record == old_record
    assert loaded[0]["metadata"]["preview_file"] == str(new / "data/materials/a.png")
    assert map_metadata({"text": str(LEGACY_MATERIALS)})["text"] == str(LEGACY_MATERIALS)


def test_legacy_previews_and_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM0_PORTABLE_ROOT", str(tmp_path))
    root = tmp_path / "materials"
    root.mkdir()
    (root / "a.pdf").write_bytes(b"%PDF-test")
    cache = tmp_path / "data/materials"
    cache.mkdir(parents=True)
    (cache / "preview.png").write_bytes(b"preview")
    settings = SimpleNamespace(
        data_dir=tmp_path / "data",
        collection="test",
        embedding_dims=3,
        embedding=SimpleNamespace(model="fake"),
        llm=SimpleNamespace(model="fake", base_url="http://unused", api_key="unused"),
    )
    app = create_app(settings, root, memory_factory=TestMemory)
    with TestClient(app) as http:
        service = app.state.service
        identity = str(uuid.uuid4())
        service.memory.client.upsert(
            "test",
            points=[
                models.PointStruct(
                    id=identity,
                    vector=[1.0, 0.0, 0.0],
                    payload={
                        "data": "legacy",
                        "user_id": "alice",
                        "source_root": str(LEGACY_MATERIALS),
                        "source_path": str(LEGACY_MATERIALS / "a.pdf"),
                        "source_file": "a.pdf",
                        "preview_file": str(LEGACY_DATA / "materials/preview.png"),
                        "ingest_key": "old-key",
                    },
                )
            ],
        )
        service.refresh()
        assert service.resolve_file(identity, "source", 0) == root / "a.pdf"
        assert service.resolve_file(identity, "preview", 0) == cache / "preview.png"
        assert existing_chunks(service.memory, "alice", root)["old-key"] == identity
        assert http.get("/api/overview").status_code == 200
        assert http.get("/chat").status_code == 200


def test_reject_path_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("MEM0_PORTABLE_ROOT", str(tmp_path))
    with pytest.raises(ValueError):
        map_path(str(LEGACY_MATERIALS / "../../secret"))
    (tmp_path / "materials").mkdir()
    (tmp_path / "materials/link").symlink_to(tmp_path)
    with pytest.raises(ValueError):
        map_path(str(LEGACY_MATERIALS / "link/secret"))
