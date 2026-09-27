"""To-do lifecycle, authorization, chat protocol and portable data regression."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import sqlite3
from zoneinfo import ZoneInfo

import pytest

from .test_chat import Model, chunk, function, message, session
from .test_dashboard import HEADERS, login
from .test_management import setup_family
from . import todo_tool

pytest_plugins = ("local_runtime.test_chat",)


def create(client, **extra):
    response = client.post("/api/todos", headers=HEADERS, json={"title": "买牛奶", **extra})
    assert response.status_code == 201, response.text
    return response.json()


def update(client, item, **changes):
    return client.patch(
        "/api/todos/" + item["id"], headers=HEADERS, json={"revision": item["revision"], "changes": changes}
    )


def test_rich_form_persists_and_validates_dates(console):
    client, _, _ = console
    item = create(
        client,
        description="无糖，两盒",
        list_name="购物清单",
        priority="high",
        due_date="2030-06-15",
        start_at="2030-06-15T09:00:00+08:00",
        tags=["采购", "采购", "家庭"],
        location="超市",
        estimated_minutes=30,
        checklist=[{"text": "买奶"}, {"text": "买面包", "done": True}],
    )
    detail = client.get("/api/todos/" + item["id"]).json()
    assert detail["created_at"] == detail["updated_at"] and detail["completed_at"] == ""
    assert detail["assignee_user_id"] == "chat_default"
    assert detail["due_date"] == "2030-06-15" and not detail["due_at"]
    assert detail["tags"] == ["采购", "家庭"] and len(detail["events"]) == 1
    assert all(c["id"] for c in detail["checklist"])
    assert detail["start_at"] == "2030-06-15T01:00:00.000000+00:00"
    for values in [
        {"due_at": "2030-06-15T12:00:00"},
        {"due_date": "bad"},
        {"timezone": "Invalid/Zone"},
        {"start_at": "2030-06-17T00:00:00Z", "due_date": "2030-06-16"},
        {"due_date": "2030-06-16", "due_at": "2030-06-16T00:00:00Z"},
        {"title": " "},
        {"created_at": "2020-01-01"},
        {"tags": ["x" * 41]},
    ]:
        assert client.post("/api/todos", headers=HEADERS, json={"title": "test", **values}).status_code == 422


def test_status_transitions_timestamps_history_and_reopen(console):
    client, _, _ = console
    item = create(client)
    for status in ["in_progress", "blocked", "done", "pending", "cancelled", "pending"]:
        result = update(client, item, status=status)
        assert result.status_code == 200, result.text
        next_item = result.json()
        assert next_item["created_at"] == item["created_at"] and next_item["revision"] == item["revision"] + 1
        assert bool(next_item["completed_at"]) == (status == "done")
        assert bool(next_item["cancelled_at"]) == (status == "cancelled")
        item = next_item
    assert len(client.get("/api/todos/" + item["id"]).json()["events"]) == 7
    assert update(client, item, status="pending").json()["revision"] == item["revision"]


def test_private_family_assignment_and_source_privacy(console):
    client, _, service = console
    setup_family(client)
    private = create(client, family_id="home", device_id="robot1")
    shared = service.todos.create(
        "alice",
        {"title": "家庭采购", "family_id": "home", "visibility": "family", "assignee_user_id": "bob"},
        source={"type": "chat", "quote": "只属于原对话的原话", "session_id": "private-session"},
    )
    login(client, "bob")
    assert {i["id"] for i in client.get("/api/todos").json()["items"]} == {shared["id"]}
    assert client.get("/api/todos/" + private["id"]).status_code == 404
    assert update(client, private, title="越权").status_code == 404
    detail = client.get("/api/todos/" + shared["id"]).json()
    assert detail["source"] == {"type": "chat"} and detail["can_edit"] and not detail["can_manage"]
    assert update(client, shared, title="改标题").status_code == 403
    done = update(client, shared, status="done")
    assert done.status_code == 200 and done.json()["completed_at"]
    assert client.delete("/api/todos/" + shared["id"] + "?revision=2", headers=HEADERS).status_code == 403
    login(client, "outsider")
    assert client.get("/api/todos").json()["total"] == 0
    assert client.get("/api/todos/" + shared["id"]).status_code == 404
    assert client.post("/api/todos", headers=HEADERS, json={"title": "越权", "family_id": "home"}).status_code == 403
    login(client, "alice")
    assert client.delete("/api/todos/" + shared["id"] + "?revision=2", headers=HEADERS).status_code == 200


def test_unassigned_shared_read_only_and_context_validation(console):
    client, _, service = console
    setup_family(client)
    shared = create(client, family_id="home", visibility="family")
    login(client, "bob")
    assert not client.get("/api/todos/" + shared["id"]).json()["can_edit"]
    assert update(client, shared, status="done").status_code == 403
    for body in [
        {"visibility": "family"},
        {"family_id": "home", "assignee_user_id": "alice"},
        {"family_id": "home", "visibility": "family", "assignee_user_id": "outsider"},
        {"device_id": "robot1"},
    ]:
        assert client.post("/api/todos", headers=HEADERS, json={"title": "x", **body}).status_code in {403, 422}
    with service.access.db() as db:
        db.execute("DELETE FROM app_members WHERE family_id='home' AND user_id='bob'")
    assert client.get("/api/todos/" + shared["id"]).status_code == 404


def test_idempotency_replay_concurrency_and_delete_tombstone(console):
    client, _, service = console
    body = {"title": "一次创建", "checklist": [{"text": "第一步"}]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: service.todos.create("chat_default", body, request_id="same"), range(2)))
    assert rows[0]["id"] == rows[1]["id"]
    item = rows[0]
    assert len(client.get("/api/todos/" + item["id"]).json()["events"]) == 1
    assert (
        client.post("/api/todos", headers=HEADERS, json={"title": "不同内容", "request_id": "same"}).status_code == 409
    )
    assert client.delete("/api/todos/" + item["id"] + "?revision=1", headers=HEADERS).status_code == 200
    assert client.get("/api/todos/" + item["id"]).status_code == 404
    with pytest.raises(Exception) as replay:
        service.todos.create("chat_default", body, request_id="same")
    assert replay.value.status_code == 410
    with service.access.db() as db:
        assert db.execute("SELECT COUNT(*) FROM todo_events WHERE todo_id=?", (item["id"],)).fetchone()[0] == 0


def test_concurrent_versions_scope_immutable_and_no_resurrection(console):
    client, _, service = console
    item = create(client)

    def change(title):
        try:
            return service.todos.update("chat_default", item["id"], 1, {"title": title})["revision"]
        except Exception as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, ["A", "B"])) == [2, 409]
    assert update(client, item, visibility="family").status_code == 422
    assert client.delete("/api/todos/" + item["id"] + "?revision=1", headers=HEADERS).status_code == 409
    assert client.delete("/api/todos/" + item["id"] + "?revision=2", headers=HEADERS).status_code == 200
    assert update(client, item, title="复活").status_code == 404


def test_exact_filters_pagination_date_only_and_timezone(console):
    client, _, service = console
    tz = ZoneInfo("Asia/Shanghai")
    today = datetime.now(tz).date()
    for i in range(25):
        create(client, title=f"事项 {i}", due_date=today.isoformat(), priority="high")
    create(client, title="已过期", due_at=(datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    create(client, title="昨天但完成了", due_date=(today - timedelta(days=1)).isoformat(), status="done")
    create(client, title="明天", due_date=(today + timedelta(days=1)).isoformat(), list_name="出行")
    create(client, title="没定时间")
    result = client.get("/api/todos?period=today&priority=high").json()
    assert result["total"] == 25 and len(result["items"]) == 20 and result["has_more"]
    assert client.get("/api/todos?period=today&priority=high&page=2").json()["total"] == 25
    assert len(client.get("/api/todos?period=today&priority=high&page=2").json()["items"]) == 5
    assert client.get("/api/todos?period=overdue").json()["total"] == 1
    assert client.get("/api/todos?period=unscheduled").json()["total"] == 1
    assert client.get("/api/todos?period=upcoming&list_name=出行").json()["total"] == 1
    assert client.get("/api/todos?q=%25").json()["total"] == 0  # '%' is literal, not SQL wildcard
    with service.access.db() as db:
        end = db.execute("SELECT due_at FROM todos WHERE title=?", ("事项 0",)).fetchone()[0]
    assert datetime.fromisoformat(end).astimezone(tz).date() == today


def test_date_only_dst_deadline_and_reschedule(console):
    client, _, service = console
    item = create(client, due_date="2030-03-10", timezone="America/New_York")
    with service.access.db() as db:
        due = db.execute("SELECT due_at FROM todos WHERE id=?", (item["id"],)).fetchone()[0]
    assert due == "2030-03-11T03:59:59.999999+00:00"
    result = update(client, item, due_date="", due_at="2030-03-12T14:00:00-04:00")
    assert result.status_code == 200 and result.json()["due_at"] == "2030-03-12T18:00:00.000000+00:00"


def test_chat_todo_protocol_not_vector_memory_and_retry(console):
    client, chat, service = console
    sid = session(client)
    args = {
        "action": "create",
        "data": {"title": "买牛奶", "due_date": "2030-06-15", "list_name": "购物"},
        "source_quote": "帮我建待办买牛奶",
    }
    model = Model(
        [
            function(args, name="todo"),
            function(args, name="todo", identity="call-two"),
            [chunk("已加入购物清单。"), chunk(finish="stop")],
        ]
    )
    chat.client_factory = lambda: model
    total = len(service.snapshot())
    events = message(client, sid, text="帮我建待办买牛奶，2030年6月15日截止")
    assert events[-1]["status"] == "complete"
    tasks = client.get("/api/todos").json()
    assert tasks["total"] == 1 and tasks["items"][0]["source"]["turn_id"]
    assert len(service.snapshot()) == total
    assert model.calls[0]["messages"][-2]["role"] == "tool"
    assert {t["function"]["name"] for t in model.calls[0]["tools"]} == {"mem0", "todo"}
    item = tasks["items"][0]
    model = Model(
        [
            function(
                {
                    "action": "update",
                    "task_id": item["id"],
                    "revision": 1,
                    "data": {"status": "done"},
                    "source_quote": "买牛奶这条待办完成了",
                },
                name="todo",
            ),
            [chunk("已标记完成。"), chunk(finish="stop")],
        ]
    )
    chat.client_factory = lambda: model
    assert message(client, sid, text="买牛奶这条待办完成了")[-1]["status"] == "complete"
    assert client.get("/api/todos/" + item["id"]).json()["status"] == "done"


def test_tool_guards_and_private_family_scopes(console):
    client, chat, service = console
    setup_family(client)
    sid = session(client, user_id="alice", family_id="home")
    context = {**chat.session(sid), "_user_text": "创建一个待办买奶", "_turn_id": "test"}
    args = {"action": "create", "data": {"title": "买奶"}, "source_quote": "捏造的原话"}
    assert todo_tool.execute(service, context, args)["status_code"] == 422
    args["source_quote"] = "创建一个待办买奶"
    args["data"]["family_id"] = "injected"
    assert todo_tool.execute(service, context, args)["status_code"] == 422
    args["data"].pop("family_id")
    item = todo_tool.execute(service, context, args)["todo"]
    assert item["visibility"] == "personal"
    assert (
        todo_tool.execute(service, {**context, "family_id": ""}, {"action": "get", "task_id": item["id"]})[
            "status_code"
        ]
        == 403
    )
    assert (
        todo_tool.execute(
            service,
            context,
            {
                "action": "update",
                "task_id": item["id"],
                "data": {"status": "done"},
                "source_quote": args["source_quote"],
            },
        )["status_code"]
        == 422
    )
    assert "error" in todo_tool.execute(service, context, {"action": "list", "page": "bad"})


def test_todos_survive_migration_and_records_are_not_overwritten(console, tmp_path):
    from . import migration as m
    from .test_migration import settings

    client, _, service = console
    item = create(client, title="迁移清单", description="迁移备注", due_date="2030-06-15", checklist=[{"text": "步骤"}])
    update(client, item, status="in_progress")
    root = service.settings.data_dir.parent
    package, manifest = m.export_bundle(root, settings(root), client=service.memory.client, materials=service.root)
    assert manifest["version"] == 3 and manifest["counts"]["todos"] == 1 and manifest["counts"]["todo_events"] == 2
    target = tmp_path / "restored"
    m.restore_bundle(package, settings(target), target)
    with sqlite3.connect(target / "data/dashboard/chat.sqlite") as db:
        row = db.execute("SELECT title,status,created_at,revision FROM todos WHERE id=?", (item["id"],)).fetchone()
        assert row == ("迁移清单", "in_progress", item["created_at"], 2)
        assert db.execute("SELECT COUNT(*) FROM todo_events").fetchone()[0] == 2


def test_family_owner_can_manage_members_task_and_no_identity_injection(console):
    client, _, _ = console
    setup_family(client)
    login(client, "bob")
    item = create(client, family_id="home", visibility="family", status="draft")
    assert client.get("/api/todos?status=open").json()["total"] == 1
    login(client, "alice")
    assert client.get("/api/todos/" + item["id"]).json()["can_manage"]
    updated = update(client, item, title="家庭共同事项", assignee_user_id="bob")
    assert updated.status_code == 200
    assert client.post("/api/todos", headers=HEADERS, json={"title": "冒充", "owner_user_id": "bob"}).status_code == 422
    assert client.delete("/api/todos/" + item["id"] + "?revision=2", headers=HEADERS).status_code == 200
    client.cookies.clear()
    assert client.get("/api/todos").status_code == 401


def test_todo_queries_do_not_depend_on_embedding_and_model_payload_is_bounded(console):
    client, chat, service = console
    sid = session(client)
    create(client, title="模型预算", description="内容" * 3000, checklist=[{"text": "x" * 300} for _ in range(50)])

    def broken(*args, **kwargs):
        raise AssertionError("To-do lookup must not embed queries")

    service.memory.search = broken
    model = Model([[chunk("有一条待办。"), chunk(finish="stop")]])
    chat.client_factory = lambda: model
    events = message(client, sid, text="我的待办清单里有什么？")
    assert events[-1]["status"] == "complete"
    result = next(e["event"]["result"] for e in events if e["type"] == "tool_end")
    assert result["total"] == 1 and result["items"][0]["description_truncated"]
    assert len(result["items"][0]["description"]) == 300 and "checklist" not in result["items"][0]
    assert result["items"][0]["checklist_progress"]["total"] == 50
