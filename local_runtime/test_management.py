import json
import uuid

import httpx

from local_runtime.chat import Chat
from local_runtime.rerank import RerankConfig, Reranker
from local_runtime.test_chat import Model, chunk, message
from local_runtime.test_dashboard import HEADERS, login

pytest_plugins = ("local_runtime.test_chat",)


def post(client, path, body):
    response = client.post(path, json=body, headers=HEADERS)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def setup_family(client):
    login(client, "alice")
    post(client, "/api/identity/families", {"family_id": "home", "name": "我们的家"})
    post(client, "/api/identity/families/home/members", {"user_id": "bob"})
    post(client, "/api/identity/devices", {"family_id": "home", "device_id": "robot1", "name": "客厅机器人"})
    post(client, "/api/identity/devices", {"family_id": "home", "device_id": "robot2"})


def note(client, text, **kwargs):
    return post(client, "/api/manage/notes", {"text": text, **kwargs})["results"][0]["id"]


def session(client, user="alice", device="robot1"):
    login(client, user)
    return post(client, "/api/chat/sessions", {"user_id": user, "family_id": "home", "device_id": device})["id"]


def test_cookie_required_and_cross_tab_identity_rejected(console):
    client, _, _ = console
    client.cookies.clear()
    for path in ["/api/overview", "/api/memories", "/api/search?q=x", "/api/jobs", "/api/sources", "/api/chat/users", "/api/manage/diaries"]:
        assert client.get(path).status_code == 401
    assert client.get("/api/identity/users").status_code == 200
    login(client, "alice")
    assert client.get("/api/overview", headers={"X-Memory-User": "bob"}).status_code == 409
    assert client.get("/api/chat/sessions?user_id=bob").status_code == 403


def test_membership_not_inferred_from_client_ids_and_devices_scoped(console):
    client, _, _ = console
    setup_family(client)
    login(client, "outsider")
    for body in [{"user_id": "outsider", "family_id": "home"}, {"user_id": "outsider", "device_id": "robot1"}]:
        assert client.post("/api/chat/sessions", json=body, headers=HEADERS).status_code == 403
    assert client.post("/api/identity/families/home/members", json={"user_id": "outsider"}, headers=HEADERS).status_code == 403
    assert client.post("/api/identity/families", json={"family_id": "home"}, headers=HEADERS).status_code == 409
    assert client.get("/api/manage/memories?family_id=home").status_code == 403
    login(client, "bob")
    assert client.post("/api/identity/devices", json={"family_id": "home", "device_id": "injected"}, headers=HEADERS).status_code == 403
    assert client.post("/api/chat/sessions", json={"user_id": "alice", "family_id": "home"}, headers=HEADERS).status_code == 403
    assert {d["device_id"] for d in client.get("/api/identity/me").json()["devices"]} == {"robot1", "robot2"}


def test_private_shared_builtin_and_devices_visible_without_cross_user_leaks(console):
    client, _, service = console
    setup_family(client)
    private = note(client, "Alice 秘密", family_id="home", device_id="robot1")
    shared = note(client, "家庭周末计划", family_id="home", device_id="robot1", scope="family")
    builtin = note(client, "机器人说明", family_id="home", device_id="robot2", scope="family", memory_type="builtin")
    assert client.get("/api/manage/memories?device_id=robot1").json()["total"] == 2
    login(client, "bob")
    items = client.get("/api/manage/memories?family_id=home").json()["items"]
    assert {i["id"] for i in items} == {shared, builtin}
    assert all(not i["can_delete"] for i in items)
    for path in [f"/api/memories/{private}", f"/api/memories/{private}/file"]:
        assert client.get(path).status_code == 404
    assert client.delete(f"/api/memories/{private}", headers=HEADERS).status_code == 404
    assert client.delete(f"/api/memories/{shared}", headers=HEADERS).status_code == 403
    assert private not in {i["id"] for i in client.get("/api/search?q=秘密").json()["items"]}
    assert all(i["user_id"] != "alice" for i in client.get("/api/memories?user_id=alice").json()["items"])
    assert len(service.snapshot()) == 28


def test_same_device_does_not_share_sessions_or_diaries(console):
    client, chat, _ = console
    setup_family(client)
    alice = session(client)
    chat.client_factory = lambda: Model([[chunk("Alice 的回答"), chunk(finish="stop")]])
    message(client, alice, text="普通问候")
    bob = session(client, "bob")
    for suffix in ["", "/memories", "/diary", "/diary/days", "/diary.md"]:
        assert client.get(f"/api/chat/sessions/{alice}" + suffix).status_code == 404
    assert client.delete(f"/api/chat/sessions/{alice}", headers=HEADERS).status_code == 404
    assert client.get("/api/manage/conversations").json()["items"][0]["id"] == bob
    assert client.get("/api/manage/diaries").json()["items"] == []


