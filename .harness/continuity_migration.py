"""Non-destructive migration from typed project JSONL stores to continuity.jsonl."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import continuity
import project_workspace


def migration_plan(root: Path, name: str) -> dict[str, Any]:
    pp = project_workspace.paths_for(root, name)
    if not pp.project_json.exists():
        return {"status": "not_found", "project_id": pp.project_id}

    canonical = continuity.read_jsonl(pp.continuity)
    canonical_ids = {str(row.get("id")) for row in canonical if row.get("id")}
    canonical_fingerprints = {str(row.get("fingerprint")) for row in canonical if row.get("fingerprint")}
    candidates: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []

    for filename, default_kind in continuity.LEGACY_MEMORY_FILES.items():
        path = pp.memory_dir / filename
        for row in continuity.read_jsonl(path):
            if row.get("kind") == "parse_error":
                parse_errors.append({"path": str(path), "line": row.get("_line"), "summary": row.get("summary")})
                continue
            if row.get("kind") == "pending_resolution":
                continue
            converted = continuity.legacy_to_record(pp.project_id, row, default_kind)
            descriptor = {
                "source": str(path.relative_to(pp.project_dir)),
                "line": row.get("_line"),
                "legacy_id": row.get("id"),
                "record_id": converted.get("id"),
                "kind": converted.get("kind"),
                "summary": converted.get("summary"),
                "fingerprint": converted.get("fingerprint"),
                "index_policy": converted.get("index_policy"),
            }
            if str(converted.get("id")) in canonical_ids or str(converted.get("fingerprint")) in canonical_fingerprints:
                duplicates.append(descriptor)
            else:
                candidates.append({**descriptor, "record": converted})
                canonical_ids.add(str(converted.get("id")))
                canonical_fingerprints.add(str(converted.get("fingerprint")))

    indexable_candidates = [item for item in candidates if item["record"].get("index_policy") == "safe"]
    excluded_candidates = [item for item in candidates if item["record"].get("index_policy") != "safe"]
    return {
        "status": "preview",
        "project_id": pp.project_id,
        "canonical_path": str(pp.continuity),
        "legacy_files_retained": True,
        "candidate_count": len(candidates),
        "duplicate_count": len(duplicates),
        "parse_error_count": len(parse_errors),
        "record_index_preview": {
            "indexable_candidate_count": len(indexable_candidates),
            "excluded_candidate_count": len(excluded_candidates),
            "excluded": [
                {key: item.get(key) for key in ("source", "line", "record_id", "kind", "summary", "index_policy")}
                for item in excluded_candidates
            ],
        },
        "candidates": candidates,
        "duplicates": duplicates,
        "parse_errors": parse_errors,
    }


def migrate(root: Path, name: str, *, apply: bool = False) -> dict[str, Any]:
    plan = migration_plan(root, name)
    if plan.get("status") == "not_found" or not apply:
        return plan
    pp = project_workspace.paths_for(root, name)
    appended: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for candidate in plan["candidates"]:
        saved = continuity.append_record(pp.continuity, candidate["record"])
        result = {key: value for key, value in candidate.items() if key != "record"}
        if saved.get("_write_status") == "appended":
            project_workspace.register_appended_record(root, pp.project_id, saved)
            appended.append(result)
        else:
            skipped.append(result)
    refresh = project_workspace.refresh_project_files(root, pp.project_id)
    try:
        from harness_core import HarnessPaths, project_index_preview
        index_preview = project_index_preview(
            pp.project_id,
            include_artifacts=True,
            include_code=False,
            paths=HarnessPaths(root=root, global_root=root / ".awoki-global"),
        )
    except Exception as exc:  # pragma: no cover - diagnostics only
        index_preview = {"status": "warning", "reason": f"index_preview_failed:{exc}"}
    return {
        "status": "migrated",
        "project_id": pp.project_id,
        "appended_count": len(appended),
        "skipped_count": len(skipped) + int(plan["duplicate_count"]),
        "parse_error_count": plan["parse_error_count"],
        "legacy_files_retained": True,
        "appended": appended,
        "skipped": skipped + plan["duplicates"],
        "refresh": refresh,
        "index_preview": index_preview,
    }
