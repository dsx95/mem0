import copy
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from local_runtime.chat import Chat, SessionInput
from local_runtime.dashboard import create_app
from local_runtime.test_dashboard import TestMemory

HEADERS = {"X-Memory-Client": "dashboard"}


class Memory(TestMemory):
    def add(self, text, user_id, infer, metadata):
        result = super().add(text, user_id, infer, metadata)
        stamp = datetime.now(timezone.utc).isoformat()
        self.client.set_payload(self.settings.collection, {"created_at": stamp, "updated_at": stamp},
                                points=[result["results"][0]["id"]])
        return result

    def search(self, text, filters, top_k):
        rows, _ = self.client.scroll(self.settings.collection, limit=100)
        return {"results": [{"id": str(row.id), "memory": row.payload["data"], "score": .9, "metadata": {},
                             "created_at": row.payload.get("created_at"), "updated_at": row.payload.get("updated_at")}
                            for row in rows if row.payload.get("user_id") == filters["user_id"]][:top_k]}


def chunk(content=None, call=None, finish=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=[call] if call else None), finish_reason=finish)])


def function(args, name="mem0", identity="call-one", finish="tool_calls"):
    encoded = json.dumps(args)
    return [chunk(call=NS(index=0, id=identity, function=NS(name=name, arguments=encoded[:9]))),
            chunk(call=NS(index=0, id=None, function=NS(name=None, arguments=encoded[9:]))),
            chunk(finish=finish)]


class Stream:
    def __init__(self, values):
        self.values = values
    def __iter__(self):
        yield from self.values
    def close(self):
        pass


class Model:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.calls = []
        self.chat = NS(completions=self)
    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        value = next(self.batches)
        if isinstance(value, Exception):
            raise value
        return Stream(value)
    def close(self):
        pass


@pytest.fixture
def console(tmp_path):
    settings = NS(data_dir=tmp_path / "data", collection="test", timeout=1, max_retries=0, max_tokens=1024,
        preset="qwen", validate=lambda *args: None, llm=NS(model="qwen-plus", provider="openai", api_key="never-return-me", base_url="http://example.invalid"),
        embedding=NS(model="test"), embedding_dims=3)
    app = create_app(settings=settings, root=tmp_path / "materials", memory_factory=Memory)
    with TestClient(app) as client:
        yield client, app.state.chat, app.state.service


def session(client, **kwargs):
    response = client.post("/api/chat/sessions", json={"user_id": "chat_alice", **kwargs}, headers=HEADERS)
    assert response.status_code == 201
    return response.json()["id"]


def message(client, sid, text="记住我喜欢茶", request_id=None, **context):
    response = client.post(f"/api/chat/sessions/{sid}/messages", headers=HEADERS,
        json={"text": text, "request_id": request_id or str(uuid.uuid4()), **context})
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1]["type"] == "done"
    return events


def test_real_protocol_streamed_tool_ids_and_persisted_new_session(console):
    client, chat, service = console
    model = Model([function({"action": "remember", "text": "喜欢喝茶"}), [chunk("已记住。"), chunk(finish="stop")]])
    chat.client_factory = lambda: model
    sid = session(client)
    events = message(client, sid)
    assert events[-1]["status"] == "complete"
    assert [event["type"] for event in events].count("tool_end") == 2
    assert any(event["type"] == "tool_end" and event["event"].get("automatic") for event in events)
    assert model.calls[0]["tool_choice"] == "auto"
    tool = model.calls[1]["messages"][-1]
    assert tool["role"] == "tool" and tool["tool_call_id"] == "call-one"
    assert json.loads(tool["content"])["saved"]
    assert model.calls[1]["messages"][-2]["tool_calls"][0]["id"] == "call-one"
    another = session(client)
    memory = client.get(f"/api/chat/sessions/{another}/memories").json()
    assert memory["total"] == 1 and memory["items"][0]["text"] == "喜欢喝茶"
    assert len(service.snapshot()) == 26
    detail = client.get(f"/api/chat/sessions/{sid}").json()
    assert any(event["result"].get("saved") for event in detail["turns"][0]["events"])
    assert "never-return-me" not in json.dumps(detail)