def test_device_in_messages_memories_and_filtered_diary(console):
    client, chat, _ = console
    setup_family(client)
    first, second = session(client), session(client, device="robot2")
    chat.client_factory = lambda: Model([[chunk("好的"), chunk(finish="stop")]])
    message(client, first, text="今天在客厅", device_id="robot1")
    message(client, second, text="今天在卧室", device_id="robot2")
    detail = client.get(f"/api/chat/sessions/{first}").json()
    assert detail["device_id"] == detail["turns"][0]["input_context"]["device_id"] == "robot1"
    memory = chat.execute(chat.session(first), {"action": "remember", "text": "喜欢茶"})
    record = client.get('/api/memories/' + memory["memories"][0]["id"]).json()
    assert record["device_id"] == "robot1"
    assert client.post(f"/api/chat/sessions/{first}/messages", headers=HEADERS,
        json={"text": "x", "request_id": str(uuid.uuid4()), "device_id": "robot2"}).status_code == 409
    day = client.get(f"/api/chat/sessions/{first}/diary?device_id=robot1").json()
    assert day["turn_count"] == 1 and day["entries"][0]["device_id"] == "robot1" and day["summary"] == ""
    grouped = client.get("/api/manage/diaries?device_id=robot2").json()["items"]
    assert len(grouped) == 1 and grouped[0]["turn_count"] == 1


def test_hidden_memories_do_not_reach_retrieval_or_reranker_and_restore(console):
    client, chat, service = console
    setup_family(client)
    sid = session(client)
    secret = note(client, "隐藏的说明不能送入模型", family_id="home", memory_type="builtin", scope="family")
    other = note(client, "公开的说明", family_id="home", memory_type="builtin", scope="family")
    note(client, "一个个人偏好", family_id="home")
    post(client, f"/api/memories/{secret}/visibility", {"hidden": True})
    requests = []
    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert not any("隐藏的说明" in text for text in body["documents"])
        return httpx.Response(200, json={"results": [{"index": i, "relevance_score": .9-i*.01} for i in range(body["top_n"])]})
    service.reranker.close()
    service.reranker = Reranker(RerankConfig(enabled=True, api_key="fake"), transport=httpx.MockTransport(respond))
    result = chat.execute(chat.session(sid), {"action": "search", "text": "说明", "scope": "all"})
    assert requests and secret not in {i["id"] for i in result["memories"]}
    assert other in {i["id"] for i in result["memories"]}
    assert client.get(f"/api/memories/{secret}").status_code == 404
    assert any(i["id"] == secret and i["hidden"] for i in client.get("/api/manage/memories?include_hidden=true").json()["items"])
    post(client, f"/api/memories/{secret}/visibility", {"hidden": False})
    assert client.get(f"/api/memories/{secret}").status_code == 200


def test_device_builtin_not_recalled_on_other_device(console):
    client, chat, _ = console
    setup_family(client)
    sid = session(client)
    first = note(client, "客厅设备说明", family_id="home", device_id="robot1", scope="family", memory_type="builtin")
    second = note(client, "卧室设备说明", family_id="home", device_id="robot2", scope="family", memory_type="builtin")
    result = chat.execute(chat.session(sid), {"action": "search", "text": "设备说明", "scope": "library"})
    assert first in {i["id"] for i in result["memories"]}
    assert second not in {i["id"] for i in result["memories"]}


def test_delete_memory_removes_vector_history_and_is_repeat_safe(console):
    from mem0.memory.storage import SQLiteManager
    client, chat, service = console
    setup_family(client)
    sid = session(client)
    result = chat.execute(chat.session(sid), {"action": "remember", "text": "待删除偏好"})
    mid = result["memories"][0]["id"]
    history = SQLiteManager(str(service.directory / 'test-history.sqlite'))
    service.memory.db = history
    history.add_history(mid, None, "待删除偏好", "ADD")
    try:
        assert client.delete(f"/api/memories/{mid}", headers=HEADERS).status_code == 200
        assert history.get_history(mid) == []
        assert client.get(f"/api/memories/{mid}").status_code == 404
        assert not service.memory.client.retrieve(service.settings.collection, [mid])
        assert client.delete(f"/api/memories/{mid}", headers=HEADERS).status_code == 200
        assert "待删除偏好" not in json.dumps(chat.execute(chat.session(sid), {"action": "search", "text": "偏好"}), ensure_ascii=False)
    finally:
        history.close()
        service.memory.db = None


