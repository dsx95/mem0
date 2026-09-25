"""Configure independent API/local LLM and embedding endpoints without changing Mem0."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

from .presets import PRESETS
from .rerank import DEFAULT_URL, RerankConfig, Reranker, accepts_shared_qwen_key

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROVIDERS = {"openai", "ollama"}


@dataclass(frozen=True)
class Endpoint:
    provider: str
    base_url: str
    model: str
    api_key: str = field(repr=False)

    def validate(self, label: str) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError(f"MEM0_{label}_PROVIDER must be openai or ollama")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"MEM0_{label}_BASE_URL must be an HTTP(S) base URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"MEM0_{label}_BASE_URL must not contain credentials, query parameters or fragments")
        if parsed.path.rstrip("/").endswith(("/chat/completions", "/embeddings", "/api/chat", "/api/embed")):
            raise ValueError(f"MEM0_{label}_BASE_URL must be the base URL, not an endpoint path")
        if not self.model or self.model.startswith("CHANGE_ME"):
            raise ValueError(f"Set MEM0_{label}_MODEL to a model served by this endpoint")
        if self.provider == "openai" and not self.api_key:
            raise ValueError(f"Set MEM0_{label}_API_KEY; use local-unused only for a service without authentication")

    def describe(self) -> dict:
        # Never display URL userinfo/query or the API key, even for invalid configurations.
        parsed = urlsplit(self.base_url)
        host = parsed.hostname or ""
        try:
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            port = ""
        if ":" in host:
            host = f"[{host}]"
        return {
            "provider": self.provider,
            "base_url": f"{parsed.scheme}://{host}{port}{parsed.path}",
            "model": self.model,
            "api_key_set": bool(self.api_key),
        }


@dataclass(frozen=True)
class Settings:
    llm: Endpoint
    embedding: Endpoint
    embedding_dims: int
    send_embedding_dimensions: bool
    data_dir: Path
    collection: str
    timeout: float
    max_retries: int
    temperature: float
    max_tokens: int
    is_reasoning_model: bool | None
    custom_instructions: str
    env_file: Path
    preset: str = ""
    diary_auto_summary: bool = True
    rerank: RerankConfig = field(default_factory=RerankConfig)

    def validate(self, component: str = "all") -> None:
        if component in {"all", "rerank"}:
            self.rerank.validate()
        if self.preset and not self.llm.api_key:
            raise ValueError("Set MEM0_PROVIDER_API_KEY in the selected profile; it is shared by LLM and embedding")
        if component in {"all", "llm"}:
            self.llm.validate("LLM")
            if self.llm.provider == "openai" and os.getenv("OPENROUTER_API_KEY"):
                raise ValueError(
                    "Unset OPENROUTER_API_KEY for this process: Mem0's openai adapter otherwise overrides the endpoint. "
                    "To use OpenRouter, set MEM0_LLM_BASE_URL and MEM0_LLM_API_KEY explicitly instead."
                )
        if component in {"all", "embedding"}:
            self.embedding.validate("EMBEDDING")
            if self.embedding_dims <= 0:
                raise ValueError("MEM0_EMBEDDING_DIMS must match the model's positive output dimension")

    def llm_config(self) -> dict:
        config = {
            "model": self.llm.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": 1.0,
        }
        if self.llm.provider == "openai":
            config.update(api_key=self.llm.api_key, openai_base_url=self.llm.base_url)
            if self.is_reasoning_model is not None:
                config["is_reasoning_model"] = self.is_reasoning_model
        else:
            config["ollama_base_url"] = self.llm.base_url
        return {"provider": self.llm.provider, "config": config}

    def embedding_config(self) -> dict:
        config = {"model": self.embedding.model}
        if self.embedding.provider == "openai":
            config.update(api_key=self.embedding.api_key, openai_base_url=self.embedding.base_url)
            # Most compatible embedding servers have a fixed output dimension and reject this field.
            if self.send_embedding_dimensions:
                config["embedding_dims"] = self.embedding_dims
        else:
            config.update(ollama_base_url=self.embedding.base_url, embedding_dims=self.embedding_dims)
        return {"provider": self.embedding.provider, "config": config}

    def memory_config(self) -> dict:
        self.validate()
        return {
            "llm": self.llm_config(),
            "embedder": self.embedding_config(),
            "vector_store": {
                "provider": "qdrant",
                "config": {
                    "path": str(self.data_dir / "qdrant"),
                    "collection_name": self.collection,
                    "embedding_model_dims": self.embedding_dims,
                },
            },
            "history_db_path": str(self.data_dir / f"{self.collection}_history.db"),
            "custom_instructions": self.custom_instructions or None,
        }

    def describe(self) -> dict:
        errors = []
        for component in ("llm", "embedding", "rerank"):
            try:
                self.validate(component)
            except ValueError as exc:
                errors.append(str(exc))
        return {
            "env_file": str(self.env_file),
            "preset": self.preset or None,
            "llm": self.llm.describe(),
            "embedding": self.embedding.describe(),
            "rerank": self.rerank.describe(),
            "embedding_dims": self.embedding_dims,
            "send_embedding_dimensions": self.send_embedding_dimensions,
            "data_dir": str(self.data_dir),
            "collection": self.collection,
            "timeout_seconds": self.timeout,
            "max_retries": self.max_retries,
            "configured": not errors,
            "configuration_errors": errors,
        }


def _bool(value: str, key: str) -> bool:
    if value.lower() in {"true", "1", "yes"}:
        return True
    if value.lower() in {"false", "0", "no"}:
        return False
    raise ValueError(f"{key} must be true or false")


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Read a profile; MEM0_* process environment overrides file values. No network calls."""
    path = Path(env_file) if env_file is not None else PROJECT_ROOT / ".env"
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Configuration file not found: {path}")
    values = {key: value or "" for key, value in dotenv_values(path, interpolate=False).items()}
    values.update({key: value for key, value in os.environ.items() if key.startswith("MEM0_")})
    preset = values.get("MEM0_PRESET", "").strip().lower()
    if preset:
        if preset not in PRESETS:
            raise ValueError("MEM0_PRESET must be openai or qwen")
        # A preset pins both endpoints so old per-component environment variables
        # cannot silently mix platforms or send the shared key to another host.
        values.update({f"MEM0_{key}": value for key, value in PRESETS[preset].items()})
        shared_key = values.get("MEM0_PROVIDER_API_KEY", "").strip()
        values.update(MEM0_LLM_API_KEY=shared_key, MEM0_EMBEDDING_API_KEY=shared_key)

    def get(key, default=""):
        return values.get(f"MEM0_{key}", default).strip()

    def endpoint(label):
        return Endpoint(
            provider=get(f"{label}_PROVIDER", "openai").lower(),
            base_url=get(f"{label}_BASE_URL").rstrip("/"),
            model=get(f"{label}_MODEL"),
            api_key=get(f"{label}_API_KEY"),
        )

    llm, embedding = endpoint("LLM"), endpoint("EMBEDDING")
    dims = int(get("EMBEDDING_DIMS", "1536"))
    identity = [embedding.provider, embedding.base_url, embedding.model, dims]
    fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:12]
    collection = get("COLLECTION") or f"mem0_{fingerprint}"
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", collection):
        raise ValueError("MEM0_COLLECTION must use 1-128 letters, digits, underscores or hyphens")
    data_dir = Path(get("DATA_DIR", ".data")).expanduser()
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    reasoning = get("LLM_IS_REASONING_MODEL", "auto")
    timeout = float(get("REQUEST_TIMEOUT", "30"))
    max_retries = int(get("MAX_RETRIES", "1"))
    temperature = float(get("LLM_TEMPERATURE", "0.1"))
    max_tokens = int(get("LLM_MAX_TOKENS", "2048"))
    if not math.isfinite(timeout) or timeout <= 0 or max_retries < 0:
        raise ValueError("MEM0_REQUEST_TIMEOUT must be positive and MEM0_MAX_RETRIES must be nonnegative")
    if not math.isfinite(temperature) or not 0 <= temperature <= 2 or max_tokens <= 0:
        raise ValueError("MEM0_LLM_TEMPERATURE must be in [0, 2] and MEM0_LLM_MAX_TOKENS must be positive")
    rerank_url = get("RERANK_URL", DEFAULT_URL)
    rerank_key = get("RERANK_API_KEY")
    if not rerank_key and preset == "qwen" and accepts_shared_qwen_key(rerank_url):
        rerank_key = get("PROVIDER_API_KEY")
    rerank = RerankConfig(
        enabled=_bool(get("RERANK_ENABLED", "false"), "MEM0_RERANK_ENABLED"),
        url=rerank_url, model=get("RERANK_MODEL", "qwen3-rerank"), api_key=rerank_key,
        timeout=float(get("RERANK_TIMEOUT", "3")), candidates=int(get("RERANK_CANDIDATES", "30")),
        top_n=int(get("RERANK_TOP_N", "8")),
    )
    return Settings(
        llm=llm,
        embedding=embedding,
        embedding_dims=dims,
        send_embedding_dimensions=_bool(get("EMBEDDING_SEND_DIMENSIONS", "false"), "MEM0_EMBEDDING_SEND_DIMENSIONS"),
        data_dir=data_dir.resolve(),
        collection=collection,
        timeout=timeout,
        max_retries=max_retries,
        temperature=temperature,
        max_tokens=max_tokens,
        is_reasoning_model=None if reasoning == "auto" else _bool(reasoning, "MEM0_LLM_IS_REASONING_MODEL"),
        custom_instructions=get("CUSTOM_INSTRUCTIONS"),
        env_file=path,
        diary_auto_summary=_bool(get("DIARY_AUTO_SUMMARY", "true"), "MEM0_DIARY_AUTO_SUMMARY"),
        preset=preset,
        rerank=rerank,
    )


