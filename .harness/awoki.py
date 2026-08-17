#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import continuity
import continuity_migration
import indexing_policy
import project_workspace


def root_from_env() -> Path:
    return Path(os.environ.get("AWOKI_ROOT", Path(__file__).resolve().parents[1])).resolve()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def continuity_doctor(root: Path) -> dict[str, Any]:
    """Read-only audit of storage, generated views, indexes, and sessions."""
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    projects = project_workspace.project_list(root)
    checked: list[dict[str, Any]] = []

    # Import lazily so administration help remains lightweight and to avoid
    # coupling the read-only CLI parser to retrieval backends.
    from harness_core import HarnessPaths, project_index_preview
    harness_paths = HarnessPaths(root=root, global_root=Path(os.environ.get("AWOKI_GLOBAL_ROOT", root / ".global")).resolve())

    unsafe_reasons = {
        "symlink_not_allowed", "sensitive_content_detected", "no_rag_marker",
        "path_outside_root", "path_traversal_not_allowed",
    }

    for row in projects:
        project_id = str(row.get("project_id") or "")
        pp = project_workspace.paths_for(root, project_id)
        raw_meta = _read_json(pp.project_json)
        if not raw_meta:
            issues.append({
                "project_id": project_id,
                "kind": "invalid_project_metadata",
                "path": str(pp.project_json),
                "repair": "restore valid project.json or recreate the project metadata before writes",
            })
        records = continuity.read_jsonl(pp.continuity)
        parse_errors = [record for record in records if record.get("kind") == "parse_error"]
        legacy_parse_errors: list[dict[str, Any]] = []
        for filename in continuity.LEGACY_MEMORY_FILES:
            legacy_parse_errors.extend(
                record for record in continuity.read_jsonl(pp.memory_dir / filename)
                if record.get("kind") == "parse_error"
            )
        if parse_errors or legacy_parse_errors:
            issues.append({
                "project_id": project_id,
                "kind": "continuity_parse_error",
                "canonical_count": len(parse_errors),
                "legacy_count": len(legacy_parse_errors),
                "repair": "fix the reported JSONL lines, then run awoki migrate PROJECT --preview",
            })

        view_audit = project_workspace.generated_view_audit(root, project_id)
        if view_audit.get("situation_drift") or view_audit.get("handoff_drift"):
            issues.append({
                "project_id": project_id,
                "kind": "generated_view_drift",
                "situation_drift": bool(view_audit.get("situation_drift")),
                "handoff_drift": bool(view_audit.get("handoff_drift")),
                "repair": f"project_refresh(name='{project_id}') or python .harness/project.py handoff {project_id}",
            })

        manifest = indexing_policy.read_index_manifest(pp.index_manifest)
        if manifest and manifest.get("project_id") != project_id:
            issues.append({
                "project_id": project_id,
                "kind": "index_manifest_scope_mismatch",
                "manifest_project_id": manifest.get("project_id"),
                "repair": f"project_refresh(name='{project_id}')",
            })
        freshness = project_workspace.project_index_freshness(root, project_id)
        if not manifest:
            warnings.append({
                "project_id": project_id,
                "kind": "not_indexed",
                "repair": f"project_refresh(name='{project_id}')",
            })
        elif not freshness.get("fresh"):
            warnings.append({
                "project_id": project_id,
                "kind": "stale_project_index",
                "workspace_generation": freshness.get("workspace_generation"),
                "indexed_generation": freshness.get("indexed_generation"),
                "probe_matches": freshness.get("probe_matches"),
                "policy_matches": freshness.get("policy_matches"),
                "repair": f"project_refresh(name='{project_id}')",
            })

        preview = project_index_preview(
            project_id,
            include_artifacts=bool(manifest.get("include_artifacts", True)) if manifest else True,
            include_code=bool(manifest.get("include_code", False)) if manifest else False,
            paths=harness_paths,
            refresh_views=False,
        )
        excluded = [item for item in preview.get("excluded", []) if isinstance(item, dict)]
        unsafe = [
            item for item in excluded
            if str(item.get("reason") or "") in unsafe_reasons
            or str(item.get("reason") or "").startswith(("excluded_sensitive_extension:", "excluded_path_component:"))
        ]
        if unsafe:
            warnings.append({
                "project_id": project_id,
                "kind": "unsafe_index_candidates_excluded",
                "count": len(unsafe),
                "examples": [
                    {key: item.get(key) for key in ("path", "reason", "kind")}
                    for item in unsafe[:10]
                ],
                "repair": "review project_index_preview; excluded material requires no action unless it was misclassified",
            })

        checked.append({
            "project_id": project_id,
            "record_count": len(records) - len(parse_errors),
            "parse_error_count": len(parse_errors) + len(legacy_parse_errors),
            "generated_views_present": bool(view_audit.get("situation_present") and view_audit.get("handoff_present")),
            "generated_view_drift": bool(view_audit.get("situation_drift") or view_audit.get("handoff_drift")),
            "index_generation": int(manifest.get("index_generation") or 0),
            "index_fresh": bool(freshness.get("fresh")),
            "index_preview_included": len(preview.get("included", [])),
            "index_preview_excluded": len(excluded),
            "unsafe_candidate_count": len(unsafe),
        })

    sessions_path = root / ".harness" / "state" / "sessions"
    session_count = 0
    if sessions_path.exists():
        for path in sessions_path.glob("*.json"):
            session_count += 1
            state = _read_json(path)
            if not state:
                issues.append({
                    "kind": "invalid_session_state",
                    "path": str(path),
                    "repair": "remove or restore the invalid session JSON after reviewing it",
                })
                continue
            project_id = str(state.get("project_id") or "")
            if state.get("status") == "active" and (not project_id or not project_workspace.project_exists(root, project_id)):
                issues.append({
                    "kind": "orphaned_active_session",
                    "path": str(path),
                    "project_id": project_id,
                    "repair": "awoki sessions --stale-after-hours 0 --apply",
                })
    stale = project_workspace.session_recovery_plan(root, stale_after_hours=24)
    for item in stale.get("sessions", []):
        warnings.append({
            "kind": "stale_active_session",
            "session_id": item.get("session_id"),
            "project_id": item.get("project_id"),
            "dirty": item.get("dirty"),
            "repair": "awoki sessions --stale-after-hours 24 --apply",
        })

    return {
        "status": "issues" if issues else "ok",
        "root": str(root),
        "project_count": len(projects),
        "session_count": session_count,
        "projects": checked,
        "issues": issues,
        "warnings": warnings,
        "stale_session_preview": stale,
        "read_only": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="awoki", description="Awoki continuity administration")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="audit continuity storage without modifying it")
    doctor.add_argument("--strict", action="store_true", help="treat warnings as a failing exit status")

    migrate = sub.add_parser("migrate", help="preview or apply typed-memory migration")
    migrate.add_argument("project")
    mode = migrate.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="show the non-destructive migration plan (default)")
    mode.add_argument("--apply", action="store_true", help="append migration candidates and rebuild generated views")

    sessions = sub.add_parser("sessions", help="preview or recover stale active session attachments")
    sessions.add_argument("--stale-after-hours", type=float, default=24.0)
    sessions.add_argument("--apply", action="store_true", help="checkpoint dirty state and detach stale sessions")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = root_from_env()
    if args.command == "doctor":
        result = continuity_doctor(root)
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 1 if result["issues"] or (args.strict and result["warnings"]) else 0
    if args.command == "migrate":
        result = continuity_migration.migrate(root, args.project, apply=bool(args.apply))
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("status") not in {"not_found", "error"} else 1
    if args.command == "sessions":
        result = project_workspace.recover_stale_sessions(
            root,
            stale_after_hours=args.stale_after_hours,
            apply=bool(args.apply),
        )
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
