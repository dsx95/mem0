"""Rich task data, execution audit, reminder windows and legacy migration."""

import json
import sqlite3

import pytest

from . import todo_tool, todos
from .test_chat import Model, chunk, function, message, session
from .test_dashboard import HEADERS, login
from .test_management import setup_family
from .test_todos import create, update

pytest_plugins = ("local_runtime.test_chat",)


def plan():
    return {
        "reminder_enabled": True,
        "reminder_at": "2030-06-15T09:00:00+08:00",
        "reminder_expires_at": "2030-06-15T10:00:00+08:00",
        "reminder_note": "带上资料",
    }


def test_people_details_and_execution_audit(console):
    client, _, service = console
    setup_family(client)
    item = create(
        client,
        title="家庭采购",
        family_id="home",
        visibility="family",
        assignee_user_id="bob",
        participant_user_ids=["alice", "bob", "bob"],
        required_resources="购物袋、清单",
        completion_criteria="核对全部商品",
        **plan(),
    )
    assert item["initiator_user_id"] == item["owner_user_id"] == "alice"
    assert item["participant_user_ids"] == ["alice", "bob"]
    assert {p["user_id"] for p in item["participants"]} == {"alice", "bob"}
    assert not item["actual_started_at"] and not item["completed_by"]
    login(client, "bob")
    item = update(client, item, status="in_progress").json()
    started = item["actual_started_at"]
    assert started
    item = update(client, item, status="blocked", blocked_reason="超市未开门", actual_minutes=0).json()
    assert item["blocked_reason"] == "超市未开门" and item["actual_minutes"] == 0
    assert update(client, item, participant_user_ids=[]).status_code == 403
    assert update(client, item, reminder_enabled=False).status_code == 403
    item = update(client, item, status="done", completion_note="已全部核对", actual_minutes=45).json()
    assert item["completed_by"] == "bob" and item["completed_at"]
    assert item["actual_started_at"] == started and item["reminder_state"] == "inactive"
    login(client, "alice")
    item = update(client, item, title="采购已完成").json()
    assert item["completed_by"] == "bob"  # Editing a completed task doesn't claim its completion.
    item = update(client, item, status="pending").json()
    assert not item["completed_by"] and not item["completed_at"] and item["actual_started_at"] == started
    assert service.todos.get("alice", item["id"])["events"][1]["actor"] == "alice"


@pytest.mark.parametrize(
    "bad",
    [
        {"participant_user_ids": ["outsider"]},
        {"participant_user_ids": [""]},
        {"participant_user_ids": ["x" * 65]},
        {"actual_minutes": -1},
        {"initiator_user_id": "bob"},
        {"actual_started_at": "2030-01-01T00:00:00Z"},
        {"completed_by": "bob"},
        {"reminder_enabled": True},
        {"reminder_at": "2030-01-01T00:00:00Z"},
        {**plan(), "reminder_at": "2030-06-15T09:00:00"},
        {**plan(), "reminder_expires_at": "2030-06-15T09:00:00+08:00"},
        {**plan(), "reminder_expires_at": "2030-06-15T08:00:00+08:00"},
    ],
)
def test_rejects_invalid_or_forged_details(console, bad):
    client, _, _ = console
    assert client.post("/api/todos", headers=HEADERS, json={"title": "测试", **bad}).status_code == 422


def test_collaboration_is_not_permission_and_departure_cleans_trash(console):
    client, _, _ = console
    setup_family(client)
    shared = create(client, family_id="home", visibility="family", participant_user_ids=["bob"])
    private = create(client, family_id="home", participant_user_ids=["alice"])
    login(client, "bob")
    assert not client.get("/api/todos/" + shared["id"]).json()["can_edit"]
    assert update(client, shared, status="done").status_code == 403
    assert client.get("/api/todos/" + private["id"]).status_code == 404
    assert (
        client.post("/api/todos", headers=HEADERS, json={"title": "x", "participant_user_ids": ["alice"]}).status_code
        == 422
    )
    login(client, "alice")
    client.delete("/api/todos/" + shared["id"] + "?revision=1", headers=HEADERS)
    left = client.delete("/api/identity/families/home/members/bob", headers=HEADERS).json()
    assert left["unassigned_todos"] == 0 and left["removed_collaborations"] == 1
    row = client.get("/api/todos/" + shared["id"]).json()
    assert row["participant_user_ids"] == [] and row["revision"] == 3
    assert row["events"][0]["changes"]["participant_user_ids"] == {"before": ["bob"], "after": []}
    assert client.get("/api/todos/" + private["id"]).json()["participant_user_ids"] == ["alice"]
    login(client, "bob")
    assert client.get("/api/todos/" + shared["id"]).status_code == 404


