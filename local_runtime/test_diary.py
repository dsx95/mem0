import json
import sqlite3
import time
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from local_runtime.diary import diary_date
from local_runtime.access import Access
from local_runtime.test_dashboard import login
from local_runtime.test_chat import Model, chunk, function, message, session

pytest_plugins = ("local_runtime.test_chat",)


def test_diary_date_uses_china_calendar_day():
    assert diary_date("2026-09-24T15:59:59+00:00") == "2026-09-24"
    assert diary_date("2026-09-24T16:00:00+00:00") == "2026-09-25"


def test_diary_collects_all_sessions_and_new_memories_but_stays_private(console):
    client, chat, _ = console
    alice = session(client, user_id="alice", family_id="home1", use_library=False)
    another = session(client, user_id="alice", family_id="home1", use_library=False)
    bob = session(client, user_id="bob", family_id="home1", use_library=False)
    other_home = session(client, user_id="alice", family_id="home2", use_library=False)
    chat.client_factory = lambda: Model([
        function({"action": "remember", "text": "我喜欢无糖拿铁", "subject": "本人", "attribute": "饮品偏好"}),
        [chunk("已记住。"), chunk(finish="stop")],
    ])
    message(client, alice, "请记住：我喜欢无糖拿铁")
    chat.client_factory = lambda: Model([[chunk("明天见。"), chunk(finish="stop")]])
    message(client, another, "明天见")
    message(client, bob, "Bob 的私人事情")
    message(client, other_home, "另一个家庭的事情")

    path = f"/api/chat/sessions/{alice}/diary"
    result = client.get(path).json()
    assert result["date"] == datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert result["turn_count"] == 2
    assert [entry["user_text"] for entry in result["entries"]] == ["请记住：我喜欢无糖拿铁", "明天见"]
    assert result["entries"][0]["new_memories"][0]["text"] == "我喜欢无糖拿铁"
    assert result["summary_status"] == "pending"
    login(client, "bob")
    assert client.get(f"/api/chat/sessions/{bob}/diary").json()["turn_count"] == 1
    login(client, "alice")
    assert client.get(f"/api/chat/sessions/{other_home}/diary").json()["turn_count"] == 1
    assert chat.execute(chat.session(bob), {"action": "diary", "text": "today"})["turn_count"] == 1
    assert "请记住" not in json.dumps(chat.execute(chat.session(bob), {"action": "diary", "text": "today"}), ensure_ascii=False)
    assert client.get(f"/api/chat/sessions/{alice}/diary/days").json()["items"][0]["turn_count"] == 2
    assert "明天见" in client.get(f"/api/chat/sessions/{alice}/diary.md").text
    assert client.get(path, params={"date": "2026-99-99"}).status_code == 422


