#!/usr/bin/env python3
"""OpenCode event bridge for continuity-first Awoki sessions.

The bridge intentionally receives only sanitized operational metadata: session IDs,
event names, tool names, relative file paths, message identity/role, and bounded native
OpenCode TODO projections. TODO payloads arrive over stdin. It never receives ordinary
conversation message text, tool arguments/results, raw source output, or private reasoning.
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

import project_workspace
import continuations
import work_ledger
import agent_runtime
import acceptance_runs
import reference_catalog
from harness_core import HarnessPaths

IGNORED_PATH_PARTS = {
    ".git",
    ".harness/state",
    ".pytest_cache",
    "__pycache__",
    "node_modules",
}
IGNORED_FILENAMES = {"SITUATION.md", "HANDOFF.md", "project-index.json"}


def _now() -> str:
    return project_workspace.now_ts()


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
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


def _safe_relative_path(root: Path, value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    candidate = Path(raw)
    try:
        if candidate.is_absolute():
            candidate = candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return ""
    normalized = candidate.as_posix().lstrip("./")
    if normalized.startswith("../") or normalized == "..":
        return ""
    return normalized[:1_000]


def _ignored_path(relative_path: str) -> bool:
    if not relative_path:
        return True
    path = Path(relative_path)
    if path.name in IGNORED_FILENAMES:
        return True
    parts = {part.lower() for part in path.parts}
    joined = path.as_posix().lower()
    for ignored in IGNORED_PATH_PARTS:
        value = ignored.lower().strip("/")
        if "/" in value:
            if joined == value or joined.startswith(value + "/") or f"/{value}/" in f"/{joined}/":
                return True
        elif value in parts:
            return True
    return False


def _activity_template() -> dict[str, Any]:
    return {
        "file_events": 0,
        "tool_events": 0,
        "other_events": 0,
        "changed_files": [],
        "tools": [],
        "first_activity_at": "",
        "last_activity_at": "",
        "dirty": False,
    }


def _session_state(root: Path, session_id: str) -> tuple[Path, dict[str, Any]]:
    path = project_workspace.session_state_path(root, session_id)
    state = _read_json(path)
    if not state:
        state = {
            "session_id": session_id,
            "status": "unattached",
            "created_at": _now(),
            "last_activity_at": _now(),
        }
    activity = state.get("activity")
    if not isinstance(activity, dict):
        state["activity"] = _activity_template()
    else:
        state["activity"] = {**_activity_template(), **activity}
    return path, state


def record_activity(
    root: Path,
    session_id: str,
    *,
    event_type: str,
    path: str = "",
    tool: str = "",
) -> dict[str, Any]:
    """Record sanitized observable activity without creating continuity noise."""
    if not session_id.strip():
        return {"status": "ignored", "reason": "missing_session_id"}
    relative_path = _safe_relative_path(root, path)
    if path and _ignored_path(relative_path):
        return {"status": "ignored", "reason": "ignored_path", "path": relative_path}

    state_path = project_workspace.session_state_path(root, session_id)
    with _state_lock(state_path):
        _, state = _session_state(root, session_id)
        if state.get("status") != "active" or not state.get("project_id"):
            return {"status": "ignored", "reason": "session_not_attached", "session_id": session_id}
        activity = state["activity"]
        timestamp = _now()
        if not activity.get("first_activity_at"):
            activity["first_activity_at"] = timestamp
        activity["last_activity_at"] = timestamp
        state["last_activity_at"] = timestamp

        if event_type.startswith("file."):
            activity["file_events"] = int(activity.get("file_events") or 0) + 1
            if relative_path:
                files = [str(item) for item in activity.get("changed_files") or []]
                if relative_path not in files:
                    files.append(relative_path)
                activity["changed_files"] = files[-30:]
        elif event_type.startswith("tool."):
            activity["tool_events"] = int(activity.get("tool_events") or 0) + 1
            safe_tool = (tool or "unknown").strip()[:200]
            tools = [str(item) for item in activity.get("tools") or []]
            if safe_tool and safe_tool not in tools:
                tools.append(safe_tool)
            activity["tools"] = tools[-30:]
        else:
            activity["other_events"] = int(activity.get("other_events") or 0) + 1
        activity["dirty"] = True
        state["activity"] = activity
        _atomic_write_json(state_path, state)
    return {
        "status": "recorded",
        "session_id": session_id,
        "project_id": state.get("project_id"),
        "activity": activity,
    }


def prepare_project_switch(root: Path, session_id: str, target_project: str) -> dict[str, Any]:
    """Checkpoint and detach an active different project before an atomic switch."""
    if not session_id.strip():
        return {"status": "ignored", "reason": "missing_session_id"}
    try:
        target = project_workspace.clean_project_id(target_project)
    except ValueError as exc:
        return {"status": "rejected", "reason": str(exc)}
    current = project_workspace.current_project_id(root, session_id=session_id)
    if not current:
        return {"status": "ready", "target_project": target, "switched": False}
    if current == target:
        return {"status": "ready", "target_project": target, "current_project": current, "switched": False}
    checkpoint = checkpoint_session(
        root,
        session_id,
        reason=f"project.switch:{current}->{target}",
        detach=True,
        force=True,
    )
    return {
        "status": "ready",
        "target_project": target,
        "previous_project": current,
        "switched": True,
        "checkpoint": checkpoint,
    }


def _is_meaningful(activity: dict[str, Any], *, force: bool = False) -> bool:
    if not activity.get("dirty"):
        return False
    if force:
        return True
    return (
        int(activity.get("file_events") or 0) >= 3
        or int(activity.get("tool_events") or 0) >= 5
        or len(activity.get("changed_files") or []) >= 2
    )


def checkpoint_session(
    root: Path,
    session_id: str,
    *,
    reason: str,
    detach: bool = False,
    force: bool = False,
    expected_last_activity_at: str = "",
) -> dict[str, Any]:
    """Checkpoint observable activity without holding a session lock during capture.

    The session snapshot is reconciled after the canonical append. Activity that
    arrives concurrently is preserved, and a detach is refused rather than
    discarding that newer activity.
    """
    if not session_id.strip():
        return {"status": "ignored", "reason": "missing_session_id"}
    state_path = project_workspace.session_state_path(root, session_id)
    with _state_lock(state_path):
        _, observed = _session_state(root, session_id)
        project_id = str(observed.get("project_id") or "")
        activity = dict(observed.get("activity") or _activity_template())
        if observed.get("status") != "active" or not project_id or not project_workspace.project_exists(root, project_id):
            return {"status": "not_attached", "session_id": session_id}
        if expected_last_activity_at and str(observed.get("last_activity_at") or "") != expected_last_activity_at:
            return {
                "status": "state_changed",
                "session_id": session_id,
                "project_id": project_id,
                "reason": "Session activity changed after preview; recovery was not applied.",
            }

    captured: dict[str, Any] | None = None
    meaningful = _is_meaningful(activity, force=force)
    if meaningful:
        file_count = int(activity.get("file_events") or 0)
        tool_count = int(activity.get("tool_events") or 0)
        changed_files = [str(item) for item in activity.get("changed_files") or []]
        tools = [str(item) for item in activity.get("tools") or []]
        parts: list[str] = []
        if file_count:
            parts.append(f"observed {file_count} file operation(s)")
        if tool_count:
            parts.append(f"observed {tool_count} tool operation(s)")
        if reason.startswith("stale_session_recovery"):
            summary = "Recovered stale session activity into project continuity."
        else:
            summary = "Continuity checkpoint after " + (" and ".join(parts) if parts else "meaningful session activity") + "."
        details_bits = [f"Trigger: {reason}."]
        if changed_files:
            details_bits.append("Recently changed files: " + ", ".join(changed_files[:12]) + ".")
        if tools:
            details_bits.append("Observed tools: " + ", ".join(tools[:12]) + ".")
        captured = project_workspace.project_capture(
            root,
            project_id,
            summary,
            kind="continuity_reflection",
            details=" ".join(details_bits),
            sources=[{"type": "file", "path": item} for item in changed_files[:16]],
            confidence="high",
            metadata={
                "capture_channel": "opencode_observable_events",
                "checkpoint_reason": reason,
                "file_event_count": file_count,
                "tool_event_count": tool_count,
            },
            refresh=False,
        )

    detached: dict[str, Any] | None = None
    concurrent_activity = False
    with _state_lock(state_path):
        _, state = _session_state(root, session_id)
        if state.get("status") != "active" or state.get("project_id") != project_id:
            return {
                "status": "checkpoint_conflict",
                "session_id": session_id,
                "project_id": project_id,
                "reflection": captured,
                "reason": "Session attachment changed while the checkpoint was being written.",
            }
        current_activity = dict(state.get("activity") or _activity_template())
        concurrent_activity = current_activity != activity
        if captured:
            state["last_capture_id"] = str(captured.get("id") or "")
            state["last_checkpoint_at"] = _now()
            state["last_checkpoint_reason"] = reason
        if not concurrent_activity:
            state["activity"] = _activity_template() if meaningful else activity
        if detach:
            if concurrent_activity:
                _atomic_write_json(state_path, state)
                return {
                    "status": "checkpoint_conflict",
                    "session_id": session_id,
                    "project_id": project_id,
                    "meaningful": meaningful,
                    "reflection": captured,
                    "reason": "New observable activity arrived during checkpoint; the session remains attached.",
                }
            state["status"] = "paused"
            state["paused_at"] = _now()
            state["last_activity_at"] = _now()
            detached = {"status": "paused", "project_id": project_id, "session_id": session_id}
        _atomic_write_json(state_path, state)

    refresh = project_workspace.refresh_project_files(root, project_id)
    return {
        "status": "checkpointed" if captured else "refreshed_without_capture",
        "session_id": session_id,
        "project_id": project_id,
        "meaningful": meaningful,
        "reflection": captured,
        "refresh": refresh,
        "session": detached,
        "concurrent_activity_preserved": concurrent_activity,
    }


def sync_todos(root: Path, session_id: str, todos: list[Any]) -> dict[str, Any]:
    """Persist a bounded OpenCode TODO projection outside conversational context."""
    return work_ledger.sync_todos(root, session_id, todos)


def mark_user_turn(root: Path, session_id: str, *, message_id: str = "") -> dict[str, Any]:
    """Mark TODO state stale and account for manual recovery without storing message text."""
    work = work_ledger.mark_user_turn(root, session_id, message_id=message_id)
    runtime = agent_runtime.user_turn(root, session_id, message_id=message_id)
    return {**work, "agent_runtime": runtime}


def record_agent_terminal_turn(
    root: Path, session_id: str, *, message_id: str, finish_reason: str,
    has_reasoning: bool, has_text: bool, has_tool: bool,
    provider_id: str = "", model_id: str = "", agent_mode: str = "", error_type: str = "",
    step_finish_seen: bool = False, input_tokens: int = 0, output_tokens: int = 0, reasoning_tokens: int = 0,
    tool_executions_completed: int = 0,
) -> dict[str, Any]:
    return agent_runtime.terminal_turn(
        root, session_id, message_id=message_id, finish_reason=finish_reason,
        has_reasoning=has_reasoning, has_text=has_text, has_tool=has_tool,
        provider_id=provider_id, model_id=model_id, agent_mode=agent_mode, error_type=error_type,
        step_finish_seen=step_finish_seen, input_tokens=input_tokens, output_tokens=output_tokens, reasoning_tokens=reasoning_tokens,
        tool_executions_completed=tool_executions_completed,
    )


def mark_compaction_trigger(root: Path, session_id: str, *, trigger: str, source: str = "") -> dict[str, Any]:
    """Record only the structural compaction trigger for the next compacted event."""
    return work_ledger.mark_compaction_trigger(root, session_id, trigger=trigger, source=source)

def mark_compacted(root: Path, session_id: str) -> dict[str, Any]:
    """Advance the durable compaction generation; no conversation text is stored."""
    work = work_ledger.mark_compacted(root, session_id)
    acceptance = acceptance_runs.mark_compacted(
        root, session_id, generation=int(work.get("compaction_generation") or 0),
        trigger=str(work.get("compaction_trigger") or "unknown"),
        trigger_source=str(work.get("compaction_trigger_source") or ""),
    )
    return {**work, "acceptance_run": acceptance}


def record_acceptance_tool_event(
    root: Path, session_id: str, *, tool: str, tool_class: str, phase: str
) -> dict[str, Any]:
    """Record only structural acceptance-tool provenance; never args or results."""
    return acceptance_runs.record_tool_event(
        root, session_id, tool=tool, tool_class=tool_class, phase=phase
    )


def compaction_context(root: Path, session_id: str, *, max_chars: int = 24_000) -> dict[str, Any]:
    """Return bounded project + durable work/acceptance continuity for compaction.

    Project attachment is optional: session-local TODO state and an explicitly
    scoped acceptance run remain useful for ad-hoc/unattached work. Budgets are
    reserved before project prose so operational state cannot be truncated merely
    because HANDOFF.md is large.
    """
    project_id = project_workspace.current_project_id(root, session_id=session_id)
    execution = (
        "## Awoki execution invariants\n\n"
        "These rules survive compaction. Awoki operation names such as project_*, code_*, "
        "acceptance_run_*, reliability_*, session_* and repository_prepare_* are MCP interfaces, "
        "not shell commands. Use the Awoki MCP for Awoki state/reliability/acceptance operations. In normal "
        "repository investigation, use Awoki indexed/structural search for conceptual discovery, OpenCode Grep "
        "for ordinary exact string/symbol lookup, and native rg through Bash when the full ripgrep CLI materially "
        "helps with complex or exhaustive exact enumeration. Lexical results are discovery until confirmed from "
        "authoritative source. During an active acceptance run, call acceptance_run_next after compaction and obey "
        "its durable per-test contract; its native-tool restrictions override normal investigation ergonomics. "
        "Outside an active machine-enforced contract, normal investigation may use OpenCode/native source tools "
        "according to the source-navigation policy and the newest user instruction. allowed_actions/forbidden_actions "
        "are workflow labels, not authorization grants. "
        "Do not infer PASS from a shortened objective: acceptance_run_record machine-checks required interfaces, "
        "tool provenance, evidence scope, and declared pass requirements. Never persist or reconstruct private reasoning."
    )
    reliability = (
        "## Awoki reliability invariants\n\n"
        "Treat model output and remembered conclusions as fallible. Verify concrete "
        "source, configuration, runtime, test, and tool-state claims against observed "
        "evidence. Never claim a check ran unless its result was observed. Separate "
        "observation, inference, and hypothesis. Exploration may remain incomplete; "
        "completion claims require evidence proportional to the claim. `/reliability-check` "
        "is local-only; delivery actions require explicit `/ship-check` authorization. "
        "Repository understanding is evidence-backed by default: use indexed discovery, "
        "exact structural relationships, bounded hash-checked source, and selective atomic "
        "claim validation. Semantic hits and raw grep previews are discovery only."
    )

    # Reserve deterministic space for operational ledgers before generated project prose.
    work_budget = min(6_000, max(1_200, max_chars // 4))
    acceptance_budget = min(7_000, max(1_500, max_chars // 3))
    reference_budget = min(3_200, max(800, max_chars // 8))
    task_context = work_ledger.compact_context(root, session_id, max_chars=work_budget)
    acceptance_context = acceptance_runs.compact_context(root, session_id, max_chars=acceptance_budget)
    reference_context = reference_catalog.compact_context(root, project_id or "", session_id=session_id, max_chars=reference_budget)
    fixed_sections = [section for section in (execution, task_context, acceptance_context, reference_context, reliability) if section]
    fixed_size = sum(len(section) for section in fixed_sections) + 2 * max(0, len(fixed_sections) - 1)
    project_budget = max(0, max_chars - fixed_size - 8)

    project_context = ""
    if project_id and project_budget >= 400:
        pack = project_workspace.project_handoff(root, project_id)
        situation = str(pack.get("situation") or "").strip()
        handoff = str(pack.get("handoff") or "").strip()
        prefix = (
            "## Awoki project continuity\n\n"
            "This is generated operational continuity, not private reasoning. "
            "The user's new direction overrides all suggested continuation points.\n\n"
        )
        body_budget = max(0, project_budget - len(prefix))
        if body_budget:
            # Prefer the concise situation first and spend the remainder on handoff.
            situation_budget = min(len(situation), max(0, body_budget // 2)) if handoff else body_budget
            bounded_situation = situation[:situation_budget]
            remainder = max(0, body_budget - len(bounded_situation) - (2 if bounded_situation and handoff else 0))
            bounded_handoff = handoff[:remainder]
            project_context = (prefix + bounded_situation + ("\n\n" if bounded_situation and bounded_handoff else "") + bounded_handoff).strip()

    # Put durable operational state first so an unexpectedly small consumer-side
    # truncation preserves the exact work/acceptance continuation before prose.
    sections = [section for section in (execution, task_context, acceptance_context, reference_context, project_context, reliability) if section]
    context = "\n\n".join(sections).strip()
    acceptance_state = acceptance_runs.status(root, session_id=session_id)
    return {
        "status": "ok" if context else "empty",
        "project_id": project_id or "",
        "context": context[:max_chars],
        "work_ledger": work_ledger.status(root, session_id),
        "acceptance_run_id": str(acceptance_state.get("run_id") or "") if acceptance_state.get("status") == "ok" and acceptance_state.get("run_status") == "running" else "",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    activity = sub.add_parser("activity")
    activity.add_argument("--session-id", required=True)
    activity.add_argument("--event", required=True)
    activity.add_argument("--path", default="")
    activity.add_argument("--tool", default="")

    acceptance_tool = sub.add_parser("acceptance-tool")
    acceptance_tool.add_argument("--session-id", required=True)
    acceptance_tool.add_argument("--tool", required=True)
    acceptance_tool.add_argument("--tool-class", required=True)
    acceptance_tool.add_argument("--phase", choices=["started", "completed"], required=True)

    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--session-id", required=True)
    checkpoint.add_argument("--reason", required=True)
    checkpoint.add_argument("--detach", action="store_true")
    checkpoint.add_argument("--force", action="store_true")

    context = sub.add_parser("context")
    context.add_argument("--session-id", required=True)
    context.add_argument("--max-chars", type=int, default=24_000)

    switch = sub.add_parser("switch")
    switch.add_argument("--session-id", required=True)
    switch.add_argument("--target-project", required=True)

    todo_sync = sub.add_parser("todo-sync")
    todo_sync.add_argument("--session-id", required=True)

    user_turn = sub.add_parser("user-turn")
    user_turn.add_argument("--session-id", required=True)
    user_turn.add_argument("--message-id", default="")


    terminal = sub.add_parser("agent-turn-terminal")
    terminal.add_argument("--session-id", required=True)
    terminal.add_argument("--message-id", required=True)
    terminal.add_argument("--finish-reason", default="")
    terminal.add_argument("--has-reasoning", action="store_true")
    terminal.add_argument("--has-text", action="store_true")
    terminal.add_argument("--has-tool", action="store_true")
    terminal.add_argument("--provider-id", default="")
    terminal.add_argument("--model-id", default="")
    terminal.add_argument("--agent-mode", default="")
    terminal.add_argument("--error-type", default="")
    terminal.add_argument("--step-finish-seen", action="store_true")
    terminal.add_argument("--input-tokens", type=int, default=0)
    terminal.add_argument("--output-tokens", type=int, default=0)
    terminal.add_argument("--reasoning-tokens", type=int, default=0)
    terminal.add_argument("--tool-executions-completed", type=int, default=0)

    compaction_trigger = sub.add_parser("compaction-trigger")
    compaction_trigger.add_argument("--session-id", required=True)
    compaction_trigger.add_argument("--trigger", required=True)
    compaction_trigger.add_argument("--source", default="")

    compacted = sub.add_parser("compacted")
    compacted.add_argument("--session-id", required=True)

    cont_status = sub.add_parser("continuation-status")
    cont_status.add_argument("--session-id", required=True)

    cont_poll = sub.add_parser("continuation-poll")
    cont_poll.add_argument("--session-id", required=True)

    cont_claim = sub.add_parser("continuation-claim")
    cont_claim.add_argument("--session-id", required=True)

    cont_release = sub.add_parser("continuation-release")
    cont_release.add_argument("--session-id", required=True)
    cont_release.add_argument("--generation", required=True, type=int)
    cont_release.add_argument("--retry-seconds", type=int, default=60)
    cont_release.add_argument("--reason", default="resume_failed")

    cont_cancel = sub.add_parser("continuation-cancel")
    cont_cancel.add_argument("--session-id", required=True)
    cont_cancel.add_argument("--reason", default="session_deleted")

    sub.add_parser("continuation-pending")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = HarnessPaths.from_env()
    root = paths.root.resolve()
    if args.command == "activity":
        result = record_activity(root, args.session_id, event_type=args.event, path=args.path, tool=args.tool)
    elif args.command == "acceptance-tool":
        result = record_acceptance_tool_event(
            root, args.session_id, tool=args.tool, tool_class=args.tool_class, phase=args.phase
        )
    elif args.command == "checkpoint":
        result = checkpoint_session(root, args.session_id, reason=args.reason, detach=args.detach, force=args.force)
    elif args.command == "context":
        result = compaction_context(root, args.session_id, max_chars=max(2_000, args.max_chars))
    elif args.command == "switch":
        result = prepare_project_switch(root, args.session_id, args.target_project)
    elif args.command == "todo-sync":
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, OSError):
            payload = {}
        todos = payload.get("todos") if isinstance(payload, dict) else []
        result = sync_todos(root, args.session_id, todos if isinstance(todos, list) else [])
    elif args.command == "user-turn":
        result = mark_user_turn(root, args.session_id, message_id=args.message_id)
    elif args.command == "agent-turn-terminal":
        result = record_agent_terminal_turn(
            root, args.session_id, message_id=args.message_id, finish_reason=args.finish_reason,
            has_reasoning=args.has_reasoning, has_text=args.has_text, has_tool=args.has_tool,
            provider_id=args.provider_id, model_id=args.model_id, agent_mode=args.agent_mode, error_type=args.error_type,
            step_finish_seen=args.step_finish_seen, input_tokens=args.input_tokens, output_tokens=args.output_tokens, reasoning_tokens=args.reasoning_tokens,
            tool_executions_completed=args.tool_executions_completed,
        )
    elif args.command == "compaction-trigger":
        result = mark_compaction_trigger(root, args.session_id, trigger=args.trigger, source=args.source)
    elif args.command == "compacted":
        result = mark_compacted(root, args.session_id)
    elif args.command == "continuation-status":
        result = continuations.status(root, args.session_id)
    elif args.command == "continuation-poll":
        result = continuations.poll_due(root, args.session_id)
    elif args.command == "continuation-claim":
        result = continuations.claim_due(root, args.session_id)
    elif args.command == "continuation-release":
        result = continuations.release(
            root, args.session_id, generation=args.generation,
            retry_seconds=args.retry_seconds, reason=args.reason,
        )
    elif args.command == "continuation-cancel":
        result = continuations.cancel(root, args.session_id, reason=args.reason)
    else:
        result = {"status": "ok", "pending": continuations.pending(root)}
    json.dump(result, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