def test_scope_cannot_be_selected_by_model_and_library_is_read_only(console):
    client, chat, service = console
    sid = session(client, use_library=False)
    current = chat.session(sid)
    assert "error" in chat.execute(current, {"action": "remember", "text": "x", "user_id": "knowin_public"})
    assert "error" in chat.execute(current, {"action": "delete", "text": "x"})
    assert "error" in chat.execute(current, {"action": "search", "text": "x", "scope": "library"})
    result = chat.execute(current, {"action": "remember", "text": "仅属于我", "scope": "library"})
    assert result["saved"]
    item = next(item for item in service.snapshot() if item["memory"] == "仅属于我")
    assert item["user_id"] == current["memory_user_id"]
    bob = session(client, user_id="chat_bob")
    assert client.get(f"/api/chat/sessions/{bob}/memories").json()["total"] == 0
    assert chat.execute(chat.session(bob), {"action": "search", "text": "仅属于我", "scope": "personal"})["count"] == 0


def test_repeated_write_and_request_dedup(console):
    client, chat, service = console
    args = {"action": "remember", "text": "喜欢喝茶"}
    model = Model([function(args), function(args, identity="call-two"), [chunk("好了"), chunk(finish="stop")]])
    chat.client_factory = lambda: model
    sid = session(client)
    request_id = str(uuid.uuid4())
    message(client, sid, request_id=request_id)
    assert service.memory.add_calls == 1
    response = client.post(f"/api/chat/sessions/{sid}/messages", headers=HEADERS, json={"text": "same", "request_id": request_id})
    assert response.status_code == 409


def test_unknown_tool_and_provider_failure_are_visible_and_redacted(console):
    client, chat, _ = console
    model = Model([function({"action": "remember", "text": "x"}, name="shell"), [chunk("未执行"), chunk(finish="stop")]])
    chat.client_factory = lambda: model
    sid = session(client)
    events = message(client, sid)
    assert next(e for e in events if e["type"] == "tool_end" and not e["event"].get("automatic"))["event"]["result"]["error"] == "未知工具"
    chat.client_factory = lambda: Model([RuntimeError("never-return-me private error")])
    events = message(client, sid)
    assert events[-1]["status"] == "failed"
    assert "never-return-me" not in json.dumps(events)
    assert sid not in chat.runs


