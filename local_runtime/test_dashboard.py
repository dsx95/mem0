import hashlib
import time
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient, models

from local_runtime.dashboard import create_app
from local_runtime.materials import file_hash
from local_runtime.web_parsing import WebParser

HEADERS = {"X-Memory-Client": "dashboard"}


class TestMemory:
    __test__ = False

    def __init__(self, settings):
        self.client = QdrantClient(":memory:")
        self.client.create_collection(
            settings.collection, vectors_config=models.VectorParams(size=3, distance="Cosine")
        )
        self.vector_store = SimpleNamespace(client=self.client, collection_name=settings.collection)
        self.embedding_model = SimpleNamespace(embed=lambda *args: [1.0, 0.0, 0.0])
        self.settings, self.add_calls, self.closed = settings, 0, False
        for i in range(25):
            self.client.upsert(
                settings.collection,
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector=[1.0, 0.0, 0.0],
                        payload={
                            "data": f"已有记忆 {i}",
                            "user_id": "alice" if i % 2 else "bob",
                            "created_at": "2026-09-18",
                        },
                    )
                ],
            )

    def add(self, text, user_id, infer, metadata):
        assert infer is False
        self.add_calls += 1
        memory_id = str(uuid.uuid4())
        self.client.upsert(
            self.settings.collection,
            points=[
                models.PointStruct(
                    id=memory_id,
                    vector=[1.0, 0.0, 0.0],
                    payload={**metadata, "data": text, "user_id": user_id},
                )
            ],
        )
        return {"results": [{"id": memory_id, "event": "ADD"}]}

    def get(self, memory_id):
        payload = dict(self.client.retrieve(self.settings.collection, [memory_id], with_payload=True)[0].payload)
        return {"id": memory_id, "memory": payload.pop("data"), "metadata": payload}

    def delete(self, memory_id):
        self.client.delete(self.settings.collection, [memory_id])
        return {"message": "deleted"}

    def history(self, memory_id):
        return [{"event": "ADD", "created_at": "2026-09-18"}]

    def close(self):
        self.closed = True


@pytest.fixture
def settings(tmp_path):
    return SimpleNamespace(
        data_dir=tmp_path / "data",
        collection="test",
        embedding_dims=3,
        embedding=SimpleNamespace(model="fake-embed"),
        llm=SimpleNamespace(model="fake-llm", base_url="http://unused", api_key="never-send-this"),
    )


@pytest.fixture
def console(settings, tmp_path):
    app = create_app(settings, tmp_path / "sources", memory_factory=TestMemory, max_upload_mb=1)
    with TestClient(app) as client:
        login(client, "alice")
        yield client, app.state.service
    assert app.state.service.memory.closed


def login(client, user="alice"):
    if user not in {u["user_id"] for u in client.get("/api/identity/users").json()["items"]}:
        response = client.post("/api/identity/users", headers=HEADERS, json={"user_id": user})
        assert response.status_code == 201, response.text
    response = client.post("/api/identity/select", headers=HEADERS, json={"user_id": user})
    assert response.status_code == 200, response.text
    return response.json()


def finish(client, job_id):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        job = next(j for j in client.get("/api/jobs").json()["items"] if j["id"] == job_id)
        if job["status"] in {"complete", "failed"}:
            return job
        time.sleep(0.02)
    pytest.fail("Import worker did not finish")


def upload(client, name, content=b"New searchable memory", user="alice"):
    login(client, user)
    response = client.post("/api/uploads", params={"filename": name, "user_id": user}, content=content, headers=HEADERS)
    assert response.status_code == 202, response.text
    return finish(client, response.json()["id"])


def test_text_page_preview_uses_rendered_pdf_with_path_boundary(console, tmp_path):
    client, service = console
    rendered = service.settings.data_dir / "materials" / "rendered" / "document.pdf"
    rendered.parent.mkdir(parents=True)
    rendered.write_bytes(b"%PDF-1.4\nrendered document")
    result = service.memory.add("Slogan original text", "knowin_public", False,
                                {"rendered_file": str(rendered), "page_label": "3"})
    identity = result["results"][0]["id"]
    service.refresh()
    response = client.get(f"/api/memories/{identity}/file?asset=preview")
    assert response.status_code == 200 and response.headers["content-type"] == "application/pdf"
    assert response.content == rendered.read_bytes()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"private")
    service.memory.client.set_payload(service.settings.collection, {"rendered_file": str(outside)}, [identity])
    service.refresh()
    assert client.get(f"/api/memories/{identity}/file?asset=preview").status_code == 404


def test_existing_memories_pagination_user_filter_and_semantic_search(console):
    client, service = console
    overview = client.get("/api/overview")
    assert overview.json()["total"] == 12
    assert "never-send-this" not in overview.text
    pages = [client.get("/api/memories", params={"page": n, "page_size": 5}).json()["items"] for n in (1, 2, 3)]
    assert [len(p) for p in pages] == [5, 5, 2]
    assert len({i["id"] for page in pages for i in page}) == 12
    alice = client.get("/api/memories", params={"user_id": "alice"}).json()
    assert alice["total"] == 12 and all(i["user_id"] == "alice" for i in alice["items"])
    results = client.get("/api/search", params={"q": "记忆", "user_id": "alice"}).json()["items"]
    assert len(results) == 12 and all(i["user_id"] == "alice" for i in results)
    assert client.get("/api/search", params={"q": "记忆", "kind": "video"}).json()["items"] == []
    assert service.memory.add_calls == 0


