from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

import project_workspace

from . import store, vector_store
from .engine import ENGINE_VERSION, cross_project_search, index_project_code, search_project_code
from .languages import parser_runtime_profile


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{number}: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"suite record at {path}:{number} must be an object")
        rows.append(parsed)
    return rows


FIXTURE_RECORD_TYPE = "fixture"
MAX_FIXTURE_PROJECTS = 20
MAX_FIXTURE_FILES = 500
MAX_FIXTURE_BYTES = 5_000_000
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")


def _query_records(records: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    fixtures = [row for row in records if str(row.get("type") or "query") == FIXTURE_RECORD_TYPE]
    if len(fixtures) > 1:
        raise ValueError("a golden suite may contain at most one fixture record")
    if fixtures and records[0] is not fixtures[0]:
        raise ValueError("the fixture record must be the first suite record")
    queries = [row for row in records if str(row.get("type") or "query") != FIXTURE_RECORD_TYPE]
    return (fixtures[0] if fixtures else None), queries


def _safe_fixture_path(value: str) -> Path:
    rel = Path(value)
    if not value or rel.is_absolute() or ".." in rel.parts or ".git" in rel.parts:
        raise ValueError(f"unsafe fixture repository path: {value!r}")
    return rel


def _safe_branch_name(value: str) -> str:
    branch = value.strip()
    if not _BRANCH_RE.fullmatch(branch) or ".." in branch or "@{" in branch or branch.endswith("/"):
        raise ValueError(f"unsafe fixture branch name: {value!r}")
    completed = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(f"invalid fixture branch name: {value!r}")
    return branch


def _fixture_paths(paths: Any, root: Path) -> Any:
    try:
        return type(paths)(root=root, global_root=root / ".awoki-global")
    except TypeError as exc:  # pragma: no cover - guards nonstandard callers
        raise TypeError("evaluation fixture mode requires an Awoki HarnessPaths-compatible object") from exc


def _count_fixture_bytes(counters: dict[str, int], encoded: bytes) -> None:
    counters["files"] += 1
    counters["bytes"] += len(encoded)
    if counters["files"] > MAX_FIXTURE_FILES:
        raise ValueError(f"fixture contains more than {MAX_FIXTURE_FILES} files")
    if counters["bytes"] > MAX_FIXTURE_BYTES:
        raise ValueError(f"fixture contains more than {MAX_FIXTURE_BYTES} UTF-8 bytes")


def _write_fixture_files(repo: Path, files: dict[str, Any], counters: dict[str, int]) -> None:
    for raw_rel, raw_content in files.items():
        rel = _safe_fixture_path(str(raw_rel))
        if not isinstance(raw_content, str):
            raise ValueError(f"fixture file {raw_rel!r} must contain UTF-8 text")
        encoded = raw_content.encode("utf-8")
        _count_fixture_bytes(counters, encoded)
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(encoded)


def _clear_fixture_worktree(repo: Path) -> None:
    for child in repo.iterdir():
        if child.name in {".git", "README.md"}:
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            shutil.rmtree(child)


def _git(repo: Path, *args: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
        raise RuntimeError(f"fixture git {' '.join(args)} failed: {detail}")


def _materialize_fixture_project(
    fixture_paths: Any,
    project_id: str,
    spec: dict[str, Any],
    counters: dict[str, int],
) -> None:
    pp = project_workspace.ensure_project_layout(fixture_paths.root, project_id)
    repo = pp.project_dir / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    files = spec.get("files")
    branches = spec.get("branches")
    if isinstance(files, dict) and branches is None:
        _write_fixture_files(repo, files, counters)
        return
    if not isinstance(branches, dict) or not branches:
        raise ValueError(
            f"fixture project {project_id!r} must contain either a files object or a non-empty branches object"
        )
    initial = _safe_branch_name(str(spec.get("initial_branch") or next(iter(branches))))
    if initial not in branches:
        raise ValueError(f"fixture project {project_id!r} initial branch {initial!r} is not declared")
    for raw_branch, branch_spec in branches.items():
        _safe_branch_name(str(raw_branch))
        if not isinstance(branch_spec, dict) or not isinstance(branch_spec.get("files"), dict):
            raise ValueError(f"fixture branch {raw_branch!r} must contain a files object")

    init = subprocess.run(
        ["git", "-C", str(repo), "init", "-q", "-b", initial],
        text=True,
        capture_output=True,
        check=False,
    )
    if init.returncode != 0:
        _git(repo, "init", "-q")
        _git(repo, "branch", "-M", initial)
    _git(repo, "config", "user.email", "awoki-eval@example.invalid")
    _git(repo, "config", "user.name", "Awoki Evaluation")

    ordered = [initial, *[str(name) for name in branches if str(name) != initial]]
    for position, branch in enumerate(ordered):
        branch = _safe_branch_name(branch)
        if position:
            _git(repo, "checkout", "-q", "-b", branch)
            _clear_fixture_worktree(repo)
        branch_spec = branches[branch]
        _write_fixture_files(repo, branch_spec["files"], counters)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"fixture {branch}")
    _git(repo, "checkout", "-q", initial)


def _parse_modes_by_language(databases: list[Path]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for db in databases:
        with closing(sqlite3.connect(db)) as conn:
            rows = conn.execute(
                "SELECT language, parse_mode, COUNT(*) FROM code_files GROUP BY language, parse_mode"
            ).fetchall()
        for language, parse_mode, count in rows:
            modes = result.setdefault(str(language), {})
            modes[str(parse_mode)] = modes.get(str(parse_mode), 0) + int(count)
    return result


def _cleanup_fixture_vectors(fixture_paths: Any, project_ids: list[str]) -> dict[str, Any]:
    cleaned_branches = 0
    removed_memberships = 0
    deleted_points = 0
    for project_id in project_ids:
        pp = project_workspace.paths_for(fixture_paths.root, project_id)
        db = store.db_path(pp.project_dir)
        if not db.exists():
            continue
        with closing(sqlite3.connect(db)) as conn:
            branches = [
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT branch_key FROM code_vector_memberships WHERE project_id=?",
                    (project_id,),
                ).fetchall()
            ]
        for branch_key in branches:
            old_rows = store.synced_vector_memberships(db, project_id, branch_key)
            if not old_rows:
                continue
            result = vector_store.sync_branch_memberships(
                project_id=project_id,
                branch_key=branch_key,
                old_rows=old_rows,
                new_rows=[],
            )
            if result.get("status") not in {"indexed", "current"}:
                raise RuntimeError(
                    f"failed to clean Qdrant fixture membership for {project_id}/{branch_key}: "
                    f"{result.get('reason') or result.get('status')}"
                )
            store.replace_synced_vector_memberships(db, project_id, branch_key, [])
            cleaned_branches += 1
            removed_memberships += int(result.get("removed_memberships") or 0)
            deleted_points += int(result.get("deleted_points") or 0)
    return {
        "status": "cleaned",
        "branches": cleaned_branches,
        "removed_memberships": removed_memberships,
        "deleted_points": deleted_points,
    }


@contextmanager
def _isolated_fixture_environment(paths: Any, fixture: dict[str, Any] | None) -> Iterator[tuple[Any, dict[str, Any]]]:
    if fixture is None:
        yield paths, {
            "isolated": False,
            "projects": [],
            "file_count": 0,
            "byte_count": 0,
            "include_qdrant": False,
        }
        return

    projects = fixture.get("projects")
    if not isinstance(projects, dict) or not projects:
        raise ValueError("fixture record must contain a non-empty projects object")
    if len(projects) > MAX_FIXTURE_PROJECTS:
        raise ValueError(f"fixture contains more than {MAX_FIXTURE_PROJECTS} projects")
    include_qdrant = bool(fixture.get("include_qdrant", False))
    required_languages = sorted({
        str(value).strip()
        for value in (fixture.get("require_tree_sitter_languages") or [])
        if str(value).strip()
    })

    with tempfile.TemporaryDirectory(prefix="awoki-code-eval-") as temp_dir:
        fixture_paths = _fixture_paths(paths, Path(temp_dir).resolve())
        counters = {"files": 0, "bytes": 0}
        project_ids: list[str] = []
        databases: list[Path] = []
        fixture_meta: dict[str, Any] = {
            "isolated": True,
            "projects": project_ids,
            "file_count": 0,
            "byte_count": 0,
            "include_qdrant": include_qdrant,
            "required_tree_sitter_languages": required_languages,
            "parse_modes_by_language": {},
            "qdrant_cleanup": {"status": "not_required"},
        }
        for raw_project_id, raw_spec in projects.items():
            project_id = project_workspace.clean_project_id(str(raw_project_id))
            if project_id != str(raw_project_id):
                raise ValueError(f"fixture project ID must already be normalized: {raw_project_id!r}")
            if not isinstance(raw_spec, dict):
                raise ValueError(f"fixture project {project_id!r} must be an object")
            _materialize_fixture_project(fixture_paths, project_id, raw_spec, counters)
            index_result = index_project_code(
                fixture_paths, project_id, include_qdrant=include_qdrant, force=True
            )
            if index_result.get("status") not in {"indexed", "current"}:
                raise RuntimeError(
                    f"failed to index fixture project {project_id}: "
                    f"{index_result.get('reason') or index_result.get('status')}"
                )
            if include_qdrant:
                vector = index_result.get("vector") if isinstance(index_result.get("vector"), dict) else {}
                if vector.get("status") not in {"indexed", "current"}:
                    raise RuntimeError(
                        f"live-Qdrant fixture indexing degraded for {project_id}: "
                        f"{vector.get('reason') or vector.get('status')}"
                    )
            project_ids.append(project_id)
            pp = project_workspace.paths_for(fixture_paths.root, project_id)
            databases.append(store.db_path(pp.project_dir))

        parse_modes = _parse_modes_by_language(databases)
        fixture_meta.update({
            "projects": sorted(project_ids),
            "file_count": counters["files"],
            "byte_count": counters["bytes"],
            "parse_modes_by_language": parse_modes,
        })
        missing_languages = [
            language
            for language in required_languages
            if int(parse_modes.get(language, {}).get("tree_sitter", 0)) < 1
        ]
        if missing_languages:
            profile = parser_runtime_profile()
            raise RuntimeError(
                "fixture requires Tree-sitter parsing for languages not observed structurally: "
                f"{', '.join(missing_languages)}; parser runtime={profile.get('version')}"
            )

        try:
            yield fixture_paths, fixture_meta
        finally:
            if include_qdrant:
                fixture_meta["qdrant_cleanup"] = _cleanup_fixture_vectors(fixture_paths, project_ids)


def _match(hit: dict[str, Any], expected: dict[str, Any]) -> bool:
    fields = (
        ("project_id", "project_id"),
        ("path", "path"),
        ("symbol", "symbol"),
        ("language", "language"),
        ("parse_mode", "parse_mode"),
        ("symbol_kind", "symbol_kind"),
        ("branch_key", "branch_key"),
    )
    for key, hit_key in fields:
        wanted = str(expected.get(key) or "")
        if wanted and str(hit.get(hit_key) or "") != wanted:
            return False
    return True


def _grade(hit: dict[str, Any], expected: list[dict[str, Any]]) -> int:
    return max((int(item.get("grade") or 1) for item in expected if _match(hit, item)), default=0)


def _dcg(grades: list[int], k: int) -> float:
    total = 0.0
    for index, grade in enumerate(grades[:k], start=1):
        total += (2**grade - 1) / math.log2(index + 1)
    return total


def _checkout_fixture_branch(paths: Any, project_id: str, branch: str) -> None:
    branch = _safe_branch_name(branch)
    pp = project_workspace.paths_for(paths.root, project_id)
    repo = pp.project_dir / "repo"
    if not (repo / ".git").exists():
        raise ValueError(f"query requested checkout_branch for non-Git fixture project {project_id!r}")
    _git(repo, "checkout", "-q", branch)


def _observed_backends(hits: list[dict[str, Any]]) -> set[str]:
    observed: set[str] = set()
    for hit in hits:
        backend = str(hit.get("retrieval_backend") or "")
        if backend:
            observed.add(backend)
        for value in hit.get("retrieval_backends") or []:
            if str(value):
                observed.add(str(value))
    return observed


def _evaluate_record(paths: Any, record: dict[str, Any]) -> dict[str, Any]:
    query = str(record.get("query") or "").strip()
    projects = [str(value) for value in (record.get("projects") or []) if str(value).strip()]
    if not projects and not bool(record.get("all_indexed", False)):
        raise ValueError(f"evaluation query {record.get('id') or query!r} must declare projects or all_indexed")
    checkout_branch = str(record.get("checkout_branch") or "").strip()
    if checkout_branch:
        if len(projects) != 1:
            raise ValueError("checkout_branch requires exactly one fixture project")
        _checkout_fixture_branch(paths, projects[0], checkout_branch)
    mode = str(record.get("mode") or "auto")
    view = str(record.get("view") or "peek")
    limit = int(record.get("limit") or 10)
    refresh_index = bool(record.get("refresh_index", False) or checkout_branch)
    started = time.perf_counter()
    if len(projects) == 1:
        result = search_project_code(
            paths,
            projects[0],
            query,
            mode=mode,
            view=view,
            limit=limit,
            refresh_index=refresh_index,
            include_qdrant=bool(record.get("include_qdrant", False)),
        )
    else:
        result = cross_project_search(
            paths,
            query,
            projects=projects,
            all_indexed=bool(record.get("all_indexed", False)),
            mode=mode,
            view=view,
            limit=limit,
            refresh_stale=refresh_index,
        )
    latency_ms = (time.perf_counter() - started) * 1000.0
    hits = list(result.get("hits") or [])
    expected = list(record.get("expected") or [])
    forbidden = [str(value) for value in (record.get("forbidden_paths") or [])]
    grades = [_grade(hit, expected) for hit in hits]
    relevant_ranks = [index for index, grade in enumerate(grades, start=1) if grade > 0]
    reciprocal_rank = 1.0 / relevant_ranks[0] if relevant_ranks else 0.0
    ideal = sorted((int(item.get("grade") or 1) for item in expected), reverse=True)
    ideal_dcg = _dcg(ideal, 10)
    leakage = [
        str(hit.get("path") or "")
        for hit in hits
        if any(
            str(hit.get("path") or "") == path or str(hit.get("path") or "").endswith(path)
            for path in forbidden
        )
    ]
    expected_no_answer = bool(record.get("expected_no_answer", False))
    no_answer_correct = (not hits) if expected_no_answer else bool(relevant_ranks)
    expected_branch = str(record.get("expected_branch") or "")
    branch_leakage = [
        {
            "project_id": hit.get("project_id"),
            "path": hit.get("path"),
            "branch_key": hit.get("branch_key"),
        }
        for hit in hits
        if expected_branch and str(hit.get("branch_key") or "") != expected_branch
    ]
    selected_mode = (result.get("routing") or {}).get("selected_mode")
    expected_mode = str(record.get("expected_mode") or "")
    routing_correct = not expected_mode or selected_mode == expected_mode
    observed_backends = _observed_backends(hits)
    required_backends = sorted({str(value) for value in (record.get("required_backends") or []) if str(value)})
    missing_backends = [backend for backend in required_backends if backend not in observed_backends]
    index_result = result.get("index") if isinstance(result.get("index"), dict) else {}
    vector_result = index_result.get("vector") if isinstance(index_result.get("vector"), dict) else {}
    return {
        "id": str(record.get("id") or query),
        "query": query,
        "projects": projects,
        "checkout_branch": checkout_branch,
        "status": result.get("status"),
        "selected_mode": selected_mode,
        "expected_mode": expected_mode,
        "routing_correct": int(routing_correct),
        "latency_ms": round(latency_ms, 3),
        "hit_count": len(hits),
        "hit_at_1": int(any(rank <= 1 for rank in relevant_ranks)),
        "hit_at_3": int(any(rank <= 3 for rank in relevant_ranks)),
        "hit_at_5": int(any(rank <= 5 for rank in relevant_ranks)),
        "reciprocal_rank": reciprocal_rank,
        "ndcg_at_10": (_dcg(grades, 10) / ideal_dcg)
        if ideal_dcg
        else (1.0 if expected_no_answer and not hits else 0.0),
        "expected_no_answer": expected_no_answer,
        "returned_no_answer": not hits,
        "no_answer_correct": int(no_answer_correct),
        "forbidden_leakage": leakage,
        "branch_leakage": branch_leakage,
        "required_backends": required_backends,
        "observed_backends": sorted(observed_backends),
        "missing_required_backends": missing_backends,
        "indexing": {
            "new_vectors": int(vector_result.get("new_vectors") or 0),
            "reused_vectors": int(vector_result.get("reused_vectors") or 0),
            "removed_memberships": int(vector_result.get("removed_memberships") or 0),
            "changed_files": len(index_result.get("changed_files") or []),
            "reused_files": len(index_result.get("reused_files") or []),
        },
        "top_hits": [
            {
                "project_id": hit.get("project_id"),
                "branch_key": hit.get("branch_key"),
                "path": hit.get("path"),
                "symbol": hit.get("symbol"),
                "language": hit.get("language"),
                "parse_mode": hit.get("parse_mode"),
                "retrieval_backends": hit.get("retrieval_backends") or [hit.get("retrieval_backend")],
                "score": hit.get("score"),
                "grade": grades[index],
            }
            for index, hit in enumerate(hits[:10])
        ],
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def run_suite(paths: Any, suite_path: Path, *, report_path: Path | None = None) -> dict[str, Any]:
    records = _load_jsonl(suite_path)
    fixture, queries = _query_records(records)
    if fixture is not None and not bool(fixture.get("include_qdrant", False)):
        vector_queries = [str(row.get("id") or row.get("query") or "") for row in queries if bool(row.get("include_qdrant", False))]
        if vector_queries:
            raise ValueError(
                "isolated fixture queries request Qdrant but the fixture was not indexed with include_qdrant=true: "
                + ", ".join(vector_queries)
            )
    with _isolated_fixture_environment(paths, fixture) as (evaluation_paths, fixture_meta):
        if not queries:
            raise ValueError("a golden suite must contain at least one query record")
        results = [_evaluate_record(evaluation_paths, record) for record in queries]
    count = len(results)
    latencies = [float(row["latency_ms"]) for row in results]
    positive_rows = [row for row in results if not row["expected_no_answer"]]
    positive_count = len(positive_rows)
    expected_no_answer_rows = [row for row in results if row["expected_no_answer"]]
    abstained_rows = [row for row in results if row["returned_no_answer"]]
    summary = {
        "query_count": count,
        "positive_query_count": positive_count,
        "no_answer_query_count": len(expected_no_answer_rows),
        "hit_at_1": sum(row["hit_at_1"] for row in positive_rows) / positive_count if positive_count else 0.0,
        "hit_at_3": sum(row["hit_at_3"] for row in positive_rows) / positive_count if positive_count else 0.0,
        "hit_at_5": sum(row["hit_at_5"] for row in positive_rows) / positive_count if positive_count else 0.0,
        "mrr_at_10": sum(row["reciprocal_rank"] for row in positive_rows) / positive_count if positive_count else 0.0,
        "ndcg_at_10": sum(row["ndcg_at_10"] for row in positive_rows) / positive_count if positive_count else 0.0,
        "no_answer_accuracy": sum(row["no_answer_correct"] for row in results) / count if count else 0.0,
        "no_answer_recall": (
            sum(1 for row in expected_no_answer_rows if row["returned_no_answer"]) / len(expected_no_answer_rows)
            if expected_no_answer_rows
            else 0.0
        ),
        "no_answer_precision": (
            sum(1 for row in abstained_rows if row["expected_no_answer"]) / len(abstained_rows)
            if abstained_rows
            else 0.0
        ),
        "forbidden_path_leakage_count": sum(len(row["forbidden_leakage"]) for row in results),
        "cross_branch_leakage_count": sum(len(row["branch_leakage"]) for row in results),
        "required_backend_failure_count": sum(bool(row["missing_required_backends"]) for row in results),
        "routing_mismatch_count": sum(not bool(row["routing_correct"]) for row in results),
        "status_failure_count": sum(row["status"] != "ok" for row in results),
        "new_vectors": sum(row["indexing"]["new_vectors"] for row in results),
        "reused_vectors": sum(row["indexing"]["reused_vectors"] for row in results),
        "latency_p50_ms": round(_percentile(latencies, 0.50), 3),
        "latency_p95_ms": round(_percentile(latencies, 0.95), 3),
    }
    acceptance = {
        "passed": all(
            row["status"] == "ok"
            and bool(row["no_answer_correct"])
            and not row["forbidden_leakage"]
            and not row["branch_leakage"]
            and not row["missing_required_backends"]
            and bool(row["routing_correct"])
            for row in results
        ),
        "rules": [
            "every query completed with status=ok",
            "every positive query returned a graded result and every no-answer query abstained",
            "no forbidden path or cross-branch leakage",
            "every explicitly required retrieval backend contributed",
            "every explicitly expected router mode matched",
        ],
    }
    suite_bytes = suite_path.read_bytes()
    payload = {
        "status": "ok",
        "suite": str(suite_path),
        "suite_sha256": hashlib.sha256(suite_bytes).hexdigest(),
        "engine_version": ENGINE_VERSION,
        "parser_runtime": parser_runtime_profile(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fixture": fixture_meta,
        "summary": summary,
        "acceptance": acceptance,
        "results": results,
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        payload["report"] = str(report_path)
    return payload
