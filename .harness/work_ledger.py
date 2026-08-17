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
import safety

SCHEMA = "awoki-session-work/v3"
MAX_TODOS = 64
MAX_CONTENT = 800
MAX_ID = 200
MAX_ACTIVE_REFERENCES = 12
_ALLOWED_STATUS = {"pending", "in_progress", "completed", "cancelled"}
_ALLOWED_PRIORITY = {"low", "medium", "high"}


def _now() -> str:
    return project_workspace.now_ts()


def _key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _path(root: Path, session_id: str) -> Path:
    return project_workspace.state_dir(root) / "work-ledger" / f"{_key(session_id)}.json"


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
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _base(session_id: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "session_key": _key(session_id),
        "created_at": _now(),
        "updated_at": _now(),
        "project_id": "",
        "todo_generation": 0,
        "next_todo_sequence": 1,
        "user_turn_generation": 0,
        "compaction_generation": 0,
        "pending_compaction_trigger": "",
        "pending_compaction_trigger_source": "",
        "last_compaction_trigger": "unknown",
        "todos_need_review": False,
        "last_user_message_id": "",
        "last_compacted_at": "",
        "todos": [],
        "active_references": [],
        "references_need_review": False,
    }


def _clean_todo(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    content = str(raw.get("content") or "").strip()[:MAX_CONTENT]
    if not content:
        return None
    content, redacted = safety.redact_text(content)
    source_id = str(raw.get("id") or "").strip()[:MAX_ID]
    status = str(raw.get("status") or "pending").strip().lower()
    priority = str(raw.get("priority") or "medium").strip().lower()
    if status not in _ALLOWED_STATUS:
        status = "pending"
    if priority not in _ALLOWED_PRIORITY:
        priority = "medium"
    return {"id": "", "source_id": source_id, "content": content, "status": status, "priority": priority, "redacted": bool(redacted)}



def _allocate_todo_id(state: dict[str, Any]) -> str:
    seq = max(1, int(state.get("next_todo_sequence") or 1))
    state["next_todo_sequence"] = seq + 1
    return f"atd_{str(state.get('session_key') or '')[:8]}_{seq:06d}"


def _ensure_existing_ids(state: dict[str, Any]) -> bool:
    changed = False
    todos = list(state.get("todos") or [])
    seen: set[str] = set()
    for row in todos:
        if not isinstance(row, dict):
            continue
        todo_id = str(row.get("id") or "").strip()
        if not todo_id or todo_id in seen:
            row["id"] = _allocate_todo_id(state)
            changed = True
        seen.add(str(row.get("id") or ""))
        row.setdefault("source_id", "")
    state["todos"] = todos
    return changed


def _reconcile_todo_ids(state: dict[str, Any], cleaned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve ledger-owned TODO identity across status changes, reorder, and clear renames.

    OpenCode 1.18.x may emit empty todo IDs.  The ledger therefore owns stable
    session-local identities.  Incoming OpenCode IDs, when present, are treated as
    source hints rather than durable identity.  Ambiguous duplicate delete/add
    operations cannot be distinguished without upstream IDs; they still receive
    distinct durable IDs and are reconciled conservatively.
    """
    _ensure_existing_ids(state)
    old = [dict(row) for row in (state.get("todos") or []) if isinstance(row, dict)]
    used: set[int] = set()

    def claim(predicate) -> dict[str, Any] | None:
        for idx, row in enumerate(old):
            if idx in used:
                continue
            if predicate(row):
                used.add(idx)
                return row
        return None

    assigned: list[dict[str, Any]] = []
    for idx, row in enumerate(cleaned):
        source_id = str(row.get("source_id") or "")
        match = None
        if source_id:
            match = claim(lambda old_row: str(old_row.get("source_id") or "") == source_id)
        if match is None:
            match = claim(lambda old_row: (
                str(old_row.get("content") or "") == str(row.get("content") or "")
                and str(old_row.get("status") or "") == str(row.get("status") or "")
                and str(old_row.get("priority") or "") == str(row.get("priority") or "")
            ))
        if match is None:
            match = claim(lambda old_row: str(old_row.get("content") or "") == str(row.get("content") or ""))
        if match is None and len(old) == len(cleaned) and idx < len(old) and idx not in used:
            # A same-length rewrite with a single unambiguous positional remainder
            # is treated as a rename/status edit rather than delete+add.
            remaining_old = [i for i in range(len(old)) if i not in used]
            remaining_new = len(cleaned) - len(assigned)
            if len(remaining_old) == remaining_new:
                used.add(idx)
                match = old[idx]
        durable_id = str((match or {}).get("id") or "") or _allocate_todo_id(state)
        item = dict(row)
        item["id"] = durable_id
        if match is not None and not source_id:
            item["source_id"] = str(match.get("source_id") or "")
        assigned.append(item)
    return assigned

def sync_todos(root: Path, session_id: str, todos: list[Any]) -> dict[str, Any]:
    if not session_id.strip():
        return {"status": "ignored", "reason": "missing_session_id"}
    path = _path(root, session_id)
    cleaned = [row for item in todos[:MAX_TODOS] if (row := _clean_todo(item)) is not None]
    with _lock(path):
        state = {**_base(session_id), **_read(path)}
        current_project = project_workspace.current_project_id(root, session_id=session_id) or ""
        state["schema"] = SCHEMA
        reconciled = _reconcile_todo_ids(state, cleaned)
        state.update({
            "project_id": current_project,
            "updated_at": _now(),
            "todo_generation": int(state.get("todo_generation") or 0) + 1,
            "todos_need_review": False,
            "todos": reconciled,
        })
        _write(path, state)
    return {"status": "saved", "project_id": current_project, "todo_count": len(cleaned), "todo_generation": state["todo_generation"]}




def _references_need_review(state: dict[str, Any]) -> bool:
    current_generation = int(state.get("user_turn_generation") or 0)
    return any(
        isinstance(row, dict)
        and int(row.get("user_turn_generation") or 0) < current_generation
        for row in (state.get("active_references") or [])
    )

def mark_user_turn(root: Path, session_id: str, *, message_id: str = "") -> dict[str, Any]:
    if not session_id.strip():
        return {"status": "ignored", "reason": "missing_session_id"}
    path = _path(root, session_id)
    with _lock(path):
        state = {**_base(session_id), **_read(path)}
        message_id = str(message_id or "").strip()[:MAX_ID]
        if message_id and message_id == str(state.get("last_user_message_id") or ""):
            return {"status": "unchanged", "user_turn_generation": int(state.get("user_turn_generation") or 0)}
        state["last_user_message_id"] = message_id
        state["user_turn_generation"] = int(state.get("user_turn_generation") or 0) + 1
        state["todos_need_review"] = bool(state.get("todos"))
        state["references_need_review"] = bool(state.get("active_references"))
        state["updated_at"] = _now()
        _write(path, state)
    return {
        "status": "marked",
        "user_turn_generation": state["user_turn_generation"],
        "todos_need_review": state["todos_need_review"],
        "references_need_review": state["references_need_review"],
    }


def touch_reference(
    root: Path,
    session_id: str,
    *,
    project_id: str,
    reference_id: str,
    label: str = "",
    why_saved: str = "",
) -> dict[str, Any]:
    """Keep a tiny current-session reference working set.

    This deliberately reuses the existing session work ledger rather than
    creating a second intent/reference lifecycle.  It stores only stable IDs and
    bounded human navigation metadata; rich evidence remains in its authoritative
    project store.  A new user turn marks the set for review, but does not erase
    it because a follow-up may intentionally refer to the same investigation.
    """
    if not session_id.strip() or not reference_id.strip():
        return {"status": "ignored", "reason": "missing_session_or_reference"}
    path = _path(root, session_id)
    with _lock(path):
        state = {**_base(session_id), **_read(path)}
        rows = [dict(row) for row in (state.get("active_references") or []) if isinstance(row, dict)]
        clean_id = str(reference_id).strip()[:MAX_ID]
        clean_label, _ = safety.redact_text(str(label or "").strip()[:240])
        clean_why, _ = safety.redact_text(str(why_saved or "").strip()[:500])
        rows = [row for row in rows if str(row.get("reference_id") or "") != clean_id]
        rows.append({
            "project_id": str(project_id or "")[:MAX_ID],
            "reference_id": clean_id,
            "label": clean_label,
            "why_saved": clean_why,
            "touched_at": _now(),
            "user_turn_generation": int(state.get("user_turn_generation") or 0),
        })
        state["active_references"] = rows[-MAX_ACTIVE_REFERENCES:]
        state["references_need_review"] = _references_need_review(state)
        state["updated_at"] = _now()
        _write(path, state)
    return {"status": "saved", "active_reference_count": len(state["active_references"])}


def mark_compaction_trigger(root: Path, session_id: str, *, trigger: str, source: str = "") -> dict[str, Any]:
    """Persist the structural trigger for the next compaction event.

    OpenCode's compaction part exposes whether the compaction was automatic. No
    conversational text is stored. Unknown/missing signals stay unknown rather
    than being inferred from timing.
    """
    if not session_id.strip():
        return {"status": "ignored", "reason": "missing_session_id"}
    clean = str(trigger or "unknown").strip().lower()
    if clean not in {"automatic_context_pressure", "explicit_request", "unknown"}:
        clean = "unknown"
    path = _path(root, session_id)
    with _lock(path):
        state = {**_base(session_id), **_read(path)}
        state["pending_compaction_trigger"] = clean
        state["pending_compaction_trigger_source"] = str(source or "")[:120]
        state["updated_at"] = _now()
        _write(path, state)
    return {"status": "marked", "trigger": clean, "source": state["pending_compaction_trigger_source"]}

def mark_compacted(root: Path, session_id: str) -> dict[str, Any]:
    if not session_id.strip():
        return {"status": "ignored", "reason": "missing_session_id"}
    path = _path(root, session_id)
    with _lock(path):
        state = {**_base(session_id), **_read(path)}
        trigger = str(state.get("pending_compaction_trigger") or "unknown")
        source = str(state.get("pending_compaction_trigger_source") or "")
        if trigger not in {"automatic_context_pressure", "explicit_request", "unknown"}:
            trigger = "unknown"
        state["compaction_generation"] = int(state.get("compaction_generation") or 0) + 1
        state["last_compacted_at"] = _now()
        state["last_compaction_trigger"] = trigger
        state["pending_compaction_trigger"] = ""
        state["pending_compaction_trigger_source"] = ""
        state["updated_at"] = _now()
        _write(path, state)
    return {
        "status": "marked", "compaction_generation": state["compaction_generation"],
        "compaction_trigger": trigger, "compaction_trigger_source": source,
    }

def status(root: Path, session_id: str) -> dict[str, Any]:
    if not session_id.strip():
        return {"status": "ignored", "reason": "missing_session_id"}
    path = _path(root, session_id)
    with _lock(path):
        state = _read(path)
        if not state:
            return {"status": "none", "session_key": _key(session_id), "todos": []}
        state = {**_base(session_id), **state}
        changed = _ensure_existing_ids(state)
        expected_reference_review = _references_need_review(state)
        if bool(state.get("references_need_review")) != expected_reference_review:
            state["references_need_review"] = expected_reference_review
            changed = True
        if state.get("schema") != SCHEMA:
            state["schema"] = SCHEMA
            changed = True
        if changed:
            state["updated_at"] = _now()
            _write(path, state)
    return {"status": "ok", **state}


def compact_context(root: Path, session_id: str, *, max_chars: int = 8_000) -> str:
    state = status(root, session_id)
    if state.get("status") != "ok" or not state.get("todos"):
        return ""
    lines = [
        "## Awoki active session work",
        "",
        "This bounded TODO projection preserves the user's current multi-step goal/deliverables across compaction. It is local operational state, not private reasoning and not canonical project knowledge.",
        "The user's newest instruction always overrides older TODOs. If `needs review` is true, the snapshot predates the latest user turn and must be reconciled before acting.",
        f"project_at_last_todo_update: {state.get('project_id') or 'unattached/ad-hoc'}",
        f"todo_generation: {int(state.get('todo_generation') or 0)}",
        f"compaction_generation: {int(state.get('compaction_generation') or 0)}",
        f"needs review: {'true' if state.get('todos_need_review') else 'false'}",
        "",
        "Current TODO projection:",
    ]
    for todo in list(state.get("todos") or [])[:MAX_TODOS]:
        lines.append(
            f"- [{todo.get('status','pending')}] ({todo.get('priority','medium')}) "
            f"{todo.get('id','')} {todo.get('content','')}"
        )
    return "\n".join(lines)[:max_chars]
