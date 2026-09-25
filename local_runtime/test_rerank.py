import json
import math
import os
from dataclasses import replace
from types import SimpleNamespace as NS

import httpx
import pytest

from local_runtime.rerank import DEFAULT_URL, RerankConfig, Reranker
from local_runtime.runtime import load_settings
from local_runtime.test_chat import session

pytest_plugins = ("local_runtime.test_chat",)


@pytest.fixture(autouse=True)
def clear_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MEM0_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def config(**kwargs):
    return RerankConfig(enabled=True, api_key="rerank-secret", **kwargs)


def rows(count=3):
    return [{"id": str(i), "text": f"记忆 {i}", "score": 1 - i / count,
             "scope": "personal", "created_at": "2026-09-25", "metadata": {"source": i}} for i in range(count)]


def test_disabled_makes_no_request_and_preserves_order():
    def forbidden(request):
        pytest.fail("disabled rerank must not call HTTP")
    ranker = Reranker(transport=httpx.MockTransport(forbidden))
    selected, status = ranker.rank("问题", rows(), 2)
    assert selected == rows()[:2]
    assert status == {"status": "disabled"}
    ranker.close()


def test_qwen_wire_format_maps_indices_preserves_metadata_and_uses_new_scores():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [
            {"index": 0, "relevance_score": .2},
            {"index": 2, "relevance_score": .95, "document": "must not replace local data"}]})
    ranker = Reranker(config(), transport=httpx.MockTransport(respond))
    selected, status = ranker.rank("钥匙在哪？", rows(), 2)
    assert status["status"] == "applied"
    assert [item["id"] for item in selected] == ["2", "0"]
    assert selected[0] == {**rows()[2], "rerank_score": .95}
    request = requests[0]
    assert str(request.url) == DEFAULT_URL
    assert request.headers["Authorization"] == "Bearer rerank-secret"
    assert json.loads(request.content) == {"model": "qwen3-rerank", "query": "钥匙在哪？",
                                          "documents": [row["text"] for row in rows()], "top_n": 2}
    assert request.extensions["timeout"]["read"] == 3
    ranker.close()


@pytest.mark.parametrize("results", [
    [], [{"index": 99, "relevance_score": .8}], [{"index": -1, "relevance_score": .8}],
    [{"index": True, "relevance_score": .8}], [{"index": 1, "relevance_score": "0.8"}],
    [{"index": 1, "relevance_score": 1.8}], [{"index": 1, "relevance_score": None}],
    [{"index": 1, "relevance_score": .8}, {"index": 1, "relevance_score": .9}],
])
def test_invalid_output_falls_back_without_inventing_or_dropping_memories(results):
    ranker = Reranker(config(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"results": results})))
    count = max(1, len(results))
    selected, status = ranker.rank("问题", rows(), count)
    assert selected == rows()[:count]
    assert status["status"] == "fallback"
    ranker.close()


@pytest.mark.parametrize("status_code", [302, 401, 429, 500])
def test_http_error_and_redirect_are_not_followed_or_leaked(status_code):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status_code, headers={"Location": "https://other.invalid/steal"},
                              text="rerank-secret and private memory must not be echoed")
    ranker = Reranker(config(), transport=httpx.MockTransport(respond))
    selected, status = ranker.rank("问题", rows(), 2)
    assert selected == rows()[:2] and status["status"] == "fallback"
    assert len(calls) == 1
    assert "secret" not in json.dumps(status)
    ranker.close()


def test_timeout_empty_and_single_candidate_fallback():
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("private upstream detail", request=request)
    ranker = Reranker(config(), transport=httpx.MockTransport(timeout))
    assert ranker.rank("x", [], 2)[1]["status"] == "skipped"
    assert ranker.rank("x", rows(1), 2)[0] == rows(1)
    selected, status = ranker.rank("x", rows(), 2)
    assert selected == rows()[:2] and status["status"] == "fallback"
    assert len(calls) == 1
    ranker.close()


def test_large_payload_is_bounded_without_modifying_stored_text():
    originals = [{"id": str(i), "text": "多语种🙂" * 5000} for i in range(100)]
    def respond(request):
        data = json.loads(request.content)
        qlen = len(data["query"].encode())
        assert qlen < 4000
        assert all(len(text.encode()) < 4000 for text in data["documents"])
        assert qlen * len(data["documents"]) + sum(len(text.encode()) for text in data["documents"]) <= 90000
        return httpx.Response(200, json={"results": [{"index": 99, "relevance_score": .9}]})
    ranker = Reranker(config(candidates=100), transport=httpx.MockTransport(respond))
    selected, status = ranker.rank("问题" * 5000, originals + originals, 1)
    assert status["input_truncated"] and status["candidate_count"] == 100
    assert selected[0]["text"] == originals[99]["text"]
    ranker.close()


def test_profile_switch_key_reuse_and_no_cross_provider_key_leak(tmp_path, monkeypatch):
    path = tmp_path / "qwen.env"
    path.write_text("MEM0_PRESET=qwen\nMEM0_PROVIDER_API_KEY=shared-secret\nMEM0_RERANK_ENABLED=true\n")
    settings = load_settings(path)
    settings.validate()
    assert settings.rerank.api_key == "shared-secret"
    assert "shared-secret" not in repr(settings) + json.dumps(settings.describe())
    monkeypatch.setenv("MEM0_RERANK_URL", "https://elsewhere.example/reranks")
    external = load_settings(path)
    assert external.rerank.api_key == ""
    with pytest.raises(ValueError, match="RERANK_API_KEY"):
        external.validate()
    monkeypatch.setenv("MEM0_RERANK_API_KEY", "separate-secret")
    load_settings(path).validate()
    monkeypatch.delenv("MEM0_RERANK_API_KEY")
    monkeypatch.delenv("MEM0_RERANK_URL")
    monkeypatch.setenv("MEM0_PRESET", "openai")
    assert load_settings(path).rerank.api_key == ""
    monkeypatch.setenv("MEM0_RERANK_ENABLED", "false")
    load_settings(path).validate()