def test_upload_repeat_and_renamed_file_skip_without_model_writes(console):
    client, service = console
    first = upload(client, "示例.txt")
    assert first["status"] == "complete" and first["added"] == 1
    second = upload(client, "示例.txt")
    third = upload(client, "改名后.txt")
    assert second["added"] == third["added"] == 0
    assert second["skipped"] == third["skipped"] == 1
    assert first["path"] == third["path"]
    assert service.memory.add_calls == 1
    assert client.get("/api/overview").json()["total"] == 13
    assert len(client.get("/api/jobs").json()["items"]) == 3
    assert len(client.get("/api/sources").json()["items"]) == 1


def test_changed_bytes_and_different_users_are_separate_versions(console):
    client, service = console
    first = upload(client, "notes.txt", b"one")
    second = upload(client, "notes.txt", b"two")
    third = upload(client, "notes.txt", b"one", "another_user")
    assert all(j["added"] == 1 for j in [first, second, third])
    assert first["sha256"] != second["sha256"]
    assert service.memory.add_calls == 3


def test_failed_parse_has_persisted_error_and_can_retry(console):
    client, service = console
    job = upload(client, "bad.json", b"{not valid json}")
    assert job["status"] == "failed"
    assert job["error"] == "JSONDecodeError"
    assert service.memory.add_calls == 0
    assert client.post(f"/api/jobs/{job['id']}/retry", headers=HEADERS).status_code == 200
    assert finish(client, job["id"])["status"] == "failed"
    assert client.post("/api/jobs/missing/retry", headers=HEADERS).status_code == 404


@pytest.mark.parametrize(
    "name,body,status",
    [
        ("../escape.txt", b"x", 400),
        ("file.exe", b"x", 415),
        ("empty.txt", b"", 400),
        ("big.txt", b"x" * (1024**2 + 1), 413),
    ],
)
def test_upload_validation_cleans_temporary_files(console, name, body, status):
    client, service = console
    reply = client.post("/api/uploads", params={"filename": name}, content=body, headers=HEADERS)
    assert reply.status_code == status
    assert not list(service.directory.glob("*.part"))
    assert service.memory.add_calls == 0


def test_cross_origin_and_dns_rebinding_are_blocked(console):
    client, _ = console
    assert client.get("/api/overview", headers={"Host": "malicious.example"}).status_code == 403
    assert client.post("/api/uploads?filename=a.txt", content=b"x").status_code == 403
    assert (
        client.post(
            "/api/uploads?filename=a.txt", content=b"x", headers={**HEADERS, "Origin": "https://malicious.example"}
        ).status_code
        == 403
    )


def test_file_preview_range_and_path_escape(console, tmp_path):
    client, service = console
    upload(client, "reference.html", b"<p>Stored source</p><script>alert(1)</script>")
    item = next(i for i in service.snapshot() if i["metadata"].get("source_type") == "html")
    response = client.get(f"/api/memories/{item['id']}/file")
    assert response.status_code == 200 and response.headers["content-disposition"].startswith("attachment")
    assert "script" not in item["memory"]
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    item["metadata"]["source_path"] = str(outside)
    assert client.get(f"/api/memories/{item['id']}/file").status_code == 404
    assert client.get("/api/memories/missing").status_code == 404


def test_docx_without_renderer_preserves_text_table_and_no_fake_page(settings, tmp_path, monkeypatch):
    monkeypatch.setattr("local_runtime.web_parsing.shutil.which", lambda _: None)
    source = tmp_path / "demo.docx"
    xml = """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:r><w:t>用户说明</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>房间</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>书房</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>"""
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", xml)
    parser = WebParser(settings, tmp_path / "cache", "unused")
    units = parser.parse(source, file_hash(source))
    assert "用户说明" in units[0]["text"] and "房间 | 书房" in units[0]["text"]
    assert units[0]["metadata"]["page_basis"] == "not_paginated"
    assert "page_start" not in units[0]["metadata"]
    assert parser.parse(source, file_hash(source)) == units


def test_interrupted_job_resumes_from_persisted_queue(settings, tmp_path):
    first = create_app(settings, tmp_path / "sources", memory_factory=TestMemory)
    service = first.state.service
    temp = service.directory / "test.part"
    temp.write_bytes(b"recover after restart")
    job = service.accept(temp, "recover.txt", hashlib.sha256(temp.read_bytes()).hexdigest(), temp.stat().st_size, "alice", {"owner_user_id": "alice"})
    service.save_job(job["id"], status="writing", message="interrupted")
    second = create_app(settings, tmp_path / "sources", memory_factory=TestMemory)
    with TestClient(second) as client:
        login(client, "alice")
        restored = finish(client, job["id"])
        assert restored["status"] == "complete" and restored["added"] == 1
        assert len(client.get("/api/jobs").json()["items"]) == 1
