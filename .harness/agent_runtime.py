from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

import project_workspace

SCHEMA = "awoki-agent-runtime/v1"


def _now() -> str:
    return project_workspace.now_ts()


def _key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _path(root: Path, session_id: str) -> Path:
    return project_workspace.state_dir(root) / "agent-runtime" / f"{_key(session_id)}.json"


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _base() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "anomaly_count": 0,
        "manual_recovery_attempts": 0,
        "automatic_recovery_attempts": 0,
        "recovered_count": 0,
        "unresolved_anomaly": False,
        "last_terminal_turn": {},
        "last_anomaly": {},
        "last_user_recovery_message_id": "",
        "updated_at": _now(),
    }


def terminal_turn(
    root: Path,
    session_id: str,
    *,
    message_id: str,
    finish_reason: str,
    has_reasoning: bool,
    has_text: bool,
    has_tool: bool,
    provider_id: str = "",
    model_id: str = "",
    agent_mode: str = "",
    error_type: str = "",
    step_finish_seen: bool = False,
    input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    tool_executions_completed: int = 0,
) -> dict[str, Any]:
    """Record structural terminal-turn metadata only; never persist reasoning text."""
    if not session_id.strip() or not message_id.strip():
        return {"status": "ignored", "reason": "missing_session_or_message_id"}
    path = _path(root, session_id)
    with _lock(path):
        state = {**_base(), **_read(path)}
        prior_terminal = dict(state.get("last_terminal_turn") or {})
        if str(prior_terminal.get("message_id") or "") == message_id:
            return {"status": "duplicate", **public_status(state)}
        finish = str(finish_reason or "").strip().lower()[:80]
        terminal = {
            "message_id": message_id[:240],
            "finish_reason": finish,
            "reasoning_present": bool(has_reasoning),
            "text_present": bool(has_text),
            "tool_present": bool(has_tool),
            "provider_id": str(provider_id or "")[:240],
            "model_id": str(model_id or "")[:240],
            "agent_mode": str(agent_mode or "")[:120],
            "error_type": str(error_type or "")[:160],
            "step_finish_seen": bool(step_finish_seen),
            "input_tokens": _count(input_tokens),
            "output_tokens": _count(output_tokens),
            "reasoning_tokens": _count(reasoning_tokens),
            "tool_executions_completed": _count(tool_executions_completed),
            "observed_at": _now(),
        }
        # This function is called at OpenCode session.idle, so the turn is terminal even
        # when the provider/SDK omitted a finish reason. Reasoning with neither normal
        # text nor an executable tool part is therefore observable degradation by itself.
        if has_reasoning and not has_text and not has_tool:
            classification = "reasoning_only_terminal_turn"
        elif has_tool and not has_text and _count(tool_executions_completed) > 0:
            classification = "tool_execution_without_followup"
        elif str(error_type or "").strip():
            classification = "provider_error_terminal_turn"
        else:
            classification = "normal_or_unclassified_terminal_turn"
        anomaly = classification != "normal_or_unclassified_terminal_turn"
        terminal["classification"] = classification
        state["last_terminal_turn"] = terminal
        if anomaly:
            state["anomaly_count"] = int(state.get("anomaly_count") or 0) + 1
            state["unresolved_anomaly"] = True
            state["last_anomaly"] = dict(terminal)
            state["last_user_recovery_message_id"] = ""
        elif state.get("unresolved_anomaly") and bool(str(state.get("last_user_recovery_message_id") or "")):
            state["unresolved_anomaly"] = False
            state["recovered_count"] = int(state.get("recovered_count") or 0) + 1
            last = dict(state.get("last_anomaly") or {})
            if last:
                last["recovered_at"] = _now()
                last["recovery_mode"] = "manual_user_followup"
                state["last_anomaly"] = last
        state["updated_at"] = _now()
        _write(path, state)
    return {"status": "recorded", **public_status(state)}


def user_turn(root: Path, session_id: str, *, message_id: str = "") -> dict[str, Any]:
    """Count a user follow-up after an unresolved runtime anomaly as a manual recovery attempt."""
    if not session_id.strip():
        return {"status": "ignored", "reason": "missing_session_id"}
    path = _path(root, session_id)
    with _lock(path):
        state = {**_base(), **_read(path)}
        if state.get("unresolved_anomaly"):
            clean_id = str(message_id or "")[:240]
            if not clean_id or clean_id != str(state.get("last_user_recovery_message_id") or ""):
                state["manual_recovery_attempts"] = int(state.get("manual_recovery_attempts") or 0) + 1
                state["last_user_recovery_message_id"] = clean_id
                state["updated_at"] = _now()
                _write(path, state)
                return {"status": "recovery_attempt_recorded", **public_status(state)}
        return {"status": "no_recovery_needed", **public_status(state)}


def public_status(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "runtime_state": "degraded" if state.get("unresolved_anomaly") else "ok",
        "unresolved_anomaly": bool(state.get("unresolved_anomaly")),
        "anomaly_count": int(state.get("anomaly_count") or 0),
        "agent_turn_recovery_attempts": int(state.get("manual_recovery_attempts") or 0),
        "automatic_recovery_attempts": int(state.get("automatic_recovery_attempts") or 0),
        "recovered_count": int(state.get("recovered_count") or 0),
        "last_terminal_turn": dict(state.get("last_terminal_turn") or {}),
        "last_anomaly": dict(state.get("last_anomaly") or {}),
        "privacy": "structural turn metadata only; reasoning content is never persisted",
    }


def status(root: Path, session_id: str) -> dict[str, Any]:
    if not session_id.strip():
        return {"status": "rejected", "reason": "session_id is required"}
    state = _read(_path(root, session_id))
    if not state:
        return {"status": "none", **public_status(_base())}
    return {"status": "ok", **public_status(state)}
