import json
import threading
import time

import pytest

from local_runtime.grounding import (
    INSUFFICIENT,
    INVALID,
    checked_answer,
    grounding_scope,
    hybrid_library,
    needs_library,
)
from local_runtime.test_chat import Model, chunk, function, message, session
from local_runtime.test_chat import console as _console

console = _console

PUBLIC = {"user_id": "knowin_public", "use_library": True}
SOURCE = "官方资料20260917.docx"
ORIGINAL = "愿景\n让智能机器人走进每个家庭。\nSlogan\n让机器人成为有温度的家庭伙伴，把时间还给生活、陪伴与创造。"


def evidence(identity="real", text=ORIGINAL):
    return {"id": identity, "text": text, "source": SOURCE, "page": "1", "scope": "library"}


def extract(identity="real", quote="愿景\n让智能机器人走进每个家庭。", text="愿景是让智能机器人走进每个家庭"):
    return {"status": "supported", "claims": [{"text": text, "citations": [{"id": identity, "quote": quote}]}]}


def batch(payload):
    return [chunk(json.dumps(payload, ensure_ascii=False)), chunk(finish="stop")]


def seed(service):
    result = service.memory.add(ORIGINAL, user_id="knowin_public", infer=False,
                               metadata={"source_file": SOURCE, "page_label": "1", "extraction_method": "pdf_text"})
    service.refresh()
    return result["results"][0]["id"]


@pytest.mark.parametrize("query", ["你的口号是什么？", "你的slogan是什么？", "你的成立日期是哪一天？",
    "你的愿景是什么？", "你们的官网是什么？", "你融资了多少？", "请严格查阅资料库，你们的品牌官网是什么？",
    "请查阅资料库，介绍一下诺因智能的创始人，并注明资料来源。", "产品保修期多少年？"])
def test_every_company_question_requires_new_retrieval(query):
    assert needs_library(PUBLIC, query)
    assert needs_library({**PUBLIC, "user_id": "member"}, query)
    assert not needs_library({**PUBLIC, "user_id": "member", "use_library": False}, query)


@pytest.mark.parametrize("query", ["请记住我喜欢喝茶", "我最喜欢喝什么？", "查一下我们家的约定", "你好", "谢谢"])
def test_personal_memory_flow_is_preserved(query):
    assert not needs_library({**PUBLIC, "user_id": "member"}, query)


def test_memory_questions_route_by_authorized_scope():
    person = {"user_id": "member", "family_id": "home", "use_library": True}
    assert grounding_scope(person, "我最喜欢喝什么？") == "personal"
    assert grounding_scope(person, "我们家的约定是什么？") == "family"
    assert grounding_scope(person, "结合资料和我之前说过的，给个建议") == "all"
    assert grounding_scope(person, "记住我喜欢喝茶") is None
    assert grounding_scope({**person, "family_id": ""}, "我们家的约定是什么？") is None
    assert grounding_scope(person, "Python 如何排序？") is None


def test_hybrid_recovers_keyword_only_hit_without_private_memory_leak():
    snapshot = [
        {"id": "noise", "user_id": "knowin_public", "memory": "Knowin 产品有双臂", "metadata": {"source_file": "宣传.docx"}},
        {"id": "correct", "user_id": "knowin_public", "memory": "品牌官网\nhttps://example.test/", "metadata": {"source_file": SOURCE, "page_label": "3", "extraction_method": "pdf_text"}},
        {"id": "private", "user_id": "other_family", "memory": "品牌官网\n秘密私有信息", "metadata": {"source_file": SOURCE}},
    ]
    found = hybrid_library("你们的官网是什么", [{"id": "noise", "memory": "Knowin 产品有双臂", "score": .95}], snapshot)
    assert found[0]["id"] == "correct" and all(row["id"] != "private" for row in found)


def test_checked_quote_is_rendered_from_source_with_server_source_page():
    answer, grounding = checked_answer(extract(), [evidence()], "你的愿景是什么")
    assert answer == "愿景是让智能机器人走进每个家庭。"
    assert grounding["status"] == "verified"
    assert grounding["citations"][0]["source"] == SOURCE and grounding["citations"][0]["page"] == "1"
    assert grounding["citations"][0]["quote"] in ORIGINAL


@pytest.mark.parametrize("payload", [
    extract(identity="invented"),
    extract(quote="愿景：让每个家庭，拥有可生长的记忆。"),
    extract(quote="愿景……让智能机器人走进每个家庭。"),
    {**extract(), "answer": "没有依据的额外回答"},
    {"status": "supported", "claims": []},
    {"status": "insufficient", "claims": [extract()["claims"][0]]},
])
def test_fabricated_citations_and_answers_are_rejected(payload):
    with pytest.raises(ValueError):
        checked_answer(payload, [evidence()], "愿景")


def test_real_quote_of_wrong_field_does_not_pass():
    with pytest.raises(ValueError, match="所问字段"):
        checked_answer(extract(quote="Slogan\n让机器人成为有温度的家庭伙伴，把时间还给生活、陪伴与创造。"), [evidence()], "愿景")