@pytest.mark.parametrize("changes", [{"timeout": math.nan}, {"timeout": 0}, {"candidates": 0},
                                   {"top_n": 31}, {"url": "http://remote.example/rerank"},
                                   {"url": "https://user:password@host/rerank"}])
def test_enabled_config_rejects_bad_limits_and_credential_urls(changes):
    with pytest.raises(ValueError):
        replace(config(), **changes).validate()


def test_chat_reranks_enlarged_authorized_pool_and_does_not_resort_by_old_score(console):
    client, chat, service = console
    alice = chat.session(session(client, user_id="rerank_alice", family_id="home1", use_library=False))
    bob = chat.session(session(client, user_id="rerank_bob", family_id="home1", use_library=False))
    other = chat.session(session(client, user_id="rerank_alice", family_id="home2", use_library=False))
    for i in range(7):
        chat.execute(alice, {"action": "remember", "text": f"Alice 私有记忆 {i}"})
    chat.execute({**alice, "remember_scope": "family"}, {"action": "remember", "text": "家里共有的钥匙"})
    chat.execute(bob, {"action": "remember", "text": "Bob-private-secret"})
    chat.execute(other, {"action": "remember", "text": "Other-home-secret"})
    requests = []
    def respond(request):
        data = json.loads(request.content)
        requests.append(data)
        assert len(data["documents"]) == 8  # more than the old personal top-5
        assert "Bob-private-secret" not in json.dumps(data)
        assert "Other-home-secret" not in json.dumps(data)
        selected = data["documents"].index("家里共有的钥匙")
        return httpx.Response(200, json={"results": [{"index": selected, "relevance_score": .99}]})
    service.reranker.close()
    service.reranker = Reranker(config(top_n=1), transport=httpx.MockTransport(respond))
    result = chat.execute(alice, {"action": "search", "text": "钥匙在哪", "scope": "all"})
    assert len(requests) == 1 and result["rerank"]["status"] == "applied"
    assert result["memories"][0]["scope"] == "family" and result["count"] == 1


def test_dashboard_search_filters_before_rerank_and_keeps_provider_order(console):
    client, _, service = console
    from local_runtime.test_dashboard import login
    login(client, "alice")
    def respond(request):
        data = json.loads(request.content)
        assert len(data["documents"]) > 2
        return httpx.Response(200, json={"results": [{"index": 2, "relevance_score": .99},
                                                   {"index": 0, "relevance_score": .4}]})
    service.reranker.close()
    service.reranker = Reranker(config(), transport=httpx.MockTransport(respond))
    response = client.get("/api/search", params={"q": "test", "user_id": "alice", "limit": 2})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["rerank"]["status"] == "applied"
    assert all(row["user_id"] == "alice" for row in result["items"])
    assert [row["rerank_score"] for row in result["items"]] == [.99, .4]


def test_material_search_preserves_filters_and_reorders_by_response():
    from pathlib import Path
    from local_runtime.materials import search_materials
    calls = []
    def search(query, **kwargs):
        calls.append(kwargs)
        return {"results": [{"id": str(i), "memory": f"片段 {i}"} for i in range(9)]}
    ranker = Reranker(config(), transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"results": [{"index": 8, "relevance_score": .9}]})))
    memory = NS(search=search, runtime_reranker=ranker)
    result = search_materials(memory, "问题", Path("/materials"), "alice", top_k=1, source_files=["guide.pdf"], hybrid=True)
    assert calls[0]["top_k"] == 30
    assert calls[0]["filters"]["user_id"] == "alice"
    assert calls[0]["filters"]["OR"] == [{"source_file": "guide.pdf"}]
    assert result["results"][0]["id"] == "8"
    ranker.close()


def test_cli_check_calls_real_http_and_reports_failure(tmp_path, capsys):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from local_runtime.__main__ import main
    from unittest.mock import patch

    received = []
    state = {"code": 200}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, self.headers["Authorization"], data))
            body = json.dumps({"results": [{"index": 0, "relevance_score": .9},
                                           {"index": 1, "relevance_score": .1}]}).encode()
            self.send_response(state["code"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        path = tmp_path / "check.env"
        path.write_text(f"MEM0_RERANK_ENABLED=true\nMEM0_RERANK_API_KEY=only-rerank-key\n"
                        f"MEM0_RERANK_URL=http://127.0.0.1:{server.server_port}/reranks\n")
        args = ["local_runtime", "--env-file", str(path), "check", "--component", "rerank"]
        with patch("sys.argv", args):
            assert main() == 0
        assert json.loads(capsys.readouterr().out)["rerank"]["status"] == "applied"
        assert received[0][0:2] == ("/reranks", "Bearer only-rerank-key")
        state["code"] = 429
        with patch("sys.argv", args):
            assert main() == 2
        assert "only-rerank-key" not in capsys.readouterr().err
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
