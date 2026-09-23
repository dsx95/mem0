"""Exercise real provider HTTP clients and real local memory storage with fake upstreams."""

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from local_runtime import close_memory, create_embedder, create_llm, create_memory, load_settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    for key in list(os.environ):
        if key.startswith("MEM0_") or key == "OPENROUTER_API_KEY":
            monkeypatch.delenv(key)
    monkeypatch.setenv("MEM0_TELEMETRY", "false")
    monkeypatch.setenv("MEM0_DIR", str(tmp_path / "sdk"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@contextmanager
def upstream():
    state = {"requests": [], "status": 200, "dims": 4, "models": ["local-chat:latest", "local-embed:latest"]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, result):
            data = json.dumps(result).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            state["requests"].append((self.path, None, self.headers.get("Authorization")))
            self.respond({"models": [{"name": name, "model": name} for name in state["models"]]})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, body, self.headers.get("Authorization")))
            if state["status"] != 200:
                self.respond({"error": {"message": "upstream failed", "type": "test_error", "code": "test_error"}})
                return
            if self.path.endswith(("/embeddings", "/api/embed")):
                texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
                vectors = [[1.0] + [0.0] * (state["dims"] - 1) for _ in texts]
                if self.path.endswith("/api/embed"):
                    self.respond({"model": body["model"], "embeddings": vectors})
                else:
                    self.respond(
                        {
                            "object": "list",
                            "model": body["model"],
                            "data": [
                                {"object": "embedding", "index": i, "embedding": vector}
                                for i, vector in enumerate(vectors)
                            ],
                            "usage": {"prompt_tokens": 1, "total_tokens": 1},
                        }
                    )
                return
            content = json.dumps({"memory": [{"id": "0", "text": "用户不吃香菜", "event": "ADD"}]}, ensure_ascii=False)
            if self.path.endswith("/api/chat"):
                self.respond(
                    {"model": body["model"], "message": {"role": "assistant", "content": content}, "done": True}
                )
            else:
                self.respond(
                    {
                        "id": "test-chat",
                        "object": "chat.completion",
                        "created": 1,
                        "model": body["model"],
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    }
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def profile(tmp_path, llm_url, embedding_url, provider="openai", **overrides):
    values = {
        "MEM0_LLM_PROVIDER": provider,
        "MEM0_LLM_BASE_URL": llm_url,
        "MEM0_LLM_MODEL": "local-chat",
        "MEM0_LLM_API_KEY": "llm-test-key",
        "MEM0_EMBEDDING_PROVIDER": provider,
        "MEM0_EMBEDDING_BASE_URL": embedding_url,
        "MEM0_EMBEDDING_MODEL": "local-embed",
        "MEM0_EMBEDDING_API_KEY": "embedding-test-key",
        "MEM0_EMBEDDING_DIMS": "4",
        "MEM0_DATA_DIR": str(tmp_path / "data"),
        "MEM0_MAX_RETRIES": "0",
        "MEM0_REQUEST_TIMEOUT": "2",
    }
    values.update(overrides)
    path = tmp_path / "profile.env"
    path.write_text("\n".join(f"{k}={v}" for k, v in values.items()) + "\n")
    return path


def test_openai_llm_and_embedding_use_independent_addresses_and_keys(tmp_path):
    with upstream() as (llm_url, llm_server), upstream() as (embedding_url, embedding_server):
        settings = load_settings(profile(tmp_path, llm_url + "/v1", embedding_url + "/v1"))
        result = create_llm(settings).generate_response(
            [{"role": "user", "content": "Return a JSON fact"}], response_format={"type": "json_object"}
        )
        assert json.loads(result)["memory"][0]["text"] == "用户不吃香菜"
        assert create_embedder(settings).embed_batch(["hello", "world"]) == [[1, 0, 0, 0], [1, 0, 0, 0]]
        path, body, auth = llm_server["requests"][0]
        assert path == "/v1/chat/completions" and auth == "Bearer llm-test-key"
        assert body["model"] == "local-chat" and body["response_format"] == {"type": "json_object"}
        path, body, auth = embedding_server["requests"][0]
        assert path == "/v1/embeddings" and auth == "Bearer embedding-test-key"
        assert body["model"] == "local-embed" and "dimensions" not in body
        assert create_llm(settings).client.timeout == 2


def test_optional_embedding_dimensions_are_forwarded_only_when_enabled(tmp_path):
    with upstream() as (url, server):
        settings = load_settings(profile(tmp_path, url + "/v1", url + "/v1", MEM0_EMBEDDING_SEND_DIMENSIONS="true"))
        create_embedder(settings).embed("hi")
        assert server["requests"][0][1]["dimensions"] == 4


def test_memory_extraction_reopen_and_user_isolation(tmp_path):
    with upstream() as (url, server):
        settings = load_settings(profile(tmp_path, url + "/v1", url + "/v1"))
        memory = create_memory(settings)
        try:
            added = memory.add([{"role": "user", "content": "我不吃香菜"}], user_id="alice")
            assert added["results"][0]["memory"] == "用户不吃香菜"
        finally:
            close_memory(memory)
        memory = create_memory(settings)
        try:
            result = memory.search("我的饮食偏好", filters={"user_id": "alice"}, top_k=1)
            assert result["results"][0]["memory"] == "用户不吃香菜"
            assert memory.search("我的饮食偏好", filters={"user_id": "bob"})["results"] == []
            assert memory.history(result["results"][0]["id"])
        finally:
            close_memory(memory)
        # A new interpreter must recover the same memory from disk.
        reopened = subprocess.run(
            [
                sys.executable,
                "-m",
                "local_runtime",
                "--env-file",
                str(settings.env_file),
                "search",
                "饮食偏好",
                "--user-id",
                "alice",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(reopened.stdout)["results"][0]["memory"] == "用户不吃香菜"
        assert any(path.endswith("/chat/completions") for path, _, _ in server["requests"])
        assert (settings.data_dir / f"{settings.collection}_history.db").is_file()


def test_native_ollama_http_routes_and_json_format(tmp_path):
    with upstream() as (url, server):
        settings = load_settings(profile(tmp_path, url, url, provider="ollama"))
        assert json.loads(
            create_llm(settings).generate_response(
                [{"role": "user", "content": "JSON please"}], response_format={"type": "json_object"}
            )
        )["memory"]
        assert create_embedder(settings).embed_batch(["一", "二"]) == [[1, 0, 0, 0], [1, 0, 0, 0]]
        chats = [body for path, body, _ in server["requests"] if path == "/api/chat"]
        assert chats[0]["format"] == "json"
        assert all(path != "/api/pull" for path, _, _ in server["requests"])


def test_missing_ollama_model_fails_without_downloading(tmp_path):
    with upstream() as (url, server):
        server["models"] = []
        settings = load_settings(profile(tmp_path, url, url, provider="ollama"))
        with pytest.raises(ValueError, match="not installed"):
            create_embedder(settings)
        assert [path for path, _, _ in server["requests"]] == ["/api/tags"]


def test_mixed_local_llm_and_api_embedding(tmp_path):
    with upstream() as (llm_url, _), upstream() as (embedding_url, embedding_server):
        settings = load_settings(
            profile(tmp_path, llm_url, embedding_url + "/v1", provider="ollama", MEM0_EMBEDDING_PROVIDER="openai")
        )
        memory = create_memory(settings)
        try:
            assert memory.add("我不吃香菜", user_id="mixed")["results"]
        finally:
            close_memory(memory)
        assert any(path == "/v1/embeddings" for path, _, _ in embedding_server["requests"])


def test_config_is_redacted_and_does_not_call_endpoints(tmp_path):
    with upstream() as (url, server):
        path = profile(tmp_path, url + "/v1", url + "/v1")
        result = subprocess.run(
            [sys.executable, "-m", "local_runtime", "--env-file", str(path), "config"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout)["configured"]
        assert "llm-test-key" not in result.stdout and "embedding-test-key" not in result.stdout
        assert server["requests"] == []


def test_check_reports_dimension_mismatch(tmp_path):
    with upstream() as (url, server):
        server["dims"] = 3
        path = profile(tmp_path, url + "/v1", url + "/v1")
        result = subprocess.run(
            [sys.executable, "-m", "local_runtime", "--env-file", str(path), "check", "--component", "embedding"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "dimension/count" in result.stderr


def test_check_validates_both_services_without_saving_memory(tmp_path):
    with upstream() as (url, _):
        path = profile(tmp_path, url + "/v1", url + "/v1")
        result = subprocess.run(
            [sys.executable, "-m", "local_runtime", "--env-file", str(path), "check"],
            capture_output=True,
            text=True,
            check=True,
        )
        response = json.loads(result.stdout)
        assert "validated" in response["llm"] and response["embedding"]["dimensions"] == 4
        assert not (tmp_path / "data" / "qdrant").exists()


def test_failed_endpoint_is_not_replaced_by_another_provider(tmp_path):
    with upstream() as (url, server):
        server["status"] = 401
        path = profile(tmp_path, url + "/v1", url + "/v1")
        result = subprocess.run(
            [sys.executable, "-m", "local_runtime", "--env-file", str(path), "llm", "hello"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1 and "HTTP 401" in result.stderr
        assert "llm-test-key" not in result.stderr and "upstream failed" not in result.stderr
        assert len(server["requests"]) == 1


def test_chat_does_not_require_embedding_credentials(tmp_path):
    with upstream() as (url, _):
        settings = load_settings(profile(tmp_path, url + "/v1", "", MEM0_EMBEDDING_API_KEY=""))
        assert create_llm(settings).generate_response([{"role": "user", "content": "hello"}])


def test_model_switch_changes_default_collection_and_env_can_override(tmp_path, monkeypatch):
    path = profile(tmp_path, "http://localhost:1/v1", "http://localhost:2/v1")
    first = load_settings(path)
    monkeypatch.setenv("MEM0_EMBEDDING_MODEL", "another-model")
    second = load_settings(path)
    assert first.collection != second.collection
    monkeypatch.setenv("MEM0_EMBEDDING_API_KEY", "rotated-test-key")
    assert load_settings(path).collection == second.collection


def test_ambient_openrouter_key_cannot_redirect_explicit_endpoint(tmp_path, monkeypatch):
    path = profile(tmp_path, "http://localhost:1/v1", "http://localhost:2/v1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "ambient-key")
    with pytest.raises(ValueError, match="Unset OPENROUTER_API_KEY"):
        create_llm(load_settings(path))


def test_bad_profile_reports_not_ready_without_leaking_url_credentials(tmp_path):
    path = profile(tmp_path, "https://user:secret@example.com/v1?token=secret", "http://localhost:2/v1")
    settings = load_settings(path)
    description = json.dumps(settings.describe())
    assert not settings.describe()["configured"] and "secret" not in description
    assert "llm-test-key" not in repr(settings)


def test_unconfigured_template_is_not_mistaken_for_a_working_model():
    template = Path(__file__).parent / "env" / "api.env.example"
    settings = load_settings(template)
    assert not settings.describe()["configured"]


@pytest.mark.parametrize("preset,dims,batches", [("openai", 1536, [25]), ("qwen", 1024, [10, 10, 5])])
def test_single_key_profiles_send_both_models_and_memory_to_same_provider(tmp_path, monkeypatch, preset, dims, batches):
    from local_runtime.presets import PRESETS

    template = Path(__file__).parent / "env" / f"{preset}.env.example"
    path = tmp_path / f"{preset}.env"
    path.write_text(
        template.read_text().replace("MEM0_PROVIDER_API_KEY=", "MEM0_PROVIDER_API_KEY=shared-test-key")
        + f"\nMEM0_DATA_DIR={tmp_path / 'data'}\nMEM0_MAX_RETRIES=0\n"
    )
    with upstream() as (url, server):
        server["dims"] = dims
        monkeypatch.setitem(
            PRESETS, preset, {**PRESETS[preset], "LLM_BASE_URL": url + "/v1", "EMBEDDING_BASE_URL": url + "/v1"}
        )
        settings = load_settings(path)
        assert "shared-test-key" not in json.dumps(settings.describe()) + repr(settings)
        llm = create_llm(settings)
        assert json.loads(
            llm.generate_response([{"role": "user", "content": "Return JSON"}], response_format={"type": "json_object"})
        )["memory"]
        embedder = create_embedder(settings)
        texts = [f"fact {i}" for i in range(25)]
        vectors = embedder.embed_batch(texts)
        assert len(vectors) == 25 and all(len(vector) == dims for vector in vectors)
        calls = [body for route, body, _ in server["requests"] if route.endswith("/embeddings")]
        assert [len(body["input"]) for body in calls] == batches
        assert [text for body in calls for text in body["input"]] == texts
        assert embedder.embed_batch([]) == []
        memory = create_memory(settings)
        try:
            assert memory.add("我不吃香菜", user_id="alice")["results"]
            assert memory.search("饮食偏好", filters={"user_id": "alice"})["results"]
        finally:
            close_memory(memory)
        for route, body, auth in server["requests"]:
            assert auth == "Bearer shared-test-key"
            if route.endswith("/chat/completions"):
                assert body["model"] == PRESETS[preset]["LLM_MODEL"]
                if preset == "qwen":
                    assert body["enable_thinking"] is False
                else:
                    assert "enable_thinking" not in body
            else:
                assert body["model"] == PRESETS[preset]["EMBEDDING_MODEL"]
                assert body["dimensions"] == dims


@pytest.mark.parametrize("preset", ["openai", "qwen"])
def test_preset_pins_pair_and_requires_shared_key_without_ambient_fallback(tmp_path, monkeypatch, preset):
    from local_runtime.presets import PRESETS

    path = tmp_path / "preset.env"
    path.write_text(f"MEM0_PRESET={preset}\nMEM0_PROVIDER_API_KEY=\n")
    for component in ("LLM", "EMBEDDING"):
        monkeypatch.setenv(f"MEM0_{component}_BASE_URL", "https://wrong-provider.invalid/v1")
        monkeypatch.setenv(f"MEM0_{component}_API_KEY", "stale-key")
        monkeypatch.setenv(f"MEM0_{component}_MODEL", "stale-model")
    settings = load_settings(path)
    assert not settings.describe()["configured"]
    with pytest.raises(ValueError, match="MEM0_PROVIDER_API_KEY"):
        settings.validate()
    monkeypatch.setenv("MEM0_PROVIDER_API_KEY", "fresh-shared-key")
    settings = load_settings(path)
    settings.validate()
    assert settings.llm.api_key == settings.embedding.api_key == "fresh-shared-key"
    assert settings.llm.base_url == PRESETS[preset]["LLM_BASE_URL"]
    assert settings.embedding.base_url == PRESETS[preset]["EMBEDDING_BASE_URL"]
    assert settings.llm.model == PRESETS[preset]["LLM_MODEL"]
    assert settings.embedding.model == PRESETS[preset]["EMBEDDING_MODEL"]


def test_single_key_templates_are_unconfigured_and_reject_unknown_preset(tmp_path):
    for preset in ("openai", "qwen"):
        settings = load_settings(Path(__file__).parent / "env" / f"{preset}.env.example")
        assert not settings.describe()["configured"]
        assert "MEM0_PROVIDER_API_KEY" in settings.describe()["configuration_errors"][0]
    path = tmp_path / "bad.env"
    path.write_text("MEM0_PRESET=invalid-secret-value\n")
    with pytest.raises(ValueError, match="MEM0_PRESET must be openai or qwen") as error:
        load_settings(path)
    assert "invalid-secret-value" not in str(error.value)