def test_diary_summary_is_grounded_in_turn_ids_and_survives_reopen(console):
    client, chat, service = console
    sid = session(client, user_id="alice", use_library=False)
    chat.client_factory = lambda: Model([[chunk("好的。"), chunk(finish="stop")]])
    message(client, sid, "我现在更喜欢喝茶")
    turn_id = chat.detail(sid)["turns"][0]["id"]

    class DiaryModel:
        def __init__(self):
            self.chat = NS(completions=self)
            self.calls = []
        def create(self, **kwargs):
            self.calls.append(kwargs)
            payload = {"summary": "用户今天说现在更喜欢喝茶。", "new_preferences": [
                {"text": "现在更喜欢喝茶", "turn_id": turn_id},
                {"text": "不应收录的偏好", "turn_id": "other-user-turn"}]}
            return NS(choices=[NS(message=NS(content=json.dumps(payload, ensure_ascii=False)))])
        def close(self):
            pass

    model = DiaryModel()
    chat.diary_client_factory = lambda: model
    result = client.post(f"/api/chat/sessions/{sid}/diary/summarize", headers={"X-Memory-Client": "dashboard"}).json()
    assert result["summary_status"] == "complete"
    assert result["summary"] == "用户今天说现在更喜欢喝茶。"
    assert result["new_preferences"] == [{"text": "现在更喜欢喝茶", "turn_id": turn_id}]
    assert model.calls[0]["response_format"] == {"type": "json_object"}
    with sqlite3.connect(chat.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM daily_diary_entries").fetchone()[0] == 1
        assert db.execute("SELECT summary_status FROM daily_diaries").fetchone()[0] == "complete"
    from local_runtime.chat import Chat
    reopened = Chat(service)
    with reopened.db() as db:
        assert reopened.diary.get(db, reopened.session(sid), "today")["summary"] == result["summary"]
    reopened.close()


def test_background_summary_failure_keeps_raw_diary_and_can_retry(tmp_path):
    from local_runtime.chat import Chat, SessionInput
    from datetime import timezone

    settings = NS(diary_auto_summary=True, llm=NS(model="fake"), max_tokens=1024, preset="qwen")
    access = Access(tmp_path / "chat.sqlite")
    access.register("alice", "Alice")
    dashboard = NS(directory=tmp_path, settings=settings, snapshot=lambda: [], access=access)
    chat = Chat(dashboard)
    current = chat.create(SessionInput(user_id="alice"))

    class Broken:
        def __init__(self):
            self.chat = NS(completions=self)
        def create(self, **kwargs):
            raise RuntimeError("model unavailable")
        def close(self):
            pass

    chat.diary_client_factory = Broken
    stamp = datetime.now(timezone.utc).isoformat()
    with chat.db() as db:
        db.execute("""
            INSERT INTO chat_turns
              (id,session_id,request_id,user_text,answer,status,events,protocol,error,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?)
        """, ("turn", current["id"], "request", "今天出门散步", "好的", "complete", "[]", "[]", None, stamp))
        turn = db.execute("SELECT * FROM chat_turns WHERE id='turn'").fetchone()
        day = chat.diary.record(db, current, turn)
    chat.diary_queue.put(("alice", "", day))
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        with chat.db() as db:
            diary = chat.diary.get(db, current, day)
        if diary["summary_status"] == "failed":
            break
        time.sleep(.05)
    assert diary["summary_status"] == "failed"
    assert diary["entries"][0]["user_text"] == "今天出门散步"

    class Working(Broken):
        def create(self, **kwargs):
            payload = {"summary": "用户今天出门散步。", "new_preferences": []}
            return NS(choices=[NS(message=NS(content=json.dumps(payload, ensure_ascii=False)))])

    chat.diary_client_factory = Working
    assert chat.summarize_diary(current, day)["summary_status"] == "complete"
    chat.close()


def test_deleting_chat_removes_its_diary_copy_but_preserves_other_sessions(console):
    client, chat, _ = console
    first = session(client, user_id="alice", use_library=False)
    second = session(client, user_id="alice", use_library=False)
    chat.client_factory = lambda: Model([[chunk("A"), chunk(finish="stop")]])
    message(client, first, "第一段")
    chat.client_factory = lambda: Model([[chunk("B"), chunk(finish="stop")]])
    message(client, second, "第二段")
    with chat.db() as db:
        db.execute("UPDATE daily_diaries SET summary='包含第一段的旧摘要', summary_status='complete'")
    assert client.delete(f"/api/chat/sessions/{first}", headers={"X-Memory-Client": "dashboard"}).status_code == 200
    day = client.get(f"/api/chat/sessions/{second}/diary").json()
    assert [entry["user_text"] for entry in day["entries"]] == ["第二段"]
    assert day["summary"] == ""
    assert day["summary_status"] == "pending"
    assert client.delete(f"/api/chat/sessions/{second}", headers={"X-Memory-Client": "dashboard"}).status_code == 200
    third = session(client, user_id="alice", use_library=False)
    assert client.get(f"/api/chat/sessions/{third}/diary").json()["turn_count"] == 0


def test_existing_chat_turns_are_backfilled_into_dated_diary(console):
    from local_runtime.chat import Chat

    client, chat, service = console
    sid = session(client, user_id="alice", family_id="home1", use_library=False)
    with chat.db() as db:
        db.execute("""
            INSERT INTO chat_turns
              (id,session_id,request_id,user_text,answer,status,events,protocol,error,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?)
        """, ("historical", sid, "historical-request", "旧对话", "旧回答", "complete",
              "[]", "[]", None, "2026-09-24T16:01:00+00:00"))
    reopened = Chat(service)
    with reopened.db() as db:
        diary = reopened.diary.get(db, reopened.session(sid), "2026-09-25")
    assert diary["turn_count"] == 1
    assert diary["entries"][0]["user_text"] == "旧对话"
    reopened.close()