def test_delete_turn_and_diary_remove_originals_and_never_backfill(console):
    client, chat, service = console
    setup_family(client)
    sid = session(client)
    chat.client_factory = lambda: Model([[chunk("好的"), chunk(finish="stop")]])
    message(client, sid, text="需要删除的对话")
    turn = client.get(f"/api/chat/sessions/{sid}").json()["turns"][0]
    assert client.delete(f"/api/chat/sessions/{sid}/turns/{turn['id']}", headers=HEADERS).json()["deleted_turns"] == 1
    assert chat.context(sid) == []
    with chat.db() as db:
        chat.diary.backfill(db)
        assert db.execute("SELECT count(*) FROM daily_diary_entries").fetchone()[0] == 0
    message(client, sid, text="新一轮")
    assert client.delete(f"/api/chat/sessions/{sid}/diary", headers=HEADERS).json()["deleted_turns"] == 1
    assert client.get(f"/api/chat/sessions/{sid}/diary").json()["turn_count"] == 0
    with chat.db() as db:
        chat.diary.backfill(db)
        assert db.execute("SELECT count(*) FROM daily_diaries").fetchone()[0] == 0
    restarted = Chat(service)
    try:
        assert restarted.detail(sid)["turns"] == []
    finally:
        restarted.close()


def test_private_upload_cannot_be_read_or_retried_by_other_user(console):
    from local_runtime.test_dashboard import finish
    client, _, _ = console
    setup_family(client)
    response = client.post('/api/uploads?filename=private.txt&family_id=home&device_id=robot1', content=b'private source', headers=HEADERS)
    assert response.status_code == 202
    job = finish(client, response.json()["id"])
    assert job["status"] == "complete"
    record = client.get('/api/manage/memories?memory_type=builtin').json()["items"][0]
    assert record["device_id"] == 'robot1' and record["scope"] == 'personal'
    login(client, 'bob')
    assert client.get('/api/jobs').json()["items"] == []
    assert client.get('/api/sources').json()["items"] == []
    assert client.get(f'/api/memories/{record["id"]}/file').status_code == 404
    assert client.post(f'/api/jobs/{job["id"]}/retry', headers=HEADERS).status_code == 404
    assert client.post('/api/uploads?filename=x.txt&user_id=alice', content=b'x', headers=HEADERS).status_code == 403


def test_profiles_persist_and_bootstrap_does_not_add_members_implicitly(console):
    from local_runtime.access import Access
    client, _, service = console
    setup_family(client)
    access = Access(service.directory / 'chat.sqlite')
    assert len(access.profile('bob')['devices']) == 2
    access.bootstrap([{'user_id':'mallory','metadata':{'family_id':'home'}}])
    assert not any(u['user_id']=='mallory' for u in access.users())
    assert client.get('/').status_code == 200
    assert 'manage.js' in client.get('/').text
    assert 'device-id' in client.get('/chat').text


def test_manual_notes_use_legacy_binding_and_reach_chat(console):
    client, chat, service = console
    login(client, 'alice')
    for item in [i for i in service.snapshot() if i['user_id'] == 'alice'][1:]:
        service.memory.delete(item['id'])
    service.refresh()
    mid = note(client, '新增的个人偏好应当可以被聊天找到')
    sid = post(client, '/api/chat/sessions', {'user_id':'alice','use_library':False})['id']
    assert chat.session(sid)['memory_user_id'] == 'alice'
    found = chat.execute(chat.session(sid), {'action':'search','text':'个人偏好','scope':'personal'})
    item = client.get('/api/memories/'+mid).json()
    assert item['user_id'] == 'alice'
    assert mid in {item['id'] for item in found['memories']}


def test_deleted_day_cannot_be_overwritten_by_an_old_inflight_summary(console):
    client, chat, _ = console
    setup_family(client)
    sid = session(client)
    chat.client_factory = lambda: Model([[chunk('好的'),chunk(finish='stop')]])
    message(client, sid, text='旧记录')
    with chat.db() as db:
        old_version = db.execute('SELECT source_version FROM daily_diaries').fetchone()[0]
    client.delete(f'/api/chat/sessions/{sid}/diary',headers=HEADERS)
    message(client, sid, text='新记录')
    with chat.db() as db:
        count = db.execute("UPDATE daily_diaries SET summary='过期摘要' WHERE source_version=?",(old_version,)).rowcount
    assert count == 0
    assert client.get(f'/api/chat/sessions/{sid}/diary').json()['summary'] != '过期摘要'


def test_memory_deletion_waits_for_active_chat_and_keeps_shared_record(console):
    from types import SimpleNamespace
    client, chat, _ = console
    setup_family(client)
    mid = note(client, '共享事实', family_id='home',scope='family')
    chat.runs['running-test'] = SimpleNamespace()
    try:
        assert client.delete('/api/memories/'+mid, headers=HEADERS).status_code == 409
        assert client.get('/api/memories/'+mid).status_code == 200
    finally:
        chat.runs.pop('running-test')