def _prepare(settings: Settings) -> None:
    # Set these before importing mem0: it reads them once, at import time.
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MEM0_DIR", str(settings.data_dir / "sdk"))
    os.environ.setdefault("MEM0_TELEMETRY", "false")
    os.environ.setdefault("FASTEMBED_CACHE_PATH", str(PROJECT_ROOT / ".data" / "models" / "fastembed"))


def _ollama_models_present(endpoint: Endpoint, timeout: float) -> None:
    """Fail on a missing model instead of letting Mem0's embedding adapter download one."""
    if endpoint.provider != "ollama":
        return
    import httpx

    response = httpx.get(f"{endpoint.base_url}/api/tags", timeout=timeout)
    response.raise_for_status()
    names = {row.get("name", row.get("model", "")) for row in response.json().get("models", [])}
    target = endpoint.model if ":" in endpoint.model else f"{endpoint.model}:latest"
    normalized = {name if ":" in name else f"{name}:latest" for name in names}
    if target not in normalized:
        raise ValueError(
            f"Ollama model {endpoint.model!r} is not installed on the configured server; pull it there first"
        )


def _set_request_options(provider, endpoint: Endpoint, settings: Settings, component: str):
    if endpoint.provider == "openai":
        provider.client = provider.client.with_options(timeout=settings.timeout, max_retries=settings.max_retries)
    else:
        from ollama import Client

        provider.client = Client(host=endpoint.base_url, timeout=settings.timeout)
    if settings.preset == "qwen":
        if component == "llm":
            # Qwen-Plus JSON extraction uses non-thinking mode.
            provider.generate_response = partial(provider.generate_response, extra_body={"enable_thinking": False})
        else:
            # text-embedding-v4 accepts at most ten inputs per request.
            original_embed_batch = provider.embed_batch

            def embed_batch(texts, memory_action="add"):
                texts = list(texts)
                vectors = []
                for start in range(0, len(texts), 10):
                    vectors.extend(original_embed_batch(texts[start : start + 10], memory_action=memory_action))
                return vectors

            provider.embed_batch = embed_batch
    return provider