def test_reminder_window_is_distinct_from_deadline_and_delivery(console, monkeypatch):
    client, _, _ = console
    monkeypatch.setattr(todos, "now", lambda: "2030-06-15T00:00:00.000000+00:00")
    item = create(client, due_at="2030-06-15T00:30:00Z", **plan())
    assert item["reminder_state"] == "scheduled" and item["reminder_delivery"] == "not_configured"
    path = "/api/todos/" + item["id"]
    monkeypatch.setattr(todos, "now", lambda: "2030-06-15T01:00:00.000000+00:00")
    assert client.get(path).json()["reminder_state"] == "due"
    assert client.get(path).json()["overdue"]
    monkeypatch.setattr(todos, "now", lambda: "2030-06-15T02:00:00.000000+00:00")
    assert client.get(path).json()["reminder_state"] == "expired"
    assert client.get(path).json()["status"] == "pending"
    item = update(client, item, reminder_enabled=False).json()
    assert item["reminder_state"] == "disabled" and item["reminder_at"]
    item = update(client, item, reminder_enabled=True, status="draft").json()
    assert item["reminder_state"] == "inactive"
    client.delete(path + "?revision=" + str(item["revision"]), headers=HEADERS)
    assert client.get(path).json()["reminder_state"] == "inactive"


@pytest.mark.parametrize("status", ["done", "in_progress"])
def test_old_database_upgrade_preserves_business_fields_and_is_repeatable(console, status):
    client, _, service = console
    item = create(client, status=status, checklist=[{"text": "旧步骤"}])
    new_fields = {
        "participant_user_ids",
        "actual_minutes",
        "required_resources",
        "completion_criteria",
        "blocked_reason",
        "completion_note",
        "reminder_enabled",
        "reminder_at",
        "reminder_expires_at",
        "reminder_note",
    }
    with service.access.db() as db:
        original = dict(db.execute("SELECT * FROM todos WHERE id=?", (item["id"],)).fetchone())
        legacy = {k: v for k, v in json.loads(original["data"]).items() if k not in new_fields}
        db.execute("UPDATE todos SET data=? WHERE id=?", (json.dumps(legacy), item["id"]))
        for column in ("actual_started_at", "completed_by"):
            db.execute("ALTER TABLE todos DROP COLUMN " + column)
    todos.Todos(service)
    todos.Todos(service)
    with service.access.db() as db:
        row = dict(db.execute("SELECT * FROM todos WHERE id=?", (item["id"],)).fetchone())
        for key in original.keys() - {"data", "actual_started_at", "completed_by"}:
            assert row[key] == original[key]
        data = json.loads(row["data"])
        assert {k: data[k] for k in legacy} == legacy
        assert not row["actual_started_at"] and not row["completed_by"]  # Don't invent old actors/times.
        assert data["participant_user_ids"] == [] and data["reminder_enabled"] is False
    assert (
        update(client, client.get("/api/todos/" + item["id"]).json(), required_resources="补充资料").status_code == 200
    )
    assert not client.get("/api/todos/" + item["id"]).json()["actual_started_at"]


def test_chat_can_resolve_members_and_save_rich_fields(console):
    client, chat, service = console
    setup_family(client)
    sid = session(client, user_id="alice", family_id="home")
    context = chat.session(sid)
    assert {m["user_id"] for m in todo_tool.execute(service, context, {"action": "members"})["members"]} == {
        "alice",
        "bob",
    }
    assert todo_tool.execute(service, {**context, "family_id": ""}, {"action": "members"})["members"] == [
        {"user_id": "alice", "name": "alice"}
    ]
    quote = "建待办采购，由bob执行，需要alice协作，2030年6月15日9点提醒，10点失效"
    args = {
        "action": "create",
        "data": {"title": "采购", "assignee_user_id": "bob", "participant_user_ids": ["alice"], **plan()},
        "source_quote": quote,
    }
    chat.client_factory = lambda: Model(
        [function(args, name="todo"), [chunk("已保存提醒计划，目前不会主动响铃或推送。"), chunk(finish="stop")]]
    )
    assert message(client, sid, text=quote, remember_scope="family")[-1]["status"] == "complete"
    item = service.todos.list("alice")["items"][0]
    assert item["participant_user_ids"] == ["alice"] and item["assignee_user_id"] == "bob"
    assert item["reminder_at"] == "2030-06-15T01:00:00.000000+00:00"
    assert item["initiator_user_id"] == "alice" and item["source"]["quote"] == quote
    assert "initiator_user_id" not in todo_tool.properties
    payload = todo_tool.model_item({**item, "required_resources": "a" * 2000})
    assert payload["required_resources_truncated"] and len(payload["required_resources"]) == 300


def test_v5_portable_package_roundtrips_details(console, tmp_path):
    from . import migration as m
    from .test_migration import settings

    client, _, service = console
    item = create(
        client, participant_user_ids=["chat_default"], required_resources="原始资料", status="in_progress", **plan()
    )
    item = update(client, item, status="done", actual_minutes=25, completion_note="核对完成").json()
    root = service.settings.data_dir.parent
    package, manifest = m.export_bundle(root, settings(root), client=service.memory.client, materials=service.root)
    assert manifest["version"] == 5
    target = tmp_path / "restored"
    m.restore_bundle(package, settings(target), target)
    with sqlite3.connect(target / "data/dashboard/chat.sqlite") as db:
        data, started, completed_by = db.execute(
            "SELECT data,actual_started_at,completed_by FROM todos WHERE id=?", (item["id"],)
        ).fetchone()
        assert json.loads(data)["reminder_expires_at"] == item["reminder_expires_at"]
        assert json.loads(data)["participant_user_ids"] == ["chat_default"]
        assert json.loads(data)["actual_minutes"] == 25
        assert started == item["actual_started_at"] and completed_by == "chat_default"
