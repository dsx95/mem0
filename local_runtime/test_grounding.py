import json
import threading
import time

import pytest

from local_runtime.grounding import (
    INSUFFICIENT,
    INVALID,
    checked_answer,
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


def extract(identity="real", quote="愿景\n让智能机器人走进每个家庭。"):
    return {"status": "supported", "citations": [{"id": identity, "quote": quote}]}


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
    assert "让智能机器人走进每个家庭" in answer and SOURCE in answer and "第 1 页" in answer
    assert grounding["status"] == "verified"
    assert grounding["citations"][0]["quote"] in ORIGINAL


@pytest.mark.parametrize("payload", [
    extract(identity="invented"),
    extract(quote="愿景：让每个家庭，拥有可生长的记忆。"),
    extract(quote="愿景……让智能机器人走进每个家庭。"),
    {**extract(), "answer": "伪造自由回答"},
    {"status": "supported", "citations": []},
    {"status": "insufficient", "citations": [{"id": "real", "quote": "fake"}]},
])
def test_fabricated_citations_and_answers_are_rejected(payload):
    with pytest.raises(ValueError):
        checked_answer(payload, [evidence()], "愿景")


def test_real_quote_of_wrong_field_does_not_pass():
    with pytest.raises(ValueError, match="所问字段"):
        checked_answer(extract(quote="Slogan\n让机器人成为有温度的家庭伙伴，把时间还给生活、陪伴与创造。"), [evidence()], "愿景")


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
    assert "让智能机器人走进每个家庭" in events[-1]["answer"]
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
    model = Model([function({"action": "search", "text": "愿景", "scope": "library"}), batch(extract("fake")), batch(extract("fake"))])
    chat.client_factory = lambda: model
    events = message(client, session(client), "你的愿景是什么？")
    assert events[-1]["answer"] == INVALID
    assert events[-1]["grounding"]["status"] == "validation_failed" and len(model.calls) == 3


def test_no_evidence_is_not_a_claim_of_absence_from_all_documents(console):
    client, chat, _ = console
    chat.client_factory = lambda: Model([function({"action": "search", "text": "保修期", "scope": "library"})])
    result = message(client, session(client), "产品保修期是多少年？")
    assert result[-1]["answer"] == INSUFFICIENT
    assert result[-1]["grounding"]["status"] == "insufficient"


def test_lookup_failure_does_not_become_a_not_found_answer(console):
    client, chat, service = console
    def broken(*args, **kwargs):
        raise RuntimeError("never-return-me")
    service.memory.search = broken
    chat.client_factory = lambda: Model([function({"action": "search", "text": "愿景", "scope": "library"})])
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
    chat.client_factory = lambda: Model([function({"action": "search", "text": "愿景", "scope": "library"}), delayed()])
    sid = session(client)
    result = []
    worker = threading.Thread(target=lambda: result.extend(message(client, sid, "你的愿景是什么？")))
    worker.start()
    assert gate.wait(3)
    client.post(f"/api/chat/sessions/{sid}/cancel", headers={"X-Memory-Client": "dashboard"})
    worker.join(4)
    assert not worker.is_alive()
    assert result[-1]["status"] == "cancelled" and result[-1]["answer"] == ""