def test_number_and_url_must_appear_in_the_claims_own_citation():
    row = evidence(text="官网 https://example.test/ 。最大负载为 6 kg。")
    with pytest.raises(ValueError, match="数字"):
        checked_answer(extract(quote="最大负载为 6 kg", text="最大负载为 8 kg"), [row], "最大负载")
    with pytest.raises(ValueError, match="网址"):
        checked_answer(extract(quote="官网 https://example.test/", text="官网是 https://wrong.test/"), [row], "官网")
    with pytest.raises(ValueError, match="数字单位"):
        checked_answer(extract(quote="最大负载为 6 kg", text="最大负载为 6 米"), [row], "最大负载")


def test_two_question_fields_can_use_different_citations():
    row = evidence()
    payload = {"status": "supported", "claims": [
        {"text": "愿景是让智能机器人走进每个家庭", "citations": [{"id": row["id"], "quote": "愿景\n让智能机器人走进每个家庭。"}]},
        {"text": "口号是让机器人成为有温度的家庭伙伴", "citations": [{"id": row["id"], "quote": "Slogan\n让机器人成为有温度的家庭伙伴"}]},
    ]}
    answer, grounding = checked_answer(payload, [row], "愿景和口号分别是什么？")
    assert "愿景" in answer and "口号" in answer and len(grounding["citations"]) == 2


def test_personal_memory_can_be_summarized_without_a_file_and_other_scope_is_rejected():
    row = {"id": "alice", "text": "我喝不加糖拿铁，也不吃香菜。", "scope": "personal", "source": None}
    payload = extract("alice", "我喝不加糖拿铁，也不吃香菜。", "你喜欢不加糖拿铁，不吃香菜")
    answer, grounding = checked_answer(payload, [row], "我喜欢什么？")
    assert answer == "你喜欢不加糖拿铁，不吃香菜。"
    assert grounding["citations"][0]["source"] == "个人记忆"
    assert grounding["citations"][0]["url"] is None
    with pytest.raises(ValueError, match="授权"):
        checked_answer(payload, [{**row, "id": "bob"}], "我喜欢什么？")
    height = {"id": "height", "text": "175cm", "scope": "personal"}
    answer, _ = checked_answer(extract("height", "175cm", "你记录的身高是175cm"), [height], "我的身高是多少？")
    assert "175cm" in answer
    english = {"id": "english", "text": "My favorite drink is black coffee", "scope": "personal"}
    answer, _ = checked_answer(extract("english", english["text"], "You prefer black coffee"), [english],
                               "What is my favorite drink?")
    assert answer == "You prefer black coffee."


def test_conflict_requires_two_real_sources_and_keeps_uncertainty():
    old = {"id": "old", "text": "2024 年的计划是周六聚餐", "scope": "family"}
    new = {"id": "new", "text": "2026 年的计划是周日聚餐", "scope": "family"}
    proposal = {"status": "conflict", "claims": [
        {"text": "2024 年记为周六聚餐", "citations": [{"id": "old", "quote": old["text"]}]},
        {"text": "2026 年记为周日聚餐，需要确认现行安排", "citations": [{"id": "new", "quote": new["text"]}]},
    ]}
    answer, grounding = checked_answer(proposal, [old, new], "家庭聚餐在哪一天？")
    assert "需要确认" in answer and grounding["status"] == "conflict"
    with pytest.raises(ValueError):
        checked_answer({**proposal, "claims": proposal["claims"][:1]}, [old, new], "家庭聚餐在哪一天？")


def test_company_turn_ignores_old_assistant_claims_and_never_streams_unverified_draft(console):
    client, chat, service = console
    identity = seed(service)
    sid = session(client, user_id="knowin_public")
    chat.client_factory = lambda: Model([[chunk("愿景是让每个家庭，拥有可生长的记忆。"), chunk(finish="stop")]])
    message(client, sid, "你好")
    model = Model([
        [chunk("不可信的提前回答")] + function({"action": "search", "text": "诺因 愿景", "scope": "library"}, finish="stop"),
        batch(extract(identity, "愿景：让家，记得你。")),
        batch(extract(identity)),
    ])
    chat.client_factory = lambda: model
    events = message(client, sid, "请严格查阅资料库，你的愿景是什么？")
    assert events[-1]["grounding"]["status"] == "verified"
    assert events[-1]["answer"] == "愿景是让智能机器人走进每个家庭。"
    assert all("可生长的记忆" not in json.dumps(call["messages"], ensure_ascii=False) for call in model.calls)
    assert all("不可信的提前回答" not in json.dumps(call["messages"], ensure_ascii=False) for call in model.calls[1:])
    assert not any("让家，记得你" in event.get("text", "") for event in events)
    assert chat.detail(sid)["turns"][-1]["grounding"]["citations"][0]["id"] == identity
    tool = next(m for m in model.calls[1]["messages"] if m["role"] == "tool")
    assert tool["tool_call_id"] == "call-one"


