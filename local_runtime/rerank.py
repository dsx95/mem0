"""Optional HTTP reranking of already authorized candidates; no database writes."""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

DEFAULT_URL = "https://dashscope.aliyuncs.com/compatible-api/v1/reranks"


def accepts_shared_qwen_key(url):
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "https" and port in {None, 443}
            and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path == "/compatible-api/v1/reranks"
            and (parsed.hostname == "dashscope.aliyuncs.com"
                 or bool(re.fullmatch(r"[a-zA-Z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com", parsed.hostname or ""))))


@dataclass(frozen=True)
class RerankConfig:
    enabled: bool = False
    url: str = DEFAULT_URL
    model: str = "qwen3-rerank"
    api_key: str = field(default="", repr=False)
    timeout: float = 3.0
    candidates: int = 30
    top_n: int = 8

    def validate(self):
        if not self.enabled:
            return
        parsed = urlsplit(self.url)
        if (parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("MEM0_RERANK_URL must be an HTTP(S) endpoint without credentials/query/fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote MEM0_RERANK_URL must use HTTPS")
        if not self.model or not self.api_key:
            raise ValueError("Set MEM0_RERANK_MODEL and MEM0_RERANK_API_KEY (Qwen can reuse its Beijing key)")
        if not math.isfinite(self.timeout) or not 0 < self.timeout <= 30:
            raise ValueError("MEM0_RERANK_TIMEOUT must be in (0, 30] seconds")
        if not 1 <= self.top_n <= self.candidates <= 100:
            raise ValueError("Require 1 <= MEM0_RERANK_TOP_N <= MEM0_RERANK_CANDIDATES <= 100")

    def describe(self):
        # Endpoint/key are deliberately omitted: config diagnostics must not expose credentials.
        return {"enabled": self.enabled, "model": self.model, "api_key_set": bool(self.api_key),
                "timeout_seconds": self.timeout, "candidates": self.candidates, "top_n": self.top_n}


def clip_bytes(text, limit):
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


class Reranker:
    def __init__(self, config=None, *, transport=None):
        self.config = config or RerankConfig()
        self.config.validate()
        self.client = (httpx.Client(timeout=self.config.timeout, follow_redirects=False, transport=transport,
                                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4))
                       if self.config.enabled else None)

    def close(self):
        if self.client:
            self.client.close()

    def candidate_limit(self, limit):
        return max(limit, self.config.candidates) if self.config.enabled else limit

    def rank(self, query, candidates, top_n, *, fallback=None):
        original = list(candidates if fallback is None else fallback)[:top_n]
        if not self.config.enabled:
            return original, {"status": "disabled"}
        # Preserve metadata and identity: upstream indices may only select local candidates.
        pool, identities = [], set()
        for row in candidates:
            identity = row.get("id")
            if identity is None or str(identity) in identities:
                continue
            text = row.get("text") or row.get("memory") or ""
            if not isinstance(text, str) or not text.strip():
                continue
            identities.add(str(identity))
            pool.append((row, text))
            if len(pool) >= max(top_n, self.config.candidates):
                break
        if len(pool) < 2:
            return original, {"status": "skipped", "reason": "fewer_than_two_candidates"}
        started = time.monotonic()
        info = {"model": self.config.model, "candidate_count": len(pool)}
        try:
            # Conservative UTF-8 byte budgets bound Qwen's 4k-token per-item and
            # 120k-token request limits without downloading a tokenizer.
            safe_query = clip_bytes(query, min(1500, 45000 // len(pool)))
            document_budget = min(3000, (90000 - len(safe_query.encode("utf-8")) * len(pool)) // len(pool))
            documents = [clip_bytes(text, document_budget) for _, text in pool]
            info["input_truncated"] = safe_query != query or any(text != doc for (_, text), doc in zip(pool, documents))
            count = min(top_n, len(pool))
            response = self.client.post(self.config.url, headers={"Authorization": "Bearer " + self.config.api_key},
                                        json={"model": self.config.model, "query": safe_query,
                                              "documents": documents, "top_n": count})
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") if isinstance(payload, dict) else None
            if not isinstance(results, list) or len(results) != count:
                raise ValueError("invalid_result_count")
            selected, indices = [], set()
            for item in results:
                if not isinstance(item, dict):
                    raise ValueError("invalid_result")
                index, score = item.get("index"), item.get("relevance_score")
                if type(index) is not int or not 0 <= index < len(pool) or index in indices:
                    raise ValueError("invalid_result_index")
                if type(score) not in {int, float} or not math.isfinite(score) or not 0 <= score <= 1:
                    raise ValueError("invalid_result_score")
                indices.add(index)
                selected.append({**pool[index][0], "rerank_score": float(score)})
            selected.sort(key=lambda item: item["rerank_score"], reverse=True)
            return selected, {**info, "status": "applied", "duration_ms": round((time.monotonic() - started) * 1000)}
        except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError):
            # Never echo upstream errors: providers may include prompts, URLs or keys.
            return original, {**info, "status": "fallback", "reason": "upstream_error_or_invalid_response",
                              "duration_ms": round((time.monotonic() - started) * 1000)}
