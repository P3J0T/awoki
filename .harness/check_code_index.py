"""Standalone, stdlib-only inspection of an existing Awoki code index.

Does not import the deployed harness (its normal database opener can reset old
schemas), execute repository code, read provider configuration, or contact a
backend. Can also run over stdin against an older deployed installation.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path


SUPPORTED_SCHEMA = 4
CODE_FILES = (
    ".harness/code_search/store.py", ".harness/code_search/parser.py",
    ".harness/code_search/engine.py", ".harness/code_search/models.py",
    ".harness/code_index_jobs.py",
)
RELATIONSHIPS = (
    ("code_symbols", "file_id", "code_files", "file_id"),
    ("code_chunks", "file_id", "code_files", "file_id"),
    ("code_chunks", "symbol_id", "code_symbols", "symbol_id"),
    ("code_references", "file_id", "code_files", "file_id"),
    ("code_references", "source_symbol_id", "code_symbols", "symbol_id"),
    ("code_edges", "source_symbol_id", "code_symbols", "symbol_id"),
    ("code_edges", "target_symbol_id", "code_symbols", "symbol_id"),
)


def inspect_code(root: Path) -> dict:
    result = {}
    for relative in CODE_FILES:
        try:
            data = (root / relative).read_bytes()
            row = {"sha256": hashlib.sha256(data).hexdigest()}
            # Read literal version declarations; never import/execute this code.
            for node in ast.parse(data).body:
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id in {"SCHEMA_VERSION", "ENGINE_VERSION"}:
                            row[target.id] = node.value.value
            result[relative] = row
        except (OSError, SyntaxError, ValueError) as exc:
            result[relative] = {"status": "unavailable", "error_type": type(exc).__name__}
    return result


def inspect_database(path: Path, *, timeout_seconds: float = 15, sample_limit: int = 10) -> dict:
    if not 0 < timeout_seconds <= 120 or not 1 <= sample_limit <= 50:
        raise ValueError("timeout must be >0 and <=120 seconds; sample limit must be 1..50")
    report = {"path": str(path), "status": "incomplete", "read_only": True}
    if not path.is_file():
        return {**report, "status": "missing"}
    deadline = time.monotonic() + timeout_seconds
    try:
        # Do not use immutable=1: it can ignore live WAL contents. A normal
        # read-only SQLite connection may still use -shm bookkeeping.
        with closing(sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro", uri=True,
            timeout=min(timeout_seconds, 2),
        )) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            conn.execute("BEGIN")  # All observations refer to one read snapshot.
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            report["schema_version"] = version
            report["supported_schema_version"] = SUPPORTED_SCHEMA
            if version != SUPPORTED_SCHEMA:
                return {**report, "status": "unsupported_schema"}
            required = {
                "code_files": {"file_id", "project_id", "source_id", "revision_key"},
                "code_symbols": {"symbol_id", "file_id"},
                "code_chunks": {"chunk_id", "file_id", "symbol_id"},
                "code_references": {"reference_id", "file_id", "source_symbol_id"},
                "code_edges": {"source_symbol_id", "target_symbol_id"},
                "code_chunks_fts": {"chunk_id"},
            }
            missing = {}
            for table, expected in required.items():
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                if expected - columns:
                    missing[table] = sorted(expected - columns)
            report["missing_columns"] = missing
            if missing:
                return {**report, "status": "schema_mismatch"}
            report["quick_check"] = [row[0] for row in conn.execute("PRAGMA quick_check(10)")]
            constraints = {
                table: list(conn.execute(f"PRAGMA foreign_key_list({table})"))
                for table in {row[0] for row in RELATIONSHIPS}
            }
            missing_cascades = []
            orphans = {}
            for child, column, parent, parent_column in RELATIONSHIPS:
                label = f"{child}.{column} -> {parent}.{parent_column}"
                if not any(
                    row["from"] == column and row["table"] == parent
                    and row["to"] == parent_column and row["on_delete"].upper() == "CASCADE"
                    for row in constraints[child]
                ):
                    missing_cascades.append(label)
                orphans[label] = conn.execute(
                    f"SELECT COUNT(*) FROM {child} c WHERE c.{column} IS NOT NULL "
                    f"AND NOT EXISTS (SELECT 1 FROM {parent} p WHERE p.{parent_column}=c.{column})"
                ).fetchone()[0]
            report["missing_cascades"] = missing_cascades
            report["orphan_counts"] = orphans
            violations = conn.execute("PRAGMA foreign_key_check").fetchmany(sample_limit + 1)
            report["foreign_key_violations_sample"] = [dict(row) for row in violations[:sample_limit]]
            report["foreign_key_sample_truncated"] = len(violations) > sample_limit
            # These set queries avoid a quadratic correlated scan of FTS5.
            report["fts_missing_chunks"] = conn.execute(
                "SELECT COUNT(*) FROM (SELECT chunk_id FROM code_chunks EXCEPT SELECT chunk_id FROM code_chunks_fts)"
            ).fetchone()[0]
            report["fts_orphan_ids"] = conn.execute(
                "SELECT COUNT(*) FROM (SELECT chunk_id FROM code_chunks_fts EXCEPT SELECT chunk_id FROM code_chunks)"
            ).fetchone()[0]
            report["fts_duplicate_ids"] = conn.execute(
                "SELECT COUNT(*) FROM (SELECT chunk_id FROM code_chunks_fts GROUP BY chunk_id HAVING COUNT(*)>1)"
            ).fetchone()[0]
            report["row_counts"] = {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in required
            }
            problems = (
                report["quick_check"] != ["ok"] or missing_cascades or any(orphans.values())
                or violations or report["fts_missing_chunks"] or report["fts_orphan_ids"]
                or report["fts_duplicate_ids"]
            )
            report["status"] = "issues_found" if problems else "ok"
    except sqlite3.Error as exc:
        # No database-controlled error text or source values in diagnostic output.
        report.update(status="incomplete", error_type=type(exc).__name__,
                      sqlite_errorname=getattr(exc, "sqlite_errorname", "unknown"))
    except OSError as exc:
        report.update(status="incomplete", error_type=type(exc).__name__)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Awoki root in THIS installation (e.g. /awoki)")
    parser.add_argument("--project", required=True, help="Exact workspace project directory ID (e.g. poly-zero)")
    parser.add_argument("--timeout", type=float, default=15, help="Database query budget in seconds (default: 15; max: 120)")
    args = parser.parse_args(argv)
    if args.project in {".", ".."} or not args.project or any(c in args.project for c in "/\\\x00"):
        parser.error("--project must be one project directory name")
    if not 0 < args.timeout <= 120:
        parser.error("--timeout must be >0 and <=120")
    root = args.root.resolve()
    projects = root / "workspace" / "projects"
    project = projects / args.project
    database = project / "index" / "sqlite" / "awoki_code.sqlite"
    if not database.resolve().is_relative_to(projects.resolve()):
        parser.error("database path escapes the workspace projects directory")
    report = {
        "root": str(root), "project_id": args.project,
        "python_version": sys.version.split()[0], "sqlite_version": sqlite3.sqlite_version,
        "code_on_disk": inspect_code(root),
        "database": inspect_database(database, timeout_seconds=args.timeout),
        "notes": [
            "Code hashes describe this installation's files on disk, not code already loaded by another process.",
            "Read snapshot only: no schema/data repair, indexing, lock deletion, configuration reads, or network requests.",
            "Foreign-key enforcement is per connection; this check cannot prove a worker's PRAGMA setting.",
            "Vector memberships may intentionally retain an older snapshot; they are not classified as orphans.",
            "An empty code-index.lock is normal. Do not delete it while any index worker may be running.",
            "An ok database result does not prove indexing, parser identities, freshness, or Qdrant readiness.",
        ],
    }
    print(json.dumps(report, indent=2, ensure_ascii=True))
    if report["database"]["status"] != "ok":
        return 1
    return 0 if all("sha256" in row for row in report["code_on_disk"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