def test_limits_invalid_payload_and_origin(console):
    client, chat, _ = console
    sid = session(client)
    endpoint = f"/api/chat/sessions/{sid}/messages"
    assert client.post(endpoint, headers=HEADERS, json={"text": "x" * 12001, "request_id": str(uuid.uuid4())}).status_code == 422
    assert client.post(endpoint, headers=HEADERS, json={"text": "   ", "request_id": str(uuid.uuid4())}).status_code == 422
    assert client.post("/api/chat/sessions", json={}).status_code == 403
    assert client.post("/api/chat/sessions", json={}, headers={**HEADERS, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/chat/sessions", headers=HEADERS, json={"user_id": "knowin_public", "family_id": "home"}).status_code == 422
    model = Model([function({"action": "search", "text": "x"}, identity=f"call-{n}") for n in range(5)])
    chat.client_factory = lambda: model
    events = message(client, sid)
    assert len(model.calls) == 5 and model.calls[-1]["tool_choice"] == "none"
    assert events[-1]["status"] == "failed"


def test_no_tool_hallucinated_write_is_not_executed_and_delete_preserves_memory(console):
    client, chat, service = console
    chat.client_factory = lambda: Model([[chunk("你好！"), chunk(finish="stop")]])
    sid = session(client)
    events = message(client, sid, "你好")
    assert all(e["event"].get("automatic") for e in events if e["type"].startswith("tool_"))
    assert service.memory.add_calls == 0
    assert client.delete(f"/api/chat/sessions/{sid}", headers=HEADERS).status_code == 200
    assert client.get(f"/api/chat/sessions/{sid}").status_code == 404
    assert len(service.snapshot()) == 25


def test_cancel_and_concurrent_session_lock(console):
    client, chat, _ = console
    gate = threading.Event()
    def delayed():
        gate.set()
        for _ in range(100):
            time.sleep(.015)
            yield chunk("字")
        yield chunk(finish="stop")
    chat.client_factory = lambda: Model([delayed()])
    sid = session(client)
    result = []
    worker = threading.Thread(target=lambda: result.extend(message(client, sid)))
    worker.start()
    assert gate.wait(3)
    collision = client.post(f"/api/chat/sessions/{sid}/messages", headers=HEADERS, json={"text": "two", "request_id": str(uuid.uuid4())})
    assert collision.status_code == 409
    assert client.post(f"/api/chat/sessions/{sid}/cancel", headers=HEADERS).status_code == 200
    worker.join(4)
    assert not worker.is_alive() and result[-1]["status"] == "cancelled"


def test_family_shared_and_member_private_scopes(console):
    client, chat, _ = console
    alice = chat.session(session(client, user_id="alice", family_id="home1", use_library=False))
    bob = chat.session(session(client, user_id="bob", family_id="home1", use_library=False))
    elsewhere = chat.session(session(client, user_id="alice", family_id="home2", use_library=False))
    assert chat.execute(alice, {"action": "remember", "text": "Alice 私人偏好"})["scope"] == "personal"
    assert "error" in chat.execute(alice, {"action": "remember", "text": "禁止模型自行共享", "scope": "family"})
    shared = chat.execute({**alice, "remember_scope": "family"}, {"action": "remember", "text": "周六家庭聚餐"})
    assert shared["saved"] and shared["scope"] == "family"
    def search(current):
        return chat.execute(current, {"action": "search", "text": "偏好和约定", "scope": "all"})["memories"]
    assert {r["text"] for r in search(alice)} == {"Alice 私人偏好", "周六家庭聚餐"}
    assert [r["text"] for r in search(bob)] == ["周六家庭聚餐"]
    assert search(elsewhere) == []
    rows = client.get(f"/api/chat/sessions/{bob['id']}/memories").json()["items"]
    assert len(rows) == 1 and rows[0]["scope"] == "family"
    listed = client.get("/api/chat/sessions?user_id=alice&family_id=home1").json()["items"]
    assert [r["id"] for r in listed] == [alice["id"]]


def test_every_ordinary_turn_prefetches_only_current_member_and_family(console):
    client, chat, _ = console
    alice = chat.session(session(client, user_id="alice", family_id="home1", use_library=False))
    bob = chat.session(session(client, user_id="bob", family_id="home1", use_library=False))
    other_home = chat.session(session(client, user_id="alice", family_id="home2", use_library=False))
    chat.execute(alice, {"action": "remember", "text": "Alice 私人事实"})
    chat.execute(bob, {"action": "remember", "text": "Bob 私人事实"})
    chat.execute(other_home, {"action": "remember", "text": "另一个家庭的私人事实"})
    chat.execute({**alice, "remember_scope": "family"}, {"action": "remember", "text": "本家庭共享事实"})
    chat.execute({**other_home, "remember_scope": "family"}, {"action": "remember", "text": "另一个家庭共享事实"})
    model = Model([[chunk("好的"), chunk(finish="stop")]])
    chat.client_factory = lambda: model
    assert message(client, alice["id"], "你好")[-1]["status"] == "complete"
    tool_result = next(item for item in model.calls[0]["messages"] if item["role"] == "tool")
    retrieved = json.loads(tool_result["content"])
    assert {(row["scope"], row["text"]) for row in retrieved["memories"]} == {
        ("personal", "Alice 私人事实"), ("family", "本家庭共享事实")}
    assert all(datetime.fromisoformat(row["created_at"]).tzinfo for row in retrieved["memories"])


def test_memory_and_chat_timestamps_are_exposed(console):
    client, chat, _ = console
    sid = session(client, user_id="alice", family_id="home1", use_library=False)
    current = chat.session(sid)
    chat.execute(current, {"action": "remember", "text": "带时间的个人记忆"})
    memory = client.get(f"/api/chat/sessions/{sid}/memories").json()["items"][0]
    assert datetime.fromisoformat(memory["created_at"]).tzinfo
    assert datetime.fromisoformat(memory["updated_at"]).tzinfo
    chat.client_factory = lambda: Model([[chunk("好的"), chunk(finish="stop")]])
    message(client, sid, "你好")
    turn = client.get(f"/api/chat/sessions/{sid}").json()["turns"][0]
    assert datetime.fromisoformat(turn["created_at"]).tzinfo


def test_combined_search_keeps_private_shared_and_document_results(console):
    client, chat, service = console
    current = chat.session(session(client, user_id="alice", family_id="home1"))
    chat.execute(current, {"action": "remember", "text": "个人偏好：喝无糖拿铁"})
    chat.execute({**current, "remember_scope": "family"},
                 {"action": "remember", "text": "家庭约定：周六聚餐"})
    for index in range(15):
        service.memory.add(f"公开资料：第 {index} 条家庭产品说明", user_id="knowin_public", infer=False,
                           metadata={"source_file": f"资料{index}.txt"})
    service.refresh()
    result = chat.execute(current, {"action": "search", "text": "结合我的偏好、家庭约定和资料", "scope": "all"})
    assert {row["scope"] for row in result["memories"]} == {"personal", "family", "library"}
    assert result["count"] <= 8


def test_message_identity_is_validated_persisted_and_passed_to_model(console):
    client, chat, _ = console
    sid = session(client, user_id="user001", family_id="family001")
    endpoint = f"/api/chat/sessions/{sid}/messages"
    for context in ({"user_id": "user002"}, {"family_id": "family002"}):
        assert client.post(endpoint, headers=HEADERS, json={"text": "你好", "request_id": str(uuid.uuid4()), **context}).status_code == 409
    model = Model([[chunk("你好"), chunk(finish="stop")]])
    chat.client_factory = lambda: model
    context = {"user_id": "user001", "family_id": "family001", "remember_scope": "family"}
    message(client, sid, "你好", **context)
    assert json.dumps(context, ensure_ascii=False) in model.calls[0]["messages"][0]["content"]
    assert chat.detail(sid)["turns"][0]["input_context"] == context
    assert client.patch(f"/api/chat/sessions/{sid}", headers=HEADERS,
        json={"user_id": "user001", "family_id": "different"}).status_code == 422
    alone = session(client, user_id="alone")
    assert client.post(f"/api/chat/sessions/{alone}/messages", headers=HEADERS,
        json={"text": "记住", "request_id": str(uuid.uuid4()), "remember_scope": "family"}).status_code == 422


def test_memory_binding_survives_deleting_all_conversations(console):
    client, chat, _ = console
    sid = session(client, user_id="alice", family_id="home1")
    key = chat.session(sid)["memory_user_id"]
    chat.execute(chat.session(sid), {"action": "remember", "text": "仍然记得"})
    assert client.delete(f"/api/chat/sessions/{sid}", headers=HEADERS).status_code == 200
    new = session(client, user_id="alice", family_id="home1")
    assert chat.session(new)["memory_user_id"] == key
    assert client.get(f"/api/chat/sessions/{new}/memories").json()["total"] == 1


def test_legacy_database_keeps_history_and_original_memory_binding(tmp_path):
    path = tmp_path / "chat.sqlite"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE chat_sessions(id TEXT PRIMARY KEY,user_id TEXT,title TEXT,use_library INTEGER,created_at TEXT,updated_at TEXT);
        CREATE TABLE chat_turns(id TEXT PRIMARY KEY,session_id TEXT,request_id TEXT UNIQUE,user_text TEXT,answer TEXT,status TEXT,events TEXT,protocol TEXT,error TEXT,created_at TEXT);
        INSERT INTO chat_sessions VALUES('old','chat_default','已有对话',1,'2026','2026');
        INSERT INTO chat_turns VALUES('turn','old','request','问题','回答','complete','[]','[]',NULL,'2026');
    """)
    db.commit()
    db.close()
    chat = Chat(NS(directory=tmp_path, snapshot=list))
    assert chat.detail("old")["turns"][0]["answer"] == "回答"
    assert chat.detail("old")["turns"][0]["input_context"] == {}
    new = chat.create(SessionInput())
    assert new["memory_user_id"] == "chat_default" and new["family_id"] == ""
    reopened = Chat(NS(directory=tmp_path, snapshot=list))
    assert reopened.create(SessionInput())["memory_user_id"] == "chat_default"


@pytest.mark.parametrize("finish", ["tool_calls", "stop"])
def test_robot_payload_question_requires_real_library_tool_call(console, finish):
    client, chat, service = console
    model = Model([function({"action": "search", "text": "Knowin-X1 双臂最大负载", "scope": "all"}, finish=finish),
        [chunk("资料中未找到该参数。"), chunk(finish="stop")]])
    chat.client_factory = lambda: model
    sid = session(client)
    events = message(client, sid, "你的双臂可以拎多少kg的重物")
    assert model.calls[0]["tool_choice"] == {"type": "function", "function": {"name": "mem0"}}
    assert any(e["type"] == "tool_end" and e["event"]["arguments"]["scope"] == "all" for e in events)
    assert service.memory.add_calls == 0
    model = Model([[chunk("资料库未启用，无法核实参数。"), chunk(finish="stop")]])
    chat.client_factory = lambda: model
    message(client, session(client, use_library=False), "你的双臂可以拎多少kg的重物")
    assert model.calls[0]["tool_choice"] == "auto"


def test_truncated_forced_tool_call_is_not_executed(console):
    client, chat, service = console
    chat.client_factory = lambda: Model([function({"action": "remember", "text": "不应写入"}, finish="length")])
    events = message(client, session(client), "你的双臂可以拎多少kg的重物")
    assert events[-1]["status"] == "failed"
    assert service.memory.add_calls == 0


def test_product_search_expands_spoken_terms_and_prioritizes_specification(console):
    client, chat, service = console
    seen = []
    def search(text, filters, top_k):
        seen.append((text, filters, top_k))
        return {"results": [
            {"id": "news", "memory": "双臂具备强大能力", "score": .9},
            {"id": "spec", "memory": "产品参数：双臂最大负载 6 kg", "score": .6,
             "metadata": {"source_file": "参数.docx", "page_label": "7"}}]}
    service.memory.search = search
    result = chat.execute(chat.session(session(client)), {"action": "search", "text": "Knowin-X1 双臂承重能力", "scope": "library"})
    assert "最大负载" in seen[0][0] and seen[0][2] == 20
    assert seen[0][1] == {"user_id": "knowin_public"}
    assert result["memories"][0]["id"] == "spec"
    assert result["memories"][0]["source"] == "参数.docx"


def test_users_include_all_imported_users_and_chat_users_without_internal_keys(console):
    client, chat, _ = console
    sid = session(client, user_id="new_member", family_id="home1")
    chat.execute(chat.session(sid), {"action": "remember", "text": "个人偏好"})
    chat.execute({**chat.session(sid), "remember_scope": "family"}, {"action": "remember", "text": "家庭约定"})
    rows = client.get("/api/chat/users").json()["items"]
    by_id = {row["user_id"]: row for row in rows}
    assert set(by_id) == {"alice", "bob", "new_member"}
    assert by_id["alice"]["memory_count"] == 12
    assert by_id["bob"]["memory_count"] == 13
    assert by_id["new_member"]["memory_count"] == 2
    assert by_id["new_member"]["family_ids"] == ["home1"]
    assert client.delete(f"/api/chat/sessions/{sid}", headers=HEADERS).status_code == 200
    assert "new_member" in {row["user_id"] for row in client.get("/api/chat/users").json()["items"]}


def test_selecting_imported_user_reads_existing_memory_and_preserves_family_isolation(console):
    client, chat, _ = console
    sid = session(client, user_id="alice")
    assert chat.session(sid)["memory_user_id"] == "alice"
    assert client.get(f"/api/chat/sessions/{sid}/memories").json()["total"] == 12
    family = session(client, user_id="alice", family_id="different")
    assert client.get(f"/api/chat/sessions/{family}/memories").json()["total"] == 0


def test_public_user_is_selectable_and_read_only(console):
    client, chat, service = console
    service.memory.add("公开产品参数", user_id="knowin_public", infer=False, metadata={})
    service.refresh()
    sid = session(client, user_id="knowin_public", use_library=False)
    current = chat.session(sid)
    assert current["use_library"] and current["memory_user_id"] == "knowin_public"
    assert "error" in chat.execute(current, {"action": "remember", "text": "禁止写入资料库"})
    assert service.memory.add_calls == 1
    result = chat.execute(current, {"action": "search", "text": "产品参数", "scope": "all"})
    assert result["count"] == 1 and result["memories"][0]["scope"] == "library"
    assert client.get(f"/api/chat/sessions/{sid}/memories").json()["items"][0]["scope"] == "library"
    public = next(row for row in client.get("/api/chat/users").json()["items"] if row["user_id"] == "knowin_public")
    assert public["read_only"]
