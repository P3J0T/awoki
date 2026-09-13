"""Bounded rerank inputs and process-local provider backpressure.

No remote tokenizer downloads, automatic retry loop, or persisted credentials.
The default byte estimate is deliberately conservative, NOT an exact token count.
"""
from __future__ import annotations

import hashlib
import math
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any


class RerankInputBudgetExceeded(ValueError):
    pass


class ProviderCoolingDown(RuntimeError):
    def __init__(self, seconds: float):
        self.retry_after_seconds = max(1, math.ceil(seconds))
        super().__init__("Provider cooldown active")


_COOLDOWNS: dict[str, float] = {}
_LOCK = threading.Lock()


def provider_key(endpoint: str, model: str, api_key: str) -> str:
    return hashlib.sha256(repr((endpoint, model, api_key)).encode()).hexdigest()


def check_cooldown(key: str) -> None:
    with _LOCK:
        remaining = _COOLDOWNS.get(key, 0) - time.monotonic()
    if remaining > 0:
        raise ProviderCoolingDown(remaining)


def error_metadata(exc: Exception) -> dict[str, Any]:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    category = ("rate_limited" if status == 429 or isinstance(exc, ProviderCoolingDown)
                else "input_budget_exceeded" if isinstance(exc, RerankInputBudgetExceeded)
                else "input_rejected" if status in (400, 413, 422)
                else "provider_unavailable" if isinstance(status, int) and status >= 500
                else "provider_error")
    return {"failure_category": category, "retryable": category in {"rate_limited", "provider_unavailable"},
            "retry_after_seconds": getattr(exc, "retry_after_seconds", None)}


def note_rate_limit(key: str, exc: Exception) -> None:
    if error_metadata(exc)["failure_category"] != "rate_limited" or isinstance(exc, ProviderCoolingDown):
        return
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    try:
        seconds = float(headers.get("retry-after", "5"))
        if not math.isfinite(seconds):
            seconds = 5
    except (TypeError, ValueError):
        seconds = 5
    seconds = max(1.0, min(seconds, 60.0))
    exc.retry_after_seconds = math.ceil(seconds)
    with _LOCK:
        now = time.monotonic()
        for expired in [k for k, deadline in _COOLDOWNS.items() if deadline <= now]:
            _COOLDOWNS.pop(expired, None)
        if len(_COOLDOWNS) >= 128:
            _COOLDOWNS.pop(min(_COOLDOWNS, key=_COOLDOWNS.get))
        _COOLDOWNS[key] = max(_COOLDOWNS.get(key, 0), now + seconds)


@lru_cache(maxsize=4)
def _load_tokenizer(path: str, size: int, mtime_ns: int):
    from tokenizers import Tokenizer  # optional: needed only for operator-supplied tokenizer.json
    tokenizer = Tokenizer.from_file(path)
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


def fit_rerank_documents(query: str, documents: list[str], profile: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    budget = max(32, min(int(profile.get("max_input_tokens") or 512), 32768))
    tokenizer_path = str(profile.get("tokenizer_path") or "").strip()
    if tokenizer_path:
        path = Path(tokenizer_path)
        stat = path.stat()
        if not path.is_absolute() or not path.is_file() or stat.st_size > 32 * 1024 * 1024:
            raise ValueError("Tokenizer must be a bounded, absolute local JSON file")
        tokenizer = _load_tokenizer(str(path), stat.st_size, stat.st_mtime_ns)
        count = lambda doc: len(tokenizer.encode(query, doc, add_special_tokens=True).ids)
        mode = "configured_tokenizer_pair"
    else:
        # Includes query bytes and reserve for pair separators/special tokens.
        # Custom tokenizer normalization can differ: report estimate, never proof.
        count = lambda doc: len(query.encode("utf-8")) + len(doc.encode("utf-8")) + 16
        mode = "conservative_utf8_estimate"
    if count("") >= budget:
        raise RerankInputBudgetExceeded("Query leaves no document budget")
    fitted: list[str] = []
    clipped = 0
    for document in documents:
        if count(document) <= budget:
            fitted.append(document)
            continue
        low, high = 0, len(document)
        while low < high:
            middle = (low + high + 1) // 2
            if count(document[:middle]) <= budget:
                low = middle
            else:
                high = middle - 1
        candidate = document[:low]
        if not candidate or count(candidate) > budget:
            raise RerankInputBudgetExceeded("Document cannot fit paired query budget")
        fitted.append(candidate)
        clipped += 1
    return fitted, {"mode": mode, "max_input_tokens": budget, "clipped_documents": clipped,
                    "document_count": len(documents), "original_hits_preserved": True,
                    "tokenizer_matches_provider": "operator_responsibility" if tokenizer_path else "unknown"}