def test_no_tool_cannot_claim_to_have_searched(console):
    client, chat, _ = console
    chat.client_factory = lambda: Model([[chunk("我已查阅资料库，官网不存在。"), chunk(finish="stop")]])
    result = message(client, session(client), "你们的官网是什么？")
    assert result[-1]["status"] == "failed" and result[-1]["answer"] == ""


def test_bad_evidence_retries_once_then_fails_closed(console):
    client, chat, service = console
    seed(service)
    model = Model([function({"action": "search", "text": "愿景", "scope": "all"}), batch(extract("fake")), batch(extract("fake"))])
    chat.client_factory = lambda: model
    events = message(client, session(client), "你的愿景是什么？")
    assert events[-1]["answer"] == INVALID
    assert events[-1]["grounding"]["status"] == "validation_failed" and len(model.calls) == 3


def test_no_evidence_is_not_a_claim_of_absence_from_all_documents(console):
    client, chat, _ = console
    chat.client_factory = lambda: Model([function({"action": "search", "text": "保修期", "scope": "all"})])
    result = message(client, session(client), "产品保修期是多少年？")
    assert result[-1]["answer"] == INSUFFICIENT
    assert result[-1]["grounding"]["status"] == "insufficient"


def test_personal_question_uses_own_memory_and_returns_a_short_answer(console):
    client, chat, service = console
    sid = session(client, user_id="alice", use_library=False)
    other = session(client, user_id="bob", use_library=False)
    own = chat.execute(chat.session(sid), {"action": "remember", "text": "我喝不加糖拿铁，也不吃香菜。"})
    chat.execute(chat.session(other), {"action": "remember", "text": "我喜欢喝甜奶茶。"})
    identity = own["memories"][0]["id"]
    original_search = service.memory.search

    def relevant(text, filters, top_k):
        result = original_search(text, filters, 100)
        return {"results": [row for row in result["results"] if row["id"] == identity][:top_k]}

    service.memory.search = relevant
    proposal = extract(identity, "我喝不加糖拿铁，也不吃香菜。", "你喜欢不加糖拿铁，不吃香菜")
    model = Model([function({"action": "search", "text": "我喜欢喝什么", "scope": "all"}), batch(proposal)])
    chat.client_factory = lambda: model
    events = message(client, sid, "我喜欢喝什么？")
    assert events[-1]["status"] == "complete", (events[-1], len(model.calls))
    assert events[-1]["answer"] == "你喜欢不加糖拿铁，不吃香菜。", events[-1]
    assert events[-1]["grounding"]["status"] == "verified"
    assert events[-1]["grounding"]["citations"][0]["source"] == "个人记忆"
    tool = next(item for item in model.calls[1]["messages"] if item["role"] == "tool")
    assert "甜奶茶" not in tool["content"]


def test_family_question_only_uses_current_family_memory(console):
    client, chat, _ = console
    sid = session(client, user_id="alice", family_id="home1", use_library=False)
    own = chat.execute({**chat.session(sid), "remember_scope": "family"},
                       {"action": "remember", "text": "家庭约定：周六一起吃晚饭。"})
    identity = own["memories"][0]["id"]
    proposal = extract(identity, "家庭约定：周六一起吃晚饭。", "你们家约定周六一起吃晚饭")
    model = Model([function({"action": "search", "text": "家庭约定", "scope": "all"}), batch(proposal)])
    chat.client_factory = lambda: model
    events = message(client, sid, "我们家的约定是什么？")
    assert events[-1]["answer"] == "你们家约定周六一起吃晚饭。"
    assert events[-1]["grounding"]["citations"][0]["source"] == "家庭共享记忆"


def test_lookup_failure_does_not_become_a_not_found_answer(console):
    client, chat, service = console
    def broken(*args, **kwargs):
        raise RuntimeError("never-return-me")
    service.memory.search = broken
    chat.client_factory = lambda: Model([function({"action": "search", "text": "愿景", "scope": "all"})])
    events = message(client, session(client), "你的愿景是什么？")
    assert events[-1]["grounding"]["status"] == "retrieval_failed"
    assert "检索失败" in events[-1]["answer"] and "never-return-me" not in json.dumps(events)


def test_cancelling_grounded_generation_emits_no_unchecked_text(console):
    client, chat, service = console
    identity = seed(service)
    gate = threading.Event()
    def delayed():
        gate.set()
        for _ in range(100):
            time.sleep(.015)
            yield chunk("假")
        yield from batch(extract(identity))
    chat.client_factory = lambda: Model([function({"action": "search", "text": "愿景", "scope": "all"}), delayed()])
    sid = session(client)
    result = []
    worker = threading.Thread(target=lambda: result.extend(message(client, sid, "你的愿景是什么？")))
    worker.start()
    assert gate.wait(3)
    client.post(f"/api/chat/sessions/{sid}/cancel", headers={"X-Memory-Client": "dashboard"})
    worker.join(4)
    assert not worker.is_alive()
    assert result[-1]["status"] == "cancelled" and result[-1]["answer"] == ""
