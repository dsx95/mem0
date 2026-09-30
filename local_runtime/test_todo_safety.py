"""Real SQLite deletion confirmation, recycle bin, departure and migration tests."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException

from . import todo_tool
from .test_chat import Model, chunk, function, message, session
from .test_dashboard import HEADERS, login
from .test_management import setup_family
from .test_todos import create, update

pytest_plugins = ("local_runtime.test_chat",)


def proposal(console, item, text="请删除这条待办", **scope):
    client, chat, service = console
    sid = session(client, **scope)
    args = {"action": "delete", "task_id": item["id"], "revision": item["revision"], "source_quote": text}
    chat.client_factory = lambda: Model([function(args, name="todo"), [chunk("请点击确认卡。"), chunk(finish="stop")]])
    events = message(client, sid, text=text, **({"remember_scope": "family"} if scope.get("family_id") else {}))
    results = [
        e["event"]["result"]
        for e in events
        if e["type"] == "tool_end" and e["event"].get("arguments", {}).get("action") == "delete"
    ]
    assert results and results[0].get("requires_confirmation"), events
    return results[0], sid


def confirm(client, pending):
    return client.post("/api/todo-delete-confirmations/" + pending["confirmation_id"] + "/confirm", headers=HEADERS)


def test_recycle_restore_and_permanent_purge(console):
    client, _, service = console
    item = create(client, request_id="once", checklist=[{"text": "一件事", "done": True}], status="done")
    path = "/api/todos/" + item["id"]
    assert (
        client.post(path + "/purge", headers=HEADERS, json={"revision": 1, "title": item["title"]}).status_code == 422
    )
    deleted = client.delete(path + "?revision=1", headers=HEADERS).json()
    assert deleted["trashed"] and deleted["revision"] == 2
    assert client.get("/api/todos").json()["total"] == 0
    trash = client.get("/api/todos?trashed=true").json()["items"][0]
    assert trash["deleted_by"] == "chat_default" and not trash["can_edit"]
    assert update(client, trash, status="pending").status_code == 409
    assert client.post(path + "/restore", headers=HEADERS, json={"revision": 1}).status_code == 409
    restored = client.post(path + "/restore", headers=HEADERS, json={"revision": 2}).json()
    assert restored["completed_at"] == item["completed_at"] and restored["status"] == "done"
    assert restored["checklist"] == item["checklist"] and not restored["deleted_at"]
    assert [e["action"] for e in client.get(path).json()["events"]] == ["restored", "trashed", "created"]
    client.delete(path + "?revision=3", headers=HEADERS)
    assert client.post(path + "/purge", headers=HEADERS, json={"revision": 4, "title": "wrong"}).status_code == 422
    assert client.post(path + "/purge", headers=HEADERS, json={"revision": 4, "title": item["title"]}).json()["purged"]
    assert client.get(path).status_code == 404
    with service.access.db() as db:
        assert db.execute("SELECT COUNT(*) FROM todo_events WHERE todo_id=?", (item["id"],)).fetchone()[0] == 0
    assert (
        client.post(
            "/api/todos",
            headers=HEADERS,
            json={
                "title": item["title"],
                "request_id": "once",
                "checklist": [{"text": "一件事", "done": True}],
                "status": "done",
            },
        ).status_code
        == 410
    )


def test_chat_negative_quote_cannot_delete_and_requires_real_click(console):
    client, _, service = console
    login(client, "alice")
    item = create(client)
    pending, sid = proposal(console, item, text="不要删除这条待办", user_id="alice")
    status_path = "/api/todo-delete-confirmations/" + pending["confirmation_id"]
    assert client.get(status_path).json()["state"] == "pending"
    assert service.todos.get("alice", item["id"])["deleted_at"] == ""
    assert "deleted" not in pending and pending["todo"]["id"] == item["id"]
    # The model cannot call the human confirmation endpoint through its tool.
    denied = todo_tool.execute(
        service,
        {"user_id": "alice", "family_id": "", "device_id": ""},
        {"action": "confirm", "confirmation_id": pending["confirmation_id"]},
    )
    assert "error" in denied
    login(client, "bob")
    assert confirm(client, pending).status_code == 404
    assert client.get(status_path).status_code == 404
    login(client, "alice")
    assert client.post("/api/todo-delete-confirmations/" + pending["confirmation_id"] + "/confirm").status_code == 403
    assert confirm(client, pending).json()["trashed"]
    assert client.delete(status_path, headers=HEADERS).status_code == 409
    assert client.get(status_path).json() == {"state": "confirmed", "trashed": True}
    restored = client.post("/api/todos/" + item["id"] + "/restore", headers=HEADERS, json={"revision": 2}).json()
    assert restored["revision"] == 3
    assert confirm(client, pending).json() == {"already_confirmed": True, "id": item["id"], "trashed": False}
    assert not service.todos.get("alice", item["id"])["deleted_at"]
    assert client.get(status_path).json() == {"state": "confirmed", "trashed": False}


@pytest.mark.parametrize("change,code", [("revision", 409), ("expired", 410), ("cancel", 404), ("turn_removed", 410)])
def test_confirmation_stale_expired_cancelled_or_source_removed(console, change, code):
    client, _, service = console
    login(client, "alice")
    item = create(client)
    pending, sid = proposal(console, item, user_id="alice")
    if change == "revision":
        assert update(client, item, title="变更后的待办").status_code == 200
    elif change == "expired":
        with service.access.db() as db:
            db.execute("UPDATE todo_delete_confirmations SET expires_at='2000-01-01T00:00:00+00:00'")
    elif change == "cancel":
        assert client.delete("/api/todo-delete-confirmations/" + pending["confirmation_id"], headers=HEADERS).json()[
            "cancelled"
        ]
    else:
        with service.access.db() as db:
            db.execute("DELETE FROM chat_turns WHERE session_id=?", (sid,))
    assert confirm(client, pending).status_code == code
    assert not service.todos.get("alice", item["id"])["deleted_at"]


def test_concurrent_confirmation_only_one_trash_event(console):
    client, _, service = console
    login(client, "alice")
    item = create(client)
    pending, _ = proposal(console, item, user_id="alice")
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda _: service.todos.confirm_delete("alice", pending["confirmation_id"]), range(2)))
    assert sum(bool(r.get("already_confirmed")) for r in replies) == 1
    assert service.todos.get("alice", item["id"])["revision"] == 2


def test_trash_permissions_for_private_family_assignee_and_outsider(console):
    client, _, service = console
    setup_family(client)
    private = create(client)
    shared = create(client, family_id="home", visibility="family", assignee_user_id="bob")
    for item in [private, shared]:
        client.delete("/api/todos/" + item["id"] + "?revision=1", headers=HEADERS)
    login(client, "bob")
    assert {r["id"] for r in client.get("/api/todos?trashed=true").json()["items"]} == {shared["id"]}
    for action, body in [("restore", {"revision": 2}), ("purge", {"revision": 2, "title": shared["title"]})]:
        assert client.post("/api/todos/" + shared["id"] + "/" + action, headers=HEADERS, json=body).status_code == 403
    login(client, "outsider")
    assert client.get("/api/todos?trashed=true").json()["total"] == 0
    assert client.get("/api/todos/" + shared["id"]).status_code == 404


def test_self_departure_unassigns_all_shared_records_and_revokes_access(console):
    client, chat, service = console
    setup_family(client)
    assigned = create(client, family_id="home", visibility="family", assignee_user_id="bob")
    trashed = create(client, family_id="home", visibility="family", assignee_user_id="bob")
    service.todos.delete("alice", trashed["id"], 1)
    login(client, "bob")
    private = create(client, family_id="home", device_id="robot1")
    owned = create(client, family_id="home", visibility="family")
    pending, sid = proposal(console, owned, user_id="bob", family_id="home")
    response = client.post("/api/identity/families/home/leave", headers=HEADERS, json={})
    assert response.status_code == 200 and response.json()["unassigned_todos"] == 2
    assert client.get("/api/identity/me").json()["families"] == []
    assert client.get("/api/identity/me").json()["devices"] == []
    assert client.get("/api/todos/" + owned["id"]).status_code == 404
    assert client.get("/api/todos/" + private["id"]).status_code == 200
    assert confirm(client, pending).status_code == 404
    with pytest.raises(HTTPException) as denied:
        service.access.require_context("bob", "home", "robot1")
    assert denied.value.status_code == 403
    login(client, "alice")
    current = client.get("/api/todos/" + assigned["id"]).json()
    assert current["assignee_user_id"] == "" and current["revision"] == 2
    assert current["events"][0]["action"] == "member_left"
    assert update(client, current, status="done").status_code == 200
    restored = client.post("/api/todos/" + trashed["id"] + "/restore", headers=HEADERS, json={"revision": 3})
    assert restored.status_code == 200 and restored.json()["assignee_user_id"] == ""
    # Rejoining never revives the old deletion confirmation or assignment.
    service.access.add_member("alice", "home", "bob")
    login(client, "bob")
    assert confirm(client, pending).status_code == 404


def test_member_removal_owner_handoff_and_last_member_guard(console):
    client, _, service = console
    setup_family(client)
    item = create(client, family_id="home", visibility="family", assignee_user_id="alice")
    login(client, "outsider")
    assert client.delete("/api/identity/families/home/members/bob", headers=HEADERS).status_code == 403
    login(client, "bob")
    assert client.delete("/api/identity/families/home/members/alice", headers=HEADERS).status_code == 403
    login(client, "alice")
    for body in [{}, {"successor_user_id": "outsider"}, {"successor_user_id": "alice"}]:
        assert client.post("/api/identity/families/home/leave", headers=HEADERS, json=body).status_code == 422
    assert (
        client.post("/api/identity/families/home/leave", headers=HEADERS, json={"successor_user_id": "bob"}).status_code
        == 200
    )
    assert client.get("/api/todos/" + item["id"]).status_code == 404
    assert (
        client.post("/api/identity/families/home/members", headers=HEADERS, json={"user_id": "outsider"}).status_code
        == 403
    )
    login(client, "bob")
    profile = client.get("/api/identity/me").json()
    assert profile["families"][0]["owner_user_id"] == "bob"
    current = client.get("/api/todos/" + item["id"]).json()
    assert current["can_manage"] and current["assignee_user_id"] == ""
    assert update(client, current, status="done").status_code == 200
    assert client.post("/api/identity/families/home/leave", headers=HEADERS, json={}).status_code == 422
    service.access.add_member("bob", "home", "outsider")
    assert client.delete("/api/identity/families/home/members/outsider", headers=HEADERS).status_code == 200
    with service.access.db() as db:
        assert db.execute("SELECT COUNT(*) FROM app_membership_events").fetchone()[0] == 2


def test_departure_serializes_active_chat_and_preserves_private_data(console):
    client, chat, service = console
    setup_family(client)
    sid = session(client, user_id="bob", family_id="home")
    chat.runs[sid] = object()
    try:
        assert client.post("/api/identity/families/home/leave", headers=HEADERS, json={}).status_code == 409
    finally:
        chat.runs.pop(sid)
    assert service.access.require_context("bob", "home")
    assert client.post("/api/identity/families/home/leave", headers=HEADERS, json={}).status_code == 200
    from .chat import MessageInput

    with pytest.raises(HTTPException) as failure:
        chat.start(sid, MessageInput(text="继续聊天", request_id=uuid.uuid4()))
    assert failure.value.status_code == 403


def test_pre_upgrade_orphan_assignment_can_be_repaired(console):
    client, _, service = console
    setup_family(client)
    item = create(client, family_id="home", visibility="family", assignee_user_id="bob")
    with service.access.db() as db:
        db.execute("DELETE FROM app_members WHERE family_id='home' AND user_id='bob'")
    response = update(client, item, status="done")
    assert response.status_code == 200 and response.json()["assignee_user_id"] == ""


def test_old_todo_schema_upgrade_keeps_records(console):
    from .todos import Todos

    client, _, service = console
    item = create(client, title="升级前的事项", status="done")
    with service.access.db() as db:
        db.execute("ALTER TABLE todos DROP COLUMN deleted_at")
        db.execute("ALTER TABLE todos DROP COLUMN deleted_by")
    service.todos = Todos(service)
    result = service.todos.get("chat_default", item["id"])
    assert result["created_at"] == item["created_at"] and result["completed_at"] == item["completed_at"]
    assert result["revision"] == 1 and result["deleted_at"] == ""
    assert service.todos.create("chat_default", {"title": "升级后"})["revision"] == 1


def test_v4_migration_keeps_trash_and_departure_but_revokes_confirmations(console, tmp_path):
    import sqlite3
    from . import migration as m
    from .test_migration import settings

    client, _, service = console
    setup_family(client)
    item = create(client, family_id="home", visibility="family", assignee_user_id="bob")
    second = create(client)
    pending, _ = proposal(console, second, user_id="alice")
    client.delete("/api/todos/" + item["id"] + "?revision=1", headers=HEADERS)
    assert client.delete("/api/identity/families/home/members/bob", headers=HEADERS).status_code == 200
    root = service.settings.data_dir.parent
    package, manifest = m.export_bundle(root, settings(root), client=service.memory.client, materials=service.root)
    assert manifest["version"] == 4
    target = tmp_path / "restored"
    m.restore_bundle(package, settings(target), target)
    with sqlite3.connect(target / "data/dashboard/chat.sqlite") as db:
        result = db.execute(
            "SELECT deleted_at,assignee_user_id,revision FROM todos WHERE id=?", (item["id"],)
        ).fetchone()
        assert result[0] and result[1:] == ("", 3)
        assert db.execute("SELECT COUNT(*) FROM todo_delete_confirmations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM app_membership_events").fetchone()[0] == 1
        assert (
            db.execute("SELECT COUNT(*) FROM app_members WHERE user_id='bob' AND family_id='home'").fetchone()[0] == 0
        )
    # Export must not revoke the original live machine's pending request.
    assert confirm(client, pending).status_code == 200
