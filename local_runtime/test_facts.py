"""Failure, concurrency, tenancy and lifecycle tests with real SQLite/Qdrant."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from local_runtime.facts import Facts
from local_runtime.test_chat import session
from local_runtime.test_dashboard import HEADERS, login
from local_runtime.test_management import setup_family

pytest_plugins = ("local_runtime.test_chat",)


def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def create(client, text="喜欢牛奶", **extra):
    response = client.post(
        "/api/manage/notes", headers=HEADERS, json={"text": text, "subject": "本人", "attribute": "饮品偏好", **extra}
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def get(client, mid):
    response = client.get("/api/manage/facts/" + mid)
    assert response.status_code == 200, response.text
    return response.json()


def update(client, mid, **extra):
    return client.patch(
        "/api/manage/facts/" + mid,
        headers=HEADERS,
        json={"revision": get(client, mid)["revision"], "text": "改喝燕麦奶", "reason": "用户确认", **extra},
    )


def test_change_correction_late_fact_and_conflict_resolution(console):
    client, _, service = console
    mid = create(client, occurred_at=ago(4))
    result = update(client, mid, occurred_at=ago(3))
    assert result.status_code == 200
    versions = get(client, mid)["versions"]
    assert versions[1]["status"] == "superseded" and versions[1]["valid_to"] == versions[0]["effective_at"]
    result = update(client, mid, text="其实喜欢豆奶", occurred_at=ago(2), kind="correction")
    assert result.status_code == 200
    assert get(client, mid)["versions"][1]["status"] == "retracted"
    result = update(client, mid, text="迟到的牛奶记录", occurred_at=ago(5))
    assert result.json()["fact_status"] == "disputed"
    assert mid not in {i["id"] for i in service.snapshot()}
    current = get(client, mid)
    response = client.post(
        f"/api/manage/facts/{mid}/resolve",
        headers=HEADERS,
        json={"revision": current["revision"], "version": current["versions"][0]["version"], "reason": "保留旧观测"},
    )
    assert response.status_code == 409
    response = client.post(
        f"/api/manage/facts/{mid}/resolve",
        headers=HEADERS,
        json={
            "revision": current["revision"],
            "version": current["current_version"],
            "reason": "迟到记录不能覆盖当前偏好",
        },
    )
    assert response.status_code == 200
    assert get(client, mid)["memory"] == "其实喜欢豆奶"
    assert get(client, mid)["versions"][0]["status"] == "rejected"


def test_same_slot_conflicts_dedup_and_user_scope(console):
    client, _, _ = console
    mid = create(client)
    assert create(client) == mid
    assert len(get(client, mid)["versions"]) == 1
    assert create(client, "不再喝牛奶") == mid
    assert get(client, mid)["fact_status"] == "disputed"
    assert client.get("/api/manage/memories?fact_status=disputed").json()["total"] == 1
    login(client, "other")
    second = create(client)
    assert second != mid
    assert client.get("/api/manage/facts/" + mid).status_code == 404
    assert client.delete("/api/memories/" + mid, headers=HEADERS).status_code == 404


def test_same_value_on_different_objects_is_not_deduplicated(console):
    client, _, _ = console
    first = create(client, "在客厅", subject="红色杯子", attribute="位置")
    second = create(client, "在客厅", subject="蓝色杯子", attribute="位置")
    assert first != second


def test_expected_revision_rejects_concurrent_writers(console):
    client, _, service = console
    mid = create(client)
    user = client.get("/api/identity/me").json()["user_id"]
    row = get(client, mid)

    def write(text):
        try:
            return service.facts.submit(
                user=user, key=row["user_id"], text=text, mid=mid, expected_revision=1, kind="change", trusted=True
            )["status"]
        except Exception as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["变化 A", "变化 B"]))
    assert sorted(map(str, results)) == ["409", "active"]
    assert len(get(client, mid)["versions"]) == 2


def test_index_failure_survives_restart_and_never_serves_stale_text(console):
    client, chat, service = console
    sid = session(client)
    mid = create(client)
    service.facts.stop()
    original = service.memory.embedding_model.embed

    def broken(*args):
        raise TimeoutError("secret-upstream-detail")

    service.memory.embedding_model.embed = broken
    assert update(client, mid).status_code == 200
    service.facts.sync_one(mid)
    assert get(client, mid)["sync_status"] == "pending"
    result = chat.execute(chat.session(sid), {"action": "search", "text": "饮品", "scope": "personal"})
    assert any(i["text"] == "改喝燕麦奶" for i in result["memories"])
    assert "喜欢牛奶" not in json.dumps(result, ensure_ascii=False)
    with service.access.db() as db:
        task = dict(db.execute("SELECT * FROM fact_outbox WHERE fact_id=?", (mid,)).fetchone())
        assert task["attempts"] == 1 and task["error_type"] == "TimeoutError"
    service.memory.embedding_model.embed = original
    service.facts = Facts(service)
    assert service.facts.sync_one(mid)
    payload = service.memory.client.retrieve(service.settings.collection, [mid])[0].payload
    assert payload["data"] == "改喝燕麦奶" and payload["fact_revision"] == 2
    assert get(client, mid)["sync_status"] == "done"


def test_conflicts_retracted_and_hidden_never_reach_reranker(console):
    client, chat, service = console
    sid = session(client)
    mid = create(client)
    create(client, "我讨厌牛奶")
    result = chat.execute(chat.session(sid), {"action": "search", "text": "牛奶", "scope": "all"})
    assert mid not in {i["id"] for i in result["memories"]}
    assert result["conflicts"][0]["id"] == mid
    detail = get(client, mid)
    assert (
        client.post(
            f"/api/manage/facts/{mid}/retract", headers=HEADERS, json={"revision": detail["revision"]}
        ).status_code
        == 200
    )
    assert get(client, mid)["fact_status"] == "retracted"
    assert mid not in {i["id"] for i in service.snapshot()}
    assert client.post(f"/api/memories/{mid}/visibility", headers=HEADERS, json={"hidden": True}).status_code == 200
    assert client.get(f"/api/manage/records/{mid}").status_code == 200


def test_delete_erases_versions_candidates_and_cannot_resurrect(console):
    client, _, service = console
    mid = create(client, "删除原文 secret A")
    create(client, "删除候选 secret B")
    service.facts.stop()
    assert client.delete("/api/memories/" + mid, headers=HEADERS).status_code == 200
    assert client.delete("/api/memories/" + mid, headers=HEADERS).status_code == 200
    with service.access.db() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_versions WHERE fact_id=?", (mid,)).fetchone()[0] == 0
        assert db.execute("SELECT text FROM facts WHERE id=?", (mid,)).fetchone()[0] == ""
    assert not service.memory.client.retrieve(service.settings.collection, [mid])
    # Simulate a stale index point restored/replayed after deletion.
    from qdrant_client.models import PointStruct

    service.memory.client.upsert(
        service.settings.collection,
        [PointStruct(id=mid, vector=[1.0, 0.0, 0.0], payload={"data": "secret A", "user_id": "chat_default"})],
    )
    service.refresh()
    assert client.get("/api/manage/facts/" + mid).status_code == 404
    assert mid not in {i["id"] for i in service.snapshot(all_states=True)}


def test_family_members_can_flag_but_only_owner_resolves(console):
    client, _, service = console
    setup_family(client)
    mid = create(client, "周末九点出门", subject="家庭", attribute="周末安排", family_id="home", scope="family")
    login(client, "bob")
    assert create(client, "周末十点出门", subject="家庭", attribute="周末安排", family_id="home", scope="family") == mid
    row = get(client, mid)
    assert row["fact_status"] == "disputed" and not row["can_delete"]
    result = service.facts.submit(
        user="bob",
        key=row["user_id"],
        family="home",
        visibility="family",
        mid=mid,
        expected_revision=row["revision"],
        text="另一个待确认的时间",
    )
    assert result["status"] == "disputed"
    assert get(client, mid)["memory"] == "周末九点出门"
    assert (
        client.post(
            f"/api/manage/facts/{mid}/resolve",
            headers=HEADERS,
            json={"revision": row["revision"], "version": 1, "reason": "test"},
        ).status_code
        == 403
    )
    assert client.delete("/api/memories/" + mid, headers=HEADERS).status_code == 403
    login(client, "outsider")
    assert client.get("/api/manage/records/" + mid).status_code == 404


def test_legacy_classification_and_slot_collision(console):
    client, _, _ = console
    login(client, "alice")
    legacy = client.get("/api/manage/memories?memory_type=longterm").json()["items"][0]
    response = update(client, legacy["id"], text=legacy["memory"], subject="本人", attribute="偏好")
    assert response.status_code == 200 and response.json()["revision"] == 2
    other = create(client, "另一个事实", attribute="已占用")
    assert update(client, legacy["id"], subject="本人", attribute="已占用").status_code == 409
    assert get(client, other)["fact_status"] == "active"


def test_shared_first_person_facts_keep_member_identity(console):
    client, _, _ = console
    setup_family(client)
    alice = create(client, family_id="home", scope="family")
    login(client, "bob")
    bob = create(client, "喜欢豆奶", subject="我", family_id="home", scope="family")
    assert alice != bob
    assert get(client, alice)["subject"] == "user:alice"
    assert get(client, bob)["subject"] == "user:bob"
    assert get(client, alice)["fact_status"] == get(client, bob)["fact_status"] == "active"


def test_bootstrap_preserves_legacy_content_ids_and_both_timestamps(console):
    client, _, service = console
    login(client, "alice")
    original = {
        "id": "5755ee1a-893d-4ae5-8f20-ae2bf9f7be34",
        "user_id": "alice",
        "memory": "旧事实",
        "metadata": {},
        "created_at": ago(10),
        "updated_at": ago(2),
    }
    service.facts.bootstrap([original])
    service.facts.bootstrap([{**original, "memory": "不应覆盖已有权威记录"}])
    detail = get(client, original["id"])
    for field in ("id", "memory", "created_at", "updated_at"):
        assert detail[field] == original[field]
    assert detail["subject"] == detail["attribute"] == ""
    assert detail["versions"][0]["kind"] == "legacy"
    assert detail["sync_status"] == "done"


def test_chat_proposal_source_evidence_and_no_silent_overwrite(console):
    client, chat, _ = console
    sid = session(client)
    mid = create(client)
    context = {**chat.session(sid), "_user_text": "现在不喝牛奶", "_turn_id": "test-turn"}
    args = {"action": "remember", "text": "不喝牛奶", "memory_id": mid, "revision": 1, "source_quote": "现在不喝牛奶"}
    result = chat.execute(context, args)
    assert not result["saved"] and result["requires_confirmation"]
    assert get(client, mid)["fact_status"] == "disputed"
    bad = chat.execute(context, {**args, "source_quote": "捏造用户原话"})
    assert "error" in bad


def test_bad_dates_and_failed_queue_retry_are_visible(console):
    client, _, service = console
    mid = create(client)
    assert update(client, mid, occurred_at="2039-01-01T00:00:00Z").status_code == 422
    assert update(client, mid, occurred_at="2026-01-01T00:00:00").status_code == 422
    service.facts.stop()
    with service.access.db() as db:
        db.execute(
            "UPDATE fact_outbox SET status='failed',attempts=8,error_type='TimeoutError' WHERE fact_id=?", (mid,)
        )
    assert get(client, mid)["sync_status"] == "failed"
    assert client.post(f"/api/manage/facts/{mid}/retry", headers=HEADERS, json={}).status_code == 200
    assert service.facts.sync_one(mid)
    assert get(client, mid)["sync_status"] == "done"


def test_burst_updates_coalesce_to_one_durable_task_and_final_value(console):
    client, _, service = console
    mid = create(client)
    service.facts.stop()
    user = client.get("/api/identity/me").json()["user_id"]
    row = get(client, mid)
    for i in range(30):
        service.facts.submit(
            user=user,
            key=row["user_id"],
            mid=mid,
            expected_revision=i + 1,
            text=f"当前设定 {i}",
            kind="change",
            trusted=True,
        )
    with service.access.db() as db:
        assert db.execute("SELECT COUNT(*) FROM fact_outbox WHERE fact_id=?", (mid,)).fetchone()[0] == 1
        assert db.execute("SELECT revision FROM fact_outbox WHERE fact_id=?", (mid,)).fetchone()[0] == 31
    service.facts.sync_one(mid)
    assert service.memory.client.retrieve(service.settings.collection, [mid])[0].payload["data"] == "当前设定 29"


def test_late_embedding_cannot_overwrite_new_revision_or_resurrect_delete(console):
    import threading

    client, _, service = console
    service.facts.stop()
    mid = create(client)
    entered, release = threading.Event(), threading.Event()
    original = service.memory.embedding_model.embed

    def slow(*args):
        entered.set()
        assert release.wait(5)
        return [1.0, 0.0, 0.0]

    service.memory.embedding_model.embed = slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(service.facts.sync_one, mid)
        assert entered.wait(5)
        assert update(client, mid, text="模型调用期间的新版本").status_code == 200
        release.set()
        job.result()
    row = service.memory.client.retrieve(service.settings.collection, [mid])
    assert not row  # stale first version was never committed
    service.memory.embedding_model.embed = original
    service.facts.sync_one(mid)
    assert service.memory.client.retrieve(service.settings.collection, [mid])[0].payload["fact_revision"] == 2
    # Repeat with a delete while a new embedding is in flight.
    assert update(client, mid, text="将被删除的新版本").status_code == 200
    entered.clear()
    release.clear()
    service.memory.embedding_model.embed = slow
    with ThreadPoolExecutor(max_workers=1) as pool:
        job = pool.submit(service.facts.sync_one, mid)
        assert entered.wait(5)
        assert client.delete("/api/memories/" + mid, headers=HEADERS).status_code == 200
        release.set()
        job.result()
    service.memory.embedding_model.embed = original
    service.facts.sync_one(mid)
    assert not service.memory.client.retrieve(service.settings.collection, [mid])


def test_pending_fact_and_versions_survive_logical_migration(console, tmp_path):
    from local_runtime import migration as m
    from local_runtime.test_migration import settings as migration_settings
    from pathlib import Path
    import sqlite3

    client, _, service = console
    service.facts.stop()
    mid = create(client)
    create(client, "不喝牛奶")
    root = service.settings.data_dir.parent
    config = migration_settings(root)
    package, manifest = m.export_bundle(root, config, client=service.memory.client, materials=service.root)
    assert manifest["version"] == 5 and manifest["counts"]["fact_versions"] == 27
    target = tmp_path / "restored-facts"
    m.restore_bundle(package, migration_settings(target), target)
    with sqlite3.connect(Path(target, "data/dashboard/chat.sqlite")) as db:
        assert db.execute("SELECT status FROM facts WHERE id=?", (mid,)).fetchone()[0] == "disputed"
        assert db.execute("SELECT COUNT(*) FROM fact_versions WHERE fact_id=?", (mid,)).fetchone()[0] == 2
        assert db.execute("SELECT status FROM fact_outbox WHERE fact_id=?", (mid,)).fetchone()[0] == "pending"


def test_task_monitor_and_retry_are_user_scoped(console):
    client, _, service = console
    service.facts.stop()
    mid = create(client)
    tasks = client.get("/api/manage/tasks").json()["items"]
    assert tasks[0]["fact_id"] == mid and tasks[0]["can_retry"]
    login(client, "outsider")
    assert client.get("/api/manage/tasks").json()["items"] == []
    assert client.post(f"/api/manage/facts/{mid}/retry", headers=HEADERS, json={}).status_code == 404