def create_llm(settings: Settings | None = None):
    """Return the existing Mem0 LLM adapter; call generate_response(messages=...)."""
    settings = settings or load_settings()
    settings.validate("llm")
    _prepare(settings)
    _ollama_models_present(settings.llm, settings.timeout)
    from mem0.utils.factory import LlmFactory

    spec = settings.llm_config()
    return _set_request_options(LlmFactory.create(spec["provider"], spec["config"]), settings.llm, settings, "llm")


def create_embedder(settings: Settings | None = None):
    settings = settings or load_settings()
    settings.validate("embedding")
    _prepare(settings)
    _ollama_models_present(settings.embedding, settings.timeout)
    from mem0.utils.factory import EmbedderFactory

    spec = settings.embedding_config()
    return _set_request_options(
        EmbedderFactory.create(spec["provider"], spec["config"], vector_config=None),
        settings.embedding,
        settings,
        "embedding",
    )


def create_memory(settings: Settings | None = None):
    """Return an OSS Memory instance using local source and project-local Qdrant/SQLite."""
    settings = settings or load_settings()
    settings.validate()
    _prepare(settings)
    _ollama_models_present(settings.llm, settings.timeout)
    _ollama_models_present(settings.embedding, settings.timeout)
    from mem0 import Memory

    memory = Memory.from_config(settings.memory_config())
    _set_request_options(memory.llm, settings.llm, settings, "llm")
    _set_request_options(memory.embedding_model, settings.embedding, settings, "embedding")
    memory.runtime_reranker = Reranker(settings.rerank)
    return memory


def close_memory(memory) -> None:
    """Release SQLite and the local Qdrant lock so another instance can reopen it."""
    if getattr(memory, "runtime_reranker", None):
        memory.runtime_reranker.close()
    memory.close()
    memory.vector_store.client.close()
