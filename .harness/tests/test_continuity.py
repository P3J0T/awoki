from __future__ import annotations

import json
import gzip
import multiprocessing
import os
import subprocess
import shutil
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import project_workspace
import code_vector_jobs
import code_index_jobs
import rag_backend
import opencode_events
import agent_runtime
import work_ledger
import acceptance_runs
import evidence_store
import continuations
import continuity_migration
import indexing_policy
import awoki
from harness_core import (
    HarnessPaths,
    harness_self_check,
    project_capture,
    codebase_search,
    code_exact_search,
    project_create,
    project_index_preview,
    project_fts_db,
    project_open,
    project_repo_add,
    project_repo_list,
    project_repo_remove,
    project_repo_default,
    project_pause,
    project_records,
    project_refresh,
    code_index_refresh_start,
    code_index_refresh_status,
    code_index_refresh_cancel,
    code_vector_refresh_start,
    code_vector_refresh_status,
    code_vector_refresh_cancel,
    project_resume,
    project_search,
    project_status,
    project_task_checkpoint,
    project_task_status,
    project_task_finalize,
    session_work_status,
    session_runtime_status,
    reference_describe,
    reference_annotate,
    reference_resolve,
    acceptance_run_start,
    acceptance_run_status,
    acceptance_run_next,
    acceptance_evidence_get,
    acceptance_run_record,
    acceptance_run_record_invariant,
    acceptance_run_finalize,
    project_continuation_schedule,
    project_continuation_status,
    project_continuation_cancel,
    project_continuation_finalize,
    save_project_fact,
    search_global_memory,
    search_records,
    save_global_fact,
)




def _concurrent_capture_worker(root_text: str, prefix: str, count: int) -> None:
    root = Path(root_text)
    for index in range(count):
        project_workspace.project_capture(
            root,
            "demo",
            f"{prefix}-{index}",
            kind="observation",
            refresh=False,
            sync_index=False,
        )


class ProjectStateCoverageTests(unittest.TestCase):
    def test_git_status_path_preserves_security_relevant_filenames(self):
        self.assertEqual(project_workspace._safe_git_status_path(".env"), ".env")
        self.assertEqual(project_workspace._safe_git_status_path("credentials/auth.json"), "credentials/auth.json")
        self.assertEqual(project_workspace._safe_git_status_path("secrets/token.txt"), "secrets/token.txt")

    def test_repository_state_rejects_parent_git_root_mismatch(self):
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            subprocess.run(["git", "init"], cwd=pp.project_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            (pp.project_dir / "repo" / ".env").write_text("TOKEN=value\n", encoding="utf-8")
            state = project_workspace._repository_state(pp)
            self.assertTrue(state["invalid_repo_root"])
            self.assertNotEqual(Path(state["git_root"]).resolve(), (pp.project_dir / "repo").resolve())

    def test_mcp_native_repo_management_infers_child_path_and_reports_index_advice(self):
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            session_id = "repo-management-session"
            project_create("demo", session_id=session_id, paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            repo = pp.project_dir / "repo" / "oathkeeper"
            repo.mkdir(parents=True)
            (repo / "main.go").write_text("package main\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)

            added = project_repo_add("oathkeeper", session_id=session_id, paths=paths)
            self.assertEqual(added["status"], "registered", added)
            self.assertEqual(added["path"], "repo/oathkeeper")
            self.assertTrue(added["repository"]["git"])
            advice = added["repository_index_advice"]
            self.assertEqual(advice["status"], "semantic_refresh_recommended")
            self.assertEqual(advice["recommended_action"]["tool"], "code_vector_refresh_start")
            self.assertEqual(advice["recommended_action"]["arguments"]["name"], "demo")
            self.assertEqual(advice["refresh_execution"], "background")

            listed = project_repo_list(session_id=session_id, paths=paths)
            self.assertEqual([row["repo_id"] for row in listed["repositories"]], ["oathkeeper"])
            self.assertIn(project_repo_default("oathkeeper", session_id=session_id, paths=paths)["status"], {"default_set", "already_default"})
            removed = project_repo_remove("oathkeeper", session_id=session_id, paths=paths)
            self.assertEqual(removed["status"], "removed")

    def test_project_open_surfaces_semantic_index_instinct_for_registered_repo(self):
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            repo = pp.project_dir / "repo" / "service"
            repo.mkdir(parents=True)
            (repo / "main.py").write_text("def entry():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
            project_workspace.project_repo_add(root, "demo", "service", "repo/service", default=True)

            opened = project_open("demo", paths=paths)
            advice = opened["repository_index_advice"]
            self.assertEqual(advice["status"], "semantic_refresh_recommended")
            self.assertTrue(advice["action_required"])
            self.assertEqual(advice["repositories"][0]["repo_id"], "service")
            self.assertFalse(advice["repositories"][0]["vector_current"])
            self.assertIn("do not trigger remote embedding implicitly", advice["message"])

    def test_code_index_refresh_start_is_detached_and_status_is_pollable(self):
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            repo = pp.project_dir / "repo" / "service"
            repo.mkdir(parents=True)
            (repo / "main.py").write_text("def entry():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
            project_workspace.project_repo_add(root, "demo", "service", "repo/service", default=True)

            fake_proc = mock.Mock(pid=43110)
            with mock.patch.object(code_index_jobs.subprocess, "Popen", return_value=fake_proc):
                started = code_index_refresh_start(name="demo", repo="service", paths=paths)
            self.assertEqual(started["status"], "started", started)
            self.assertEqual(started["job"]["pid"], 43110)
            self.assertFalse(started["network"])
            self.assertFalse(started["remote_embedding"])

            with mock.patch.object(code_index_jobs, "_pid_alive", return_value=True):
                code_index_jobs._update_progress(
                    code_index_jobs._state_path(root, "demo", started["job"]["job_id"]),
                    "service",
                    {
                        "phase": "structural_index",
                        "files_total": 4000,
                        "files_processed": 1200,
                        "files_parsed": 1180,
                        "files_reused": 20,
                        "files_removed": 0,
                        "current_path": "rule/matching_engine.go",
                        "parse_modes": {"tree_sitter": 1180},
                        "progress_percent": 27.0,
                    },
                )
                polled = code_index_refresh_status(name="demo", repo="service", paths=paths)
                self.assertEqual(polled["status"], "ok", polled)
                self.assertEqual(polled["job"]["status"], "running")
                self.assertEqual(polled["progress"]["phase"], "structural_index")
                self.assertEqual(polled["progress"]["files_total"], 4000)
                self.assertEqual(polled["progress"]["files_processed"], 1200)
                self.assertEqual(polled["progress"]["current_path"], "rule/matching_engine.go")
                self.assertEqual(polled["progress"]["parse_modes"]["tree_sitter"], 1180)
                duplicate = code_index_refresh_start(name="demo", repo="service", paths=paths)
                self.assertEqual(duplicate["status"], "already_running", duplicate)
                opened = project_open("demo", paths=paths)
                advice = opened["repository_index_advice"]
                self.assertEqual(advice["status"], "structural_refresh_running", advice)
                self.assertEqual(advice["recommended_action"]["tool"], "code_index_refresh_status")

            with mock.patch.object(code_index_jobs, "_pid_alive", return_value=True), \
                 mock.patch.object(code_index_jobs.os, "kill") as kill:
                cancelled = code_index_refresh_cancel(started["job"]["job_id"], name="demo", paths=paths)
                self.assertEqual(cancelled["status"], "cancelled", cancelled)
                kill.assert_called()

    def test_code_index_refresh_worker_starts_with_credential_free_environment(self):
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            repo = pp.project_dir / "repo" / "service"
            repo.mkdir(parents=True)
            (repo / "main.py").write_text("def entry():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
            project_workspace.project_repo_add(root, "demo", "service", "repo/service", default=True)

            fake_proc = mock.Mock(pid=43112)
            poisoned = {
                "AWOKI_EMBEDDING_API_KEY": "embedding-secret",
                "AWOKI_RERANK_URL": "http://sensitive-rerank.invalid",
                "AWOKI_QDRANT_URL": "http://sensitive-qdrant.invalid",
                "OPENAI_API_KEY": "openai-secret",
                "SSH_AUTH_SOCK": "/tmp/fake-agent.sock",
                "GIT_SSH_COMMAND": "evil-helper",
                "PYTHONPATH": "/tmp/untrusted-pythonpath",
            }
            with mock.patch.dict(os.environ, poisoned, clear=False), \
                 mock.patch.object(code_index_jobs.subprocess, "Popen", return_value=fake_proc) as popen:
                started = code_index_refresh_start(name="demo", repo="service", paths=paths)
            self.assertEqual(started["status"], "started", started)
            env = popen.call_args.kwargs.get("env")
            self.assertIsInstance(env, dict)
            for name in poisoned:
                self.assertNotIn(name, env)
            self.assertIn("PATH", env)

    def test_code_index_worker_uses_local_only_index_and_persists_progress(self):
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            repo = pp.project_dir / "repo" / "service"
            repo.mkdir(parents=True)
            (repo / "main.py").write_text("def entry():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
            project_workspace.project_repo_add(root, "demo", "service", "repo/service", default=True)

            fake_proc = mock.Mock(pid=43111)
            with mock.patch.object(code_index_jobs.subprocess, "Popen", return_value=fake_proc):
                started = code_index_refresh_start(name="demo", repo="service", paths=paths)
            job_id = started["job"]["job_id"]

            def fake_index(*args, progress_callback=None, include_qdrant=None, force=None, **kwargs):
                self.assertFalse(include_qdrant)
                self.assertFalse(force)
                self.assertIsNotNone(progress_callback)
                progress_callback({
                    "phase": "structural_index", "files_total": 10, "files_processed": 4,
                    "files_parsed": 4, "files_reused": 0, "files_removed": 0,
                    "current_path": "main.py", "parse_modes": {"python_ast": 4}, "progress_percent": 36.0,
                })
                progress_callback({
                    "phase": "structural_complete", "files_total": 10, "files_processed": 10,
                    "files_parsed": 10, "files_reused": 0, "files_removed": 0,
                    "current_path": "", "parse_modes": {"python_ast": 10}, "progress_percent": 100.0,
                })
                return {"status": "indexed", "vector": {"status": "stale"}}

            with mock.patch.object(code_index_jobs.code_search, "index_project_code", side_effect=fake_index), \
                 mock.patch.object(code_index_jobs.code_search, "index_status", return_value={"status": "degraded", "freshness": {"lexical_current": True, "vector_current": False}}):
                rc = code_index_jobs._worker(root, "demo", job_id)
            self.assertEqual(rc, 0)
            polled = code_index_refresh_status(name="demo", repo="service", job_id=job_id, paths=paths)
            self.assertEqual(polled["job"]["status"], "completed")
            self.assertEqual(polled["progress"]["phase"], "completed")
            self.assertEqual(polled["progress"]["files_processed"], 10)
            self.assertEqual(polled["job"]["results"][0]["freshness_after"]["lexical_current"], True)
            self.assertEqual(polled["job"]["results"][0]["freshness_after"]["vector_current"], False)

    def test_codebase_refresh_index_routes_to_detached_local_job(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            project_workspace.project_repo_add(root, "demo", "service", "repo/service", default=True)
            fake = {"status": "started", "job": {"job_id": "cir_fixture"}}
            with mock.patch.object(code_index_jobs, "start", return_value=fake) as start:
                result = codebase_search(
                    "MatchingEngine", name="demo", repo="service", mode="lexical",
                    refresh_index=True, use_qdrant=False, use_reranker=False, paths=paths,
                )
            self.assertEqual(result["status"], "refresh_started", result)
            self.assertEqual(result["recommended_action"]["tool"], "code_index_refresh_status")
            start.assert_called_once_with(root, "demo", repo="service", source_id="", force=True)

    def test_code_vector_refresh_start_is_detached_and_status_is_pollable(self):
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            repo = pp.project_dir / "repo" / "service"
            repo.mkdir(parents=True)
            (repo / "main.py").write_text("def entry():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
            project_workspace.project_repo_add(root, "demo", "service", "repo/service", default=True)

            fake_proc = mock.Mock(pid=43210)
            with mock.patch.object(code_vector_jobs.subprocess, "Popen", return_value=fake_proc):
                started = code_vector_refresh_start(name="demo", repo="service", paths=paths)
            self.assertEqual(started["status"], "started", started)
            self.assertEqual(started["job"]["pid"], 43210)
            self.assertEqual(started["job"]["repositories"], ["service"])
            with mock.patch.object(code_vector_jobs, "_pid_alive", return_value=True):
                code_vector_jobs._update_progress(
                    code_vector_jobs._state_path(root, "demo", started["job"]["job_id"]),
                    "service",
                    {
                        "phase": "embedding",
                        "chunks_total": 4596,
                        "chunks_ready": 384,
                        "target_vectors_total": 4596,
                        "vectors_reused_content": 0,
                        "vectors_to_embed": 4596,
                        "vectors_persisted": 384,
                        "vectors_ready": 384,
                        "vectors_remaining": 4212,
                        "batches_total": 144,
                        "batches_completed": 12,
                        "progress_percent": 8.4,
                        "qdrant_collection": "fixture_code_v1",
                    },
                )
                polled = code_vector_refresh_status(name="demo", repo="service", paths=paths)
                self.assertEqual(polled["status"], "ok", polled)
                self.assertEqual(polled["job"]["status"], "running")
                self.assertEqual(polled["progress"]["phase"], "embedding")
                self.assertEqual(polled["progress"]["chunks_total"], 4596)
                self.assertEqual(polled["progress"]["chunks_ready"], 384)
                self.assertEqual(polled["progress"]["vectors_persisted"], 384)
                self.assertEqual(polled["progress"]["batches_completed"], 12)
                self.assertEqual(polled["progress"]["progress_percent"], 8.4)
                self.assertGreaterEqual(polled["recommended_poll_after_seconds"], 5)
                immediate = code_vector_refresh_status(name="demo", repo="service", paths=paths)
                self.assertTrue(immediate["poll_too_soon"], immediate)
                self.assertGreater(immediate["retry_after_seconds"], 0)
                self.assertEqual(immediate["progress"]["vectors_persisted"], 384)
                duplicate = code_vector_refresh_start(name="demo", repo="service", paths=paths)
                self.assertEqual(duplicate["status"], "already_running", duplicate)
                opened = project_open("demo", paths=paths)
                running_advice = opened["repository_index_advice"]
                self.assertEqual(running_advice["status"], "semantic_refresh_running", running_advice)
                self.assertEqual(running_advice["recommended_action"]["tool"], "code_vector_refresh_status")

            with mock.patch.object(code_vector_jobs, "_pid_alive", return_value=True), \
                 mock.patch.object(code_vector_jobs.os, "kill") as kill:
                cancelled = code_vector_refresh_cancel(started["job"]["job_id"], name="demo", paths=paths)
                self.assertEqual(cancelled["status"], "cancelled", cancelled)
                kill.assert_called()

    def test_code_vector_worker_persists_callback_progress_without_overwrite(self):
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            repo = pp.project_dir / "repo" / "service"
            repo.mkdir(parents=True)
            (repo / "main.py").write_text("def entry():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "initial"], check=True, capture_output=True)
            project_workspace.project_repo_add(root, "demo", "service", "repo/service", default=True)

            fake_proc = mock.Mock(pid=43210)
            with mock.patch.object(code_vector_jobs.subprocess, "Popen", return_value=fake_proc):
                started = code_vector_refresh_start(name="demo", repo="service", paths=paths)
            job_id = started["job"]["job_id"]

            def fake_index(*args, progress_callback=None, **kwargs):
                self.assertIsNotNone(progress_callback)
                progress_callback({
                    "phase": "embedding",
                    "chunks_total": 10,
                    "chunks_ready": 4,
                    "target_vectors_total": 10,
                    "vectors_reused_content": 0,
                    "vectors_to_embed": 10,
                    "vectors_persisted": 4,
                    "vectors_ready": 4,
                    "vectors_remaining": 6,
                    "batches_total": 3,
                    "batches_completed": 1,
                    "progress_percent": 40.0,
                    "qdrant_collection": "fixture_code_v1",
                })
                progress_callback({
                    "phase": "finalizing",
                    "chunks_total": 10,
                    "chunks_ready": 10,
                    "target_vectors_total": 10,
                    "vectors_reused_content": 0,
                    "vectors_to_embed": 10,
                    "vectors_persisted": 10,
                    "vectors_ready": 10,
                    "vectors_remaining": 0,
                    "batches_total": 3,
                    "batches_completed": 3,
                    "progress_percent": 100.0,
                    "qdrant_collection": "fixture_code_v1",
                })
                return {"status": "indexed", "vector": {"status": "indexed", "new_vectors": 10}}

            with mock.patch.object(code_vector_jobs.code_search, "index_project_code", side_effect=fake_index):
                rc = code_vector_jobs._worker(root, "demo", job_id)
            self.assertEqual(rc, 0)
            polled = code_vector_refresh_status(name="demo", repo="service", job_id=job_id, paths=paths)
            self.assertEqual(polled["job"]["status"], "completed")
            self.assertEqual(polled["progress"]["phase"], "completed")
            self.assertEqual(polled["progress"]["chunks_ready"], 10)
            self.assertEqual(polled["progress"]["vectors_persisted"], 10)
            self.assertEqual(polled["progress"]["batches_completed"], 3)
            self.assertEqual(polled["progress"]["progress_percent"], 100.0)

    def test_code_vector_refresh_accepts_explicit_non_git_source_without_changing_repo_default_scope(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            source_root = pp.sources_dir / "smali"
            source_root.mkdir(parents=True)
            (source_root / "A.smali").write_text(".class public LA;\n.super Ljava/lang/Object;\n", encoding="utf-8")
            project_workspace.project_source_add(root, "demo", "smali", "sources/smali", source_type="smali")

            fake_proc = mock.Mock(pid=43210)
            with mock.patch.object(code_vector_jobs.subprocess, "Popen", return_value=fake_proc):
                started = code_vector_refresh_start(name="demo", source_id="smali", paths=paths)
            self.assertEqual(started["status"], "started", started)
            self.assertEqual(started["job"]["scope_type"], "source")
            self.assertEqual(started["job"]["sources"], ["smali"])
            self.assertEqual(started["job"]["repositories"], [])

            with mock.patch.object(code_vector_jobs.code_search, "index_project_code", return_value={"status": "indexed", "vector": {"status": "indexed"}}) as index_code:
                rc = code_vector_jobs._worker(root, "demo", started["job"]["job_id"])
            self.assertEqual(rc, 0)
            index_code.assert_called_once()
            kwargs = index_code.call_args.kwargs
            self.assertEqual(kwargs["source"], "smali")
            self.assertEqual(kwargs["repo"], "")
            self.assertTrue(kwargs["include_qdrant"])
            polled = code_vector_refresh_status(
                name="demo", source_id="smali", job_id=started["job"]["job_id"], paths=paths
            )
            self.assertEqual(polled["job"]["status"], "completed", polled)
            self.assertEqual(polled["progress"]["scope_type"], "source")
            self.assertEqual(polled["progress"]["current_repository"], "")
            self.assertEqual(polled["progress"]["sources"]["smali"]["source_id"], "smali")

            with mock.patch.object(code_vector_jobs.subprocess, "Popen", return_value=fake_proc):
                repository_default = code_vector_refresh_start(name="demo", paths=paths)
            self.assertEqual(repository_default["status"], "started", repository_default)
            self.assertEqual(repository_default["job"]["scope_type"], "repository")
            self.assertEqual(repository_default["job"]["sources"], [])
            self.assertNotIn("smali", repository_default["job"]["scope_ids"])

    def test_mcp_repo_add_rejects_paths_outside_project_repo_before_registration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            outside = root / "outside"
            outside.mkdir()

            escaped = project_repo_add("outside", path="../outside", name="demo", paths=paths)
            self.assertEqual(escaped["status"], "rejected", escaped)
            self.assertIn("under repo/", escaped["reason"])

            absolute = project_repo_add("outside", path=str(outside), name="demo", paths=paths)
            self.assertEqual(absolute["status"], "rejected", absolute)
            self.assertIn("under repo/", absolute["reason"])

            container = project_repo_add("container", path="repo", name="demo", paths=paths)
            self.assertEqual(container["status"], "rejected", container)
            self.assertEqual(project_workspace.project_repository_registry(root, "demo")["mode"], "legacy")

    def test_mcp_repo_add_refuses_nested_git_root(self):
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            container = pp.project_dir / "repo"
            subprocess.run(["git", "init", "-b", "main", str(container)], check=True, capture_output=True)
            nested = container / "nested"
            nested.mkdir()
            result = project_repo_add("nested", name="demo", paths=paths)
            self.assertEqual(result["status"], "invalid_repo_root", result)
            self.assertEqual(project_workspace.project_repository_registry(root, "demo")["mode"], "legacy")

class WorkspaceProbeCoverageTests(unittest.TestCase):
    def test_workspace_probe_tracks_security_named_reports_and_unknown_textual_code(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            report = pp.project_dir / "reports" / ".env.production"
            report.write_text("TOKEN=abcdef123456\n/auth/token depends on it\n", encoding="utf-8")
            proto = pp.project_dir / "repo" / "api" / "auth.proto"
            proto.parent.mkdir(parents=True, exist_ok=True)
            proto.write_text("service Auth { rpc Login(Request) returns (Reply); }\n", encoding="utf-8")
            first = project_workspace.workspace_index_probe(pp, include_artifacts=True, include_code=True)
            self.assertGreaterEqual(first["file_count"], 2)
            report.write_text("TOKEN=zyxwvutsrqpo\n/auth/token depends on it\n", encoding="utf-8")
            second = project_workspace.workspace_index_probe(pp, include_artifacts=True, include_code=True)
            self.assertNotEqual(first["hash"], second["hash"])
            proto.write_text("service Auth { rpc Logout(Request) returns (Reply); }\n", encoding="utf-8")
            third = project_workspace.workspace_index_probe(pp, include_artifacts=True, include_code=True)
            self.assertNotEqual(second["hash"], third["hash"])

class ContinuityFirstTests(unittest.TestCase):
    def make_paths(self, root: Path) -> HarnessPaths:
        (root / ".harness" / "memory").mkdir(parents=True, exist_ok=True)
        (root / ".harness" / "manifest.json").write_text(
            '{"harness_version":"test","active_project_id":"__auto__"}',
            encoding="utf-8",
        )
        (root / ".opencode" / "skills").mkdir(parents=True, exist_ok=True)
        return HarnessPaths(root=root, global_root=root / ".global")

    def test_project_without_pending_resumes_with_real_context(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("free-work", paths=paths)
            project_capture(
                "Mapped both authentication flows.",
                details="Service access uses client credentials; interactive access uses a session cookie.",
                kind="finding",
                sources=["reports/authentication.md"],
                uncertainty=["Refresh-token rotation is unverified."],
                likely_continuation="Inspect refresh behavior.",
                paths=paths,
            )
            resumed = project_resume("free-work", paths=paths)
            self.assertEqual(resumed["status"], "resumed")
            self.assertIn("Mapped both authentication flows", resumed["situation"])
            self.assertIn("Refresh-token rotation", resumed["handoff"])
            self.assertEqual(resumed["next_action"], "Inspect refresh behavior.")
            self.assertEqual(project_status("free-work", paths=paths)["pending"], [])

    def test_latest_user_direction_precedes_pending_and_inferred_continuation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            project_workspace.project_pending(root, "demo", "Old task", "Continue the old task")
            project_capture(
                "Earlier finding",
                name="demo",
                likely_continuation="Inspect the earlier finding",
                paths=paths,
            )
            project_capture(
                "Analyze the repository now.",
                name="demo",
                kind="direction",
                paths=paths,
            )
            resumed = project_resume("demo", paths=paths)
            self.assertEqual(resumed["next_action"], "Analyze the repository now.")
            self.assertEqual(resumed["possible_continuations"][0], "Analyze the repository now.")

    def test_resume_preserves_changes_since_last_consumed_handoff(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            project_resume("demo", paths=paths)  # consume project-creation handoff
            project_capture("New authentication evidence was established.", kind="finding", paths=paths)
            resumed = project_resume("demo", paths=paths)
            self.assertEqual(
                [row.get("summary") for row in resumed["changes_since_previous_handoff"]],
                ["New authentication evidence was established."],
            )
            immediate = project_resume("demo", paths=paths)
            self.assertEqual(immediate["changes_since_previous_handoff"], [])

    def test_concurrent_captures_preserve_journal_and_workspace_generation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            context = multiprocessing.get_context("spawn")
            workers = [
                context.Process(target=_concurrent_capture_worker, args=(str(root), f"worker-{worker}", 8))
                for worker in range(3)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(20)
                self.assertEqual(worker.exitcode, 0)
            pp = project_workspace.paths_for(root, "demo")
            records = project_workspace.read_jsonl(pp.continuity)
            concurrent = [row for row in records if str(row.get("summary", "")).startswith("worker-")]
            self.assertEqual(len(concurrent), 24)
            meta = json.loads(pp.project_json.read_text(encoding="utf-8"))
            self.assertEqual(meta["continuity"]["workspace_generation"], 25)

    def test_continuity_capture_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            first = project_capture("Stable observation", kind="observation", paths=paths)
            second = project_capture("Stable observation", kind="observation", paths=paths)
            self.assertEqual(first["status"], "captured")
            self.assertEqual(second["status"], "duplicate")
            pp = project_workspace.paths_for(root, "demo")
            rows = project_workspace.read_jsonl(pp.continuity)
            matching = [r for r in rows if r.get("summary") == "Stable observation"]
            self.assertEqual(len(matching), 1)

    def test_generated_views_are_reproducible(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            project_capture("A reproducible fact", kind="fact", confidence="high", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            project_workspace.refresh_project_files(root, "demo")
            situation_one = pp.situation.read_text(encoding="utf-8")
            handoff_one = pp.handoff.read_text(encoding="utf-8")
            project_workspace.refresh_project_files(root, "demo")
            self.assertEqual(situation_one, pp.situation.read_text(encoding="utf-8"))
            self.assertEqual(handoff_one, pp.handoff.read_text(encoding="utf-8"))

    def test_generic_project_task_checkpoint_does_not_use_burp_namespace(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            checkpoint = project_task_checkpoint(
                "Repository state report", name="demo", current_step="verify git state",
                next_action="write report", related_refs=["reports/T00.md"], paths=paths,
            )
            self.assertEqual(checkpoint["status"], "checkpointed")
            status = project_task_status(checkpoint["task_id"], name="demo", paths=paths)
            self.assertEqual(status["task_status"], "running")
            self.assertEqual(status["related_refs"], ["reports/T00.md"])
            finalized = project_task_finalize(
                task_id=checkpoint["task_id"], name="demo", outcome="passed",
                finding="Repository state verified.", paths=paths,
            )
            self.assertEqual(finalized["status"], "finalized")
            pp = project_workspace.paths_for(root, "demo")
            text = pp.continuity.read_text(encoding="utf-8")
            self.assertIn('"adapter": "generic_task"', text)
            self.assertNotIn('"adapter": "burp"', text)

    def test_sensitive_capture_is_stored_redacted_and_remains_indexable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            saved = project_capture("Authorization: Bearer abc123", kind="finding", paths=paths)
            self.assertIn("<REDACTED>", saved["summary"])
            self.assertEqual(saved["index_policy"], "safe")
            preview = project_index_preview("demo", include_artifacts=True, paths=paths)
            excluded_ids = {row.get("record_id") for row in preview["excluded"]}
            self.assertNotIn(saved["id"], excluded_ids)
            searched = project_search("Authorization", name="demo", paths=paths)
            self.assertTrue(searched["project_hits"])

    def test_security_code_snippet_in_continuity_is_not_censored_or_hidden(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            summary = "We proved token := helper.BearerTokenFromRequest(r) continues to JWT handling."
            saved = project_capture(summary, kind="finding", paths=paths)
            self.assertEqual(saved["summary"], summary)
            self.assertEqual(saved["index_policy"], "safe")
            searched = project_search("BearerTokenFromRequest JWT", name="demo", paths=paths)
            self.assertTrue(searched["project_hits"])

    def test_analysis_secret_value_is_redacted_but_record_remains_retrievable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            saved = project_capture(
                "Observed api_key=abcdef123456 on the test integration path.",
                kind="finding", paths=paths,
            )
            self.assertNotIn("abcdef123456", saved["summary"])
            self.assertIn("<REDACTED_SECRET>", saved["summary"])
            self.assertEqual(saved["index_policy"], "safe")
            searched = project_search("test integration path", name="demo", paths=paths)
            self.assertTrue(searched["project_hits"])

    def test_explicit_sensitive_capture_is_preserved_but_hidden_from_views_and_default_search(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            value = "password=correct-horse-battery-staple"
            saved = project_capture(value, kind="fact", allow_sensitive_plaintext=True, paths=paths)
            self.assertEqual(saved["summary"], value)
            self.assertEqual(saved["sensitivity"], "secret")
            self.assertEqual(saved["index_policy"], "no_rag")
            resumed = project_resume("demo", paths=paths)
            self.assertNotIn(value, resumed["situation"])
            self.assertNotIn(value, resumed["handoff"])
            records = project_records(paths)
            self.assertEqual(search_records("correct-horse", records, limit=5)[0]["summary"], value)
            searched = project_search("correct-horse", name="demo", paths=paths)
            self.assertEqual(searched["project_hits"], [])

    def test_global_fact_redacts_by_default_and_explicit_plaintext_remains_no_rag(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            safe = save_global_fact("token=secret-value", reviewed=True, paths=paths)
            self.assertEqual(safe["index_policy"], "safe")
            self.assertTrue(safe["redaction_applied"])
            self.assertIn("<REDACTED_SECRET>", safe["text"])
            saved = save_global_fact("token=second-secret-value", allow_sensitive_plaintext=True, paths=paths)
            self.assertEqual(saved["index_policy"], "no_rag")
            self.assertEqual(search_global_memory("second-secret-value", paths=paths), [])
            explicit = search_global_memory("second-secret-value", paths=paths, include_sensitive=True)
            self.assertEqual(explicit[0]["text"], "token=second-secret-value")

    def test_capture_reconciliation_marks_related_memory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            first = project_capture("JWT validation is local in middleware.", kind="finding", paths=paths)
            second = project_capture("Local JWT validation happens in the authentication middleware.", kind="finding", paths=paths)
            self.assertIn(second["reconciliation"]["classification"], {"refinement_or_reinforcement", "related", "duplicate_or_restatement"})
            self.assertEqual(second["reconciliation"]["matches"][0]["id"], first["id"])

    def test_capture_reconciliation_exact_restatement_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            first = project_capture("JWT validation is local in middleware.", kind="finding", paths=paths)
            second = project_capture("JWT validation is local in middleware.", kind="finding", paths=paths)
            self.assertEqual(second["status"], "duplicate")
            self.assertEqual(second["id"], first["id"])
            pp = project_workspace.paths_for(root, "demo")
            summaries = [row.get("summary") for row in project_workspace.continuity_records(pp)]
            self.assertEqual(summaries.count("JWT validation is local in middleware."), 1)

    def test_capture_reconciliation_new_source_is_reinforcement(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            first = project_capture(
                "JWT validation is local in middleware.",
                kind="finding",
                sources=[{"type": "file", "path": "repo/auth.py", "line": 20}],
                paths=paths,
            )
            second = project_capture(
                "JWT validation is local in middleware.",
                kind="finding",
                sources=[{"type": "file", "path": "reports/auth.md"}],
                paths=paths,
            )
            self.assertEqual(second["status"], "captured")
            self.assertEqual(second["reconciliation"]["classification"], "reinforcement")
            self.assertEqual(second["metadata"]["reinforces"], first["id"])

    def test_capture_reconciliation_possible_contradiction_requires_review(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            first = project_capture("JWT validation is local in middleware.", kind="finding", paths=paths)
            second = project_capture("JWT validation is not local in middleware.", kind="finding", paths=paths)
            self.assertEqual(second["status"], "needs_review")
            self.assertEqual(second["reconciliation"]["classification"], "possible_contradiction")
            correction = project_capture(
                "JWT validation is not local in middleware.",
                kind="correction",
                supersedes=[first["id"]],
                paths=paths,
            )
            self.assertEqual(correction["status"], "captured")
            self.assertEqual(correction["supersedes"], [first["id"]])

    def test_continuity_source_paths_reject_absolute_and_traversal_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            saved = project_capture(
                "Source path normalization check.",
                name="demo",
                sources=[
                    {"type": "file", "path": "../outside.txt"},
                    {"type": "file", "path": "/etc/passwd"},
                    {"type": "file", "path": "reports/safe.md"},
                ],
                paths=paths,
            )
            self.assertEqual(saved["sources"], [{"type": "file", "path": "reports/safe.md"}])

    def test_continuity_source_refs_require_opaque_allowlisted_references(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            saved = project_capture(
                "Reference normalization check.",
                name="demo",
                sources=[
                    {"type": "secret", "ref": "username:password"},
                    {"type": "secret", "ref": "secret://external-item-123"},
                    {"type": "burp", "ref": "burp-run://run_123"},
                ],
                paths=paths,
            )
            self.assertEqual(
                saved["sources"],
                [
                    {"type": "secret", "ref": "secret://external-item-123"},
                    {"type": "burp", "ref": "burp-run://run_123"},
                ],
            )

    def test_nested_adapter_metadata_secret_is_redacted_without_hiding_record(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            saved = project_workspace.project_capture(
                root,
                "demo",
                "Adapter produced a summary.",
                kind="artifact",
                metadata={"adapter": "test", "nested": {"access_token": "top-secret-value"}},
            )
            preview = project_index_preview("demo", include_artifacts=True, paths=paths)
            excluded = {row.get("record_id"): row.get("reason") for row in preview["excluded"]}
            self.assertNotIn(saved["id"], excluded)
            self.assertNotIn("top-secret-value", json.dumps(preview))
            self.assertNotIn("top-secret-value", pp.continuity.read_text(encoding="utf-8"))
            self.assertNotIn("top-secret-value", pp.situation.read_text(encoding="utf-8"))
            self.assertNotIn("top-secret-value", pp.handoff.read_text(encoding="utf-8"))

    def test_security_named_report_directories_are_not_blanket_excluded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            report = pp.project_dir / "reports" / "credentials" / "oauth.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("OAuth credential flow reaches /auth/token endpoint.\n", encoding="utf-8")
            preview = project_index_preview("demo", include_artifacts=True, paths=paths)
            included = {str(row.get("path")) for row in preview["included"]}
            self.assertTrue(any(path.endswith("reports/credentials/oauth.md") for path in included))

    def test_security_report_is_indexed_with_value_level_redaction(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            report = pp.project_dir / "reports" / "auth-analysis.md"
            report.write_text(
                "# Auth analysis\n\n"
                "The handler executes `token := helper.BearerTokenFromRequest(r)`.\n"
                "Observed api_key=abcdef123456 in the test fixture.\n",
                encoding="utf-8",
            )
            preview = project_index_preview("demo", include_artifacts=True, paths=paths)
            included_paths = {str(row.get("path")) for row in preview["included"]}
            self.assertTrue(any(path.endswith("reports/auth-analysis.md") for path in included_paths))
            indexed = project_refresh("demo", include_artifacts=True, include_qdrant=False, paths=paths)
            self.assertIn(indexed["status"], {"refreshed", "indexed", "ok"})
            hits = project_search("BearerTokenFromRequest", name="demo", paths=paths)
            rendered = json.dumps(hits, sort_keys=True)
            self.assertIn("BearerTokenFromRequest", rendered)
            self.assertNotIn("abcdef123456", rendered)

    def test_index_preview_is_fail_closed_for_raw_secrets_and_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            (pp.project_dir / "reports" / "safe-report.md").write_text("Verified safe report content", encoding="utf-8")
            (pp.artifacts_dir / "evidence" / "random.txt").write_text("ordinary artifact", encoding="utf-8")
            (pp.artifacts_dir / "evidence" / "auth-summary.md").write_text("Safe summarized evidence", encoding="utf-8")
            (pp.artifacts_dir / "raw").mkdir(parents=True, exist_ok=True)
            (pp.artifacts_dir / "raw" / "dump.txt").write_text("raw traffic", encoding="utf-8")
            (pp.project_dir / ".env").write_text("API_KEY=secret", encoding="utf-8")
            preview = project_index_preview("demo", include_artifacts=True, paths=paths)
            included_paths = {row.get("path") for row in preview["included"]}
            excluded = {row.get("path"): row.get("reason") for row in preview["excluded"]}
            self.assertIn("workspace/projects/demo/reports/safe-report.md", included_paths)
            self.assertIn("workspace/projects/demo/artifacts/evidence/auth-summary.md", included_paths)
            self.assertEqual(excluded["workspace/projects/demo/artifacts/evidence/random.txt"], "artifact_not_registered_or_safe_summary")
            self.assertTrue(excluded["workspace/projects/demo/artifacts/raw/dump.txt"].startswith("excluded_path_component"))

    def test_analysis_env_named_report_is_sanitized_not_censored_and_registry_blocks_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            env_file = pp.project_dir / "reports" / ".env.production"
            env_file.write_text("TOKEN=abcdef123456\nThe /auth/token handler depends on this setting.\n", encoding="utf-8")
            preview = project_index_preview("demo", include_artifacts=True, paths=paths)
            included = {row.get("path") for row in preview["included"]}
            self.assertIn("workspace/projects/demo/reports/.env.production", included)
            project_refresh("demo", include_artifacts=True, include_qdrant=False, paths=paths)
            hits = project_search("auth token handler", name="demo", paths=paths)
            rendered = json.dumps(hits)
            self.assertIn("/auth/token", rendered)
            self.assertNotIn("abcdef123456", rendered)
            with self.assertRaises(ValueError):
                indexing_policy.register_safe_artifact(pp.index_dir, "../../artifacts/evidence/summary.md")

    def test_analysis_report_under_build_directory_is_not_silently_hidden(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            report = pp.project_dir / "reports" / "build" / "auth-analysis.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("The OATHKEEPER_BUILD_REPORT_NEEDLE documents auth behavior.\n", encoding="utf-8")
            project_refresh("demo", include_artifacts=True, include_qdrant=False, paths=paths)
            hits = project_search("OATHKEEPER_BUILD_REPORT_NEEDLE", name="demo", paths=paths)
            self.assertIn("OATHKEEPER_BUILD_REPORT_NEEDLE", json.dumps(hits))

    def test_project_reads_never_fall_back_to_legacy_root_memory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            legacy = paths.memory_dir / "project.jsonl"
            legacy.write_text(json.dumps({"kind": "fact", "text": "legacy root value"}) + "\n", encoding="utf-8")
            self.assertEqual(project_records(paths, session_id="unattached-session"), [])

    def test_project_write_requires_explicit_attachment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_workspace.ensure_project_layout(root, "demo")
            old_session = project_workspace.SESSION_ID
            project_workspace.SESSION_ID = "unattached-test-session"
            try:
                result = save_project_fact("Must not fall back", paths=paths)
            finally:
                project_workspace.SESSION_ID = old_session
            self.assertEqual(result["status"], "rejected")
            legacy = root / ".harness" / "memory" / "project.jsonl"
            self.assertFalse(legacy.exists() and legacy.read_text(encoding="utf-8").strip())

    def test_sessions_do_not_overwrite_each_other(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_paths(root)
            original = project_workspace.SESSION_ID
            try:
                project_workspace.SESSION_ID = "session-a"
                project_workspace.project_create(root, "alpha")
                path_a = project_workspace.session_state_path(root)
                project_workspace.SESSION_ID = "session-b"
                self.assertIsNone(project_workspace.current_project_id(root))
                project_workspace.project_create(root, "beta")
                path_b = project_workspace.session_state_path(root)
                self.assertNotEqual(path_a, path_b)
                self.assertEqual(json.loads(path_a.read_text())["project_id"], "alpha")
                self.assertEqual(json.loads(path_b.read_text())["project_id"], "beta")
            finally:
                project_workspace.SESSION_ID = original

    def test_canonical_capture_is_immediately_available_in_exact_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            captured = project_capture(
                "Immediate exact index sentinel for token rotation.",
                name="demo",
                kind="finding",
                paths=paths,
            )
            self.assertIn(captured.get("exact_index_sync", {}).get("status"), {"incrementally_indexed", "indexed"})
            hits = rag_backend.search_fts(
                project_fts_db(paths, project_id="demo"),
                "token rotation sentinel",
                scope="project",
                limit=5,
            )
            self.assertTrue(hits)
            self.assertIn("Immediate exact index sentinel", hits[0]["preview"])

    def test_canonical_capture_survives_exact_index_sync_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")

            original_hook = project_workspace._CAPTURE_HOOK
            try:
                def failing_hook(*_args, **_kwargs):
                    raise RuntimeError("fts unavailable")
                project_workspace.register_capture_hook(failing_hook)
                saved = project_capture("Persist this even if SQLite sync fails.", paths=paths)
            finally:
                project_workspace.register_capture_hook(original_hook)

            rows = project_workspace.read_jsonl(pp.continuity)
            self.assertTrue(any(row.get("id") == saved.get("id") for row in rows))
            self.assertEqual(saved.get("_write_status"), "appended")
            self.assertEqual(saved.get("exact_index_sync", {}).get("status"), "warning")
            self.assertIn("capture_hook_failed", saved.get("exact_index_sync", {}).get("reason", ""))

    def test_index_preview_and_apply_use_identical_document_selection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            report = pp.project_dir / "reports" / "selection-summary.md"
            report.write_text("Stable preview/apply selection marker.", encoding="utf-8")
            preview = project_index_preview("demo", include_artifacts=True, paths=paths)
            applied = project_refresh("demo", include_artifacts=True, include_qdrant=False, paths=paths)

            manifest = indexing_policy.read_index_manifest(Path(applied["index"]["manifest"]))

            def selected(rows):
                return {
                    (str(row.get("path") or row.get("record_id") or ""), str(row.get("kind") or ""))
                    for row in rows
                    if isinstance(row, dict)
                }

            self.assertEqual(selected(preview["included"]), selected(manifest["included"]))
            self.assertEqual(
                {(str(row.get("path") or row.get("record_id") or ""), str(row.get("reason") or "")) for row in preview["excluded"]},
                {(str(row.get("path") or row.get("record_id") or ""), str(row.get("reason") or "")) for row in manifest["excluded"]},
            )

    def test_refresh_builds_manifest_and_reports_fresh_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            project_capture("Index this safe fact", kind="fact", confidence="high", paths=paths)
            result = project_refresh("demo", include_qdrant=False, paths=paths)
            self.assertEqual(result["index"]["status"], "indexed")
            status = project_status("demo", paths=paths)
            self.assertTrue(status["index_freshness"]["fresh"])
            manifest = project_workspace.paths_for(root, "demo").index_manifest
            self.assertTrue(manifest.exists())
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 2)
            self.assertTrue(payload["workspace_probe_hash"])
            self.assertTrue(all(row.get("indexed_at") for row in payload["included"]))

    def test_vector_index_runs_once_against_final_generated_views(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            project_capture("Final view indexing check", kind="finding", paths=paths)
            with mock.patch("harness_core.rag_backend.index_qdrant", return_value={"status": "indexed", "backend": "qdrant"}) as qdrant:
                result = project_refresh("demo", include_qdrant=True, paths=paths)
            self.assertEqual(result["index"]["status"], "indexed")
            qdrant.assert_called_once()
            indexed_docs = qdrant.call_args.args[0]
            source_paths = {str(doc.source_path or "") for doc in indexed_docs}
            self.assertTrue(any(path.endswith("SITUATION.md") for path in source_paths))
            self.assertTrue(any(path.endswith("HANDOFF.md") for path in source_paths))

    def test_project_search_uses_vectors_only_for_matching_document_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            project_capture("Searchable token behavior", kind="finding", paths=paths)
            with mock.patch("harness_core.rag_backend.index_qdrant", return_value={"status": "indexed", "backend": "qdrant"}):
                project_refresh("demo", include_qdrant=True, paths=paths)
            with mock.patch("harness_core.rag_backend.search_qdrant", return_value=[]) as vector_search:
                current = project_search("token behavior", name="demo", paths=paths)
            self.assertTrue(current["retrieval"]["qdrant"]["project_current"])
            vector_search.assert_called_once()

            pp = project_workspace.paths_for(root, "demo")
            report = pp.project_dir / "reports" / "new-report.md"
            report.write_text("New exact-search material", encoding="utf-8")
            with mock.patch("harness_core.rag_backend.search_qdrant", return_value=[]) as stale_vector_search:
                stale = project_search("exact-search", name="demo", paths=paths)
            self.assertFalse(stale["retrieval"]["qdrant"]["project_current"])
            stale_vector_search.assert_not_called()
            self.assertEqual(stale["index_refresh"]["status"], "indexed")

    def test_codebase_search_enables_and_searches_project_repo(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            source = pp.project_dir / "repo" / "src" / "auth.py"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("def validate_jwt_signature_locally(): pass\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"AWOKI_DISABLE_QDRANT": "1"}, clear=False):
                result = codebase_search(
                    "validate jwt signature",
                    name="demo",
                    paths=paths,
                )
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["hits"])
            self.assertTrue(any(hit.get("kind") == "code" for hit in result["hits"]))
            meta = json.loads(pp.project_json.read_text(encoding="utf-8"))
            self.assertTrue(meta["rag"]["index_code"])

    def test_memory_reconciliation_filters_qdrant_to_memory_records(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            manifest = {
                "document_set_hash": "same",
                "backends": {"qdrant": {"status": "indexed", "document_set_hash": "same"}},
            }
            pp.index_manifest.parent.mkdir(parents=True, exist_ok=True)
            pp.index_manifest.write_text(json.dumps(manifest), encoding="utf-8")
            with mock.patch("harness_core.rag_backend.search_qdrant", return_value=[]) as search:
                project_capture("JWT validation is local", name="demo", paths=paths)
            self.assertTrue(search.call_args.kwargs["memory_only"])

    def test_stale_session_recovery_is_previewable_and_non_destructive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            session_id = "stale-session"
            project_create("demo", session_id=session_id, paths=paths)
            opencode_events.record_activity(root, session_id, event_type="file.edited", path=str(root / "src" / "auth.py"))
            state_path = project_workspace.session_state_path(root, session_id)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["last_activity_at"] = "2000-01-01T00:00:00Z"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            preview = project_workspace.recover_stale_sessions(root, stale_after_hours=1, apply=False)
            self.assertEqual(preview["stale_count"], 1)
            self.assertEqual(project_workspace.current_project_id(root, session_id=session_id), "demo")

            applied = project_workspace.recover_stale_sessions(root, stale_after_hours=1, apply=True)
            self.assertEqual(applied["recovered_count"], 1)
            self.assertIsNone(project_workspace.current_project_id(root, session_id=session_id))
            pp = project_workspace.paths_for(root, "demo")
            self.assertTrue(any(
                row.get("kind") == "continuity_reflection"
                and str((row.get("metadata") or {}).get("checkpoint_reason", "")).startswith("stale_session_recovery")
                for row in project_workspace.continuity_records(pp)
            ))

    def test_orphaned_stale_session_is_marked_recovered_not_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_paths(root)
            session_id = "orphan-session"
            state_path = project_workspace.session_state_path(root, session_id)
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({
                "session_id": session_id,
                "project_id": "missing-project",
                "status": "active",
                "opened_at": "2000-01-01T00:00:00Z",
                "last_activity_at": "2000-01-01T00:00:00Z",
                "activity": {"dirty": True},
            }), encoding="utf-8")
            applied = project_workspace.recover_stale_sessions(root, stale_after_hours=1, apply=True)
            self.assertEqual(applied["recovered_count"], 1)
            recovered = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(recovered["status"], "recovered")
            self.assertTrue(state_path.exists())

    def test_continuity_doctor_is_read_only_and_reports_parse_errors(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            healthy = awoki.continuity_doctor(root)
            self.assertEqual(healthy["status"], "ok")
            pp = project_workspace.paths_for(root, "demo")
            with pp.continuity.open("a", encoding="utf-8") as handle:
                handle.write("{not-json}\n")
            broken = awoki.continuity_doctor(root)
            self.assertEqual(broken["status"], "issues")
            self.assertTrue(any(issue.get("kind") == "continuity_parse_error" for issue in broken["issues"]))

    def test_continuity_doctor_reports_drift_and_unsafe_candidates_without_repairing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            pp.situation.write_text("manually edited snapshot\n", encoding="utf-8")
            unsafe = pp.artifacts_dir / "raw" / "capture.txt"
            unsafe.parent.mkdir(parents=True, exist_ok=True)
            unsafe.write_text("Authorization: Bearer doctor-must-not-read-this", encoding="utf-8")
            global_before = (root / ".global").exists()
            before = {
                "situation": pp.situation.read_bytes(),
                "handoff": pp.handoff.read_bytes(),
                "project": pp.project_json.read_bytes(),
                "unsafe": unsafe.read_bytes(),
            }

            result = awoki.continuity_doctor(root)

            self.assertTrue(result["read_only"])
            self.assertEqual(result["status"], "issues")
            self.assertTrue(any(issue.get("kind") == "generated_view_drift" for issue in result["issues"]))
            self.assertTrue(any(warning.get("kind") == "unsafe_index_candidates_excluded" for warning in result["warnings"]))
            after = {
                "situation": pp.situation.read_bytes(),
                "handoff": pp.handoff.read_bytes(),
                "project": pp.project_json.read_bytes(),
                "unsafe": unsafe.read_bytes(),
            }
            self.assertEqual(before, after)
            self.assertEqual(global_before, (root / ".global").exists())

    def test_external_workspace_changes_mark_index_stale_and_deleted_sources_are_removed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            report = pp.project_dir / "reports" / "auth-report.md"
            report.write_text("Initial authentication report", encoding="utf-8")
            project_refresh("demo", include_qdrant=False, paths=paths)
            self.assertTrue(project_status("demo", paths=paths)["index_freshness"]["fresh"])
            report.write_text("Changed authentication report", encoding="utf-8")
            self.assertFalse(project_status("demo", paths=paths)["index_freshness"]["fresh"])
            updated = project_refresh("demo", include_qdrant=False, paths=paths)
            self.assertTrue(updated["index"]["change_set"]["changed"])
            report.unlink()
            self.assertFalse(project_status("demo", paths=paths)["index_freshness"]["fresh"])
            deleted = project_refresh("demo", include_qdrant=False, paths=paths)
            self.assertTrue(deleted["index"]["change_set"]["deleted"])

    def test_pause_captures_even_small_dirty_observable_activity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            session_id = "pause-dirty-session"
            project_create("demo", session_id=session_id, paths=paths)
            opencode_events.record_activity(root, session_id, event_type="file.edited", path=str(root / "src" / "last.py"))
            paused = project_pause(session_id=session_id, paths=paths)
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(paused["observable_checkpoint"]["status"], "checkpointed")
            self.assertIsNone(project_workspace.current_project_id(root, session_id=session_id))
            pp = project_workspace.paths_for(root, "demo")
            reflections = [row for row in project_workspace.continuity_records(pp) if row.get("kind") == "continuity_reflection"]
            self.assertTrue(any((row.get("metadata") or {}).get("checkpoint_reason") == "project.pause" for row in reflections))

    def test_pause_can_store_operational_reflection_and_detach(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            paused = project_pause(
                summary="Authentication review now covers service and interactive flows.",
                uncertainty=["Refresh behavior remains unverified."],
                likely_continuation="Inspect refresh behavior.",
                paths=paths,
            )
            self.assertEqual(paused["status"], "paused")
            self.assertIsNone(project_workspace.current_project_id(root))
            reopened = project_open("demo", paths=paths)
            self.assertNotIn("handoff", reopened)
            dense = project_resume("demo", paths=paths)
            self.assertIn("Authentication review", dense["handoff"])


    def test_project_open_is_slim_but_preserves_active_work_and_prior_pointers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            sid = "slim-open-session"
            project_create("demo", session_id=sid, paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            reports = pp.project_dir / "reports"
            reports.mkdir(parents=True, exist_ok=True)
            (reports / "auth-review.md").write_text("# Auth review\n", encoding="utf-8")
            project_capture("Useful prior authentication finding", kind="finding", paths=paths)
            work_ledger.sync_todos(root, sid, [{"id": "x", "content": "Verify handler reachability", "status": "in_progress", "priority": "high"}])

            opened = project_open("demo", session_id=sid, paths=paths)
            self.assertEqual(opened["status"], "resumed")
            self.assertTrue(opened["projection_policy"]["normal_open_is_slim"])
            for duplicated in ("situation", "handoff", "recent_reflections", "important_knowledge", "sources"):
                self.assertNotIn(duplicated, opened)
            self.assertEqual(opened["active_work"]["todos"][0]["content"], "Verify handler reachability")
            self.assertTrue(any(row.get("path") == "reports/auth-review.md" for row in opened["prior_material"]))
            self.assertIn("project_resume", opened["detail_access"])

            dense = project_resume("demo", session_id=sid, paths=paths)
            self.assertIn("situation", dense)
            self.assertIn("handoff", dense)

    def test_code_exact_search_is_structured_scoped_and_supports_rg_strength_modes(self):
        if not shutil.which("rg"):
            self.skipTest("rg unavailable")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            sid = "exact-search-session"
            project_create("demo", session_id=sid, paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            repo = pp.project_dir / "repo"
            (repo / "src").mkdir(parents=True, exist_ok=True)
            (repo / "tests").mkdir(parents=True, exist_ok=True)
            (repo / "src" / "auth.go").write_text("func Authenticate() {}\nvar ErrUnauthorized = true\n", encoding="utf-8")
            (repo / "src" / "other.go").write_text("func AuthenticateBearer() {}\n", encoding="utf-8")
            (repo / "tests" / "auth_test.go").write_text("func TestAuthenticate() {}\n", encoding="utf-8")

            matches = code_exact_search(
                ["Authenticate", "ErrUnauthorized"],
                name="demo", session_id=sid, mode="matches",
                paths_filter=["src"], include_globs=["*.go"], exclude_globs=["*_test.go"],
                context_after=1, limit=10, paths=paths,
            )
            self.assertEqual(matches["status"], "ok", matches)
            self.assertEqual(matches["engine"], "ripgrep")
            self.assertTrue(matches["structured"])
            self.assertFalse(matches["shell"])
            self.assertEqual(matches["credential_environment"], "stripped")
            match_rows = [row for row in matches["rows"] if row.get("type") == "match"]
            self.assertTrue(any(row["path"] == "src/auth.go" for row in match_rows))
            self.assertTrue(all(not row["path"].startswith("tests/") for row in match_rows))

            files = code_exact_search(["Authenticate"], name="demo", session_id=sid, mode="files", limit=1, paths=paths)
            self.assertEqual(files["returned"], 1)
            self.assertTrue(files["has_more"])
            self.assertEqual(files["continuation"]["next_offset"], 1)
            page2 = code_exact_search(["Authenticate"], name="demo", session_id=sid, mode="files", offset=1, limit=10, paths=paths)
            self.assertGreaterEqual(page2["returned"], 1)

            counts = code_exact_search(["Authenticate"], name="demo", session_id=sid, mode="count", include_globs=["*.go"], paths=paths)
            self.assertTrue(any(int(row.get("matches") or 0) >= 1 for row in counts["rows"]))

            rejected = code_exact_search(["Authenticate"], name="demo", session_id=sid, paths_filter=["../escape"], paths=paths)
            self.assertEqual(rejected["status"], "rejected")

    def test_opencode_events_capture_only_meaningful_observable_activity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            session_id = "opencode-session-1"
            project_create("demo", session_id=session_id, paths=paths)
            opencode_events.record_activity(root, session_id, event_type="file.edited", path=str(root / "src" / "auth.py"))
            first = opencode_events.checkpoint_session(root, session_id, reason="session.idle")
            self.assertEqual(first["status"], "refreshed_without_capture")
            opencode_events.record_activity(root, session_id, event_type="file.edited", path=str(root / "src" / "tokens.py"))
            opencode_events.record_activity(root, session_id, event_type="tool.execute.after", tool="read")
            second = opencode_events.checkpoint_session(root, session_id, reason="session.idle")
            self.assertEqual(second["status"], "checkpointed")
            reflection = second["reflection"]
            self.assertEqual(reflection["kind"], "continuity_reflection")
            self.assertEqual(reflection["metadata"]["capture_channel"], "opencode_observable_events")
            self.assertNotIn("arguments", json.dumps(reflection).lower())

    def test_direct_dirty_project_switch_checkpoints_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            session_id = "direct-switch-session"
            project_create("alpha", session_id=session_id, paths=paths)
            opencode_events.record_activity(root, session_id, event_type="file.edited", path=str(root / "src" / "alpha.py"))
            switched = project_open("beta", create_if_missing=True, session_id=session_id, paths=paths)
            self.assertEqual(switched["status"], "created")
            self.assertEqual(project_workspace.current_project_id(root, session_id=session_id), "beta")
            alpha = project_workspace.paths_for(root, "alpha")
            reflections = [row for row in project_workspace.continuity_records(alpha) if row.get("kind") == "continuity_reflection"]
            self.assertTrue(reflections)
            self.assertEqual(reflections[-1]["metadata"]["capture_channel"], "atomic_project_switch")

    def test_project_switch_checkpoints_previous_session_activity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            session_id = "switch-session"
            project_create("alpha", session_id=session_id, paths=paths)
            opencode_events.record_activity(root, session_id, event_type="file.edited", path=str(root / "src" / "a.py"))
            opencode_events.record_activity(root, session_id, event_type="file.edited", path=str(root / "src" / "b.py"))
            switched = opencode_events.prepare_project_switch(root, session_id, "beta")
            self.assertTrue(switched["switched"])
            self.assertEqual(switched["checkpoint"]["status"], "checkpointed")
            self.assertIsNone(project_workspace.current_project_id(root, session_id=session_id))
            project_create("beta", session_id=session_id, paths=paths)
            self.assertEqual(project_workspace.current_project_id(root, session_id=session_id), "beta")
            alpha = project_workspace.paths_for(root, "alpha")
            reflections = [row for row in project_workspace.continuity_records(alpha) if row.get("kind") == "continuity_reflection"]
            self.assertTrue(reflections)

    def test_checkpoint_preserves_activity_arriving_during_capture(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            session_id = "checkpoint-race-session"
            project_create("demo", session_id=session_id, paths=paths)
            opencode_events.record_activity(root, session_id, event_type="file.edited", path=str(root / "src" / "before.py"))
            original_capture = project_workspace.project_capture

            def capture_with_concurrent_activity(*args, **kwargs):
                result = original_capture(*args, **kwargs)
                opencode_events.record_activity(
                    root,
                    session_id,
                    event_type="file.edited",
                    path=str(root / "src" / "during.py"),
                )
                return result

            with mock.patch.object(project_workspace, "project_capture", side_effect=capture_with_concurrent_activity):
                result = opencode_events.checkpoint_session(
                    root,
                    session_id,
                    reason="session.pause",
                    detach=True,
                    force=True,
                )

            self.assertEqual(result["status"], "checkpoint_conflict")
            self.assertEqual(project_workspace.current_project_id(root, session_id=session_id), "demo")
            state = json.loads(project_workspace.session_state_path(root, session_id).read_text(encoding="utf-8"))
            self.assertTrue(state["activity"]["dirty"])
            self.assertIn("src/during.py", state["activity"]["changed_files"])

    def test_checkpoint_refuses_stale_expected_session_state(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            session_id = "checkpoint-preview-session"
            project_create("demo", session_id=session_id, paths=paths)
            state_path = project_workspace.session_state_path(root, session_id)
            expected = json.loads(state_path.read_text(encoding="utf-8"))["last_activity_at"]
            opencode_events.record_activity(root, session_id, event_type="file.edited", path=str(root / "src" / "new.py"))
            result = opencode_events.checkpoint_session(
                root,
                session_id,
                reason="stale_session_recovery:24h",
                detach=True,
                force=True,
                expected_last_activity_at=expected,
            )
            self.assertEqual(result["status"], "state_changed")
            self.assertEqual(project_workspace.current_project_id(root, session_id=session_id), "demo")

    def test_opencode_compaction_context_is_bounded_generated_continuity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            session_id = "opencode-session-2"
            project_create("demo", session_id=session_id, paths=paths)
            project_capture("Established token validation behavior.", name="demo", kind="finding", paths=paths)
            result = opencode_events.compaction_context(root, session_id, max_chars=4_000)
            self.assertEqual(result["status"], "ok")
            self.assertLessEqual(len(result["context"]), 4_000)
            self.assertIn("generated operational continuity", result["context"])
            self.assertIn("Established token validation behavior", result["context"])
            self.assertIn("Awoki reliability invariants", result["context"])
            self.assertIn("Never claim a check ran unless its result was observed", result["context"])

    def test_opencode_events_ignore_generated_views(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.make_paths(root)
            result = opencode_events.record_activity(
                root,
                "session-ignore",
                event_type="file.edited",
                path=str(root / "workspace" / "projects" / "demo" / "SITUATION.md"),
            )
            self.assertEqual(result["status"], "ignored")

    def test_legacy_migration_redacts_secrets_at_canonical_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            legacy = pp.memory_dir / "findings.jsonl"
            legacy.write_text(json.dumps({"id": "secret-finding", "kind": "finding", "summary": "Observed password=never-store-this"}) + "\n", encoding="utf-8")
            applied = continuity_migration.migrate(root, "demo", apply=True)
            self.assertEqual(applied["appended_count"], 1)
            text = pp.continuity.read_text(encoding="utf-8")
            self.assertNotIn("never-store-this", text)
            row = next(item for item in project_workspace.read_jsonl(pp.continuity) if item.get("id") == "secret-finding")
            self.assertEqual(row["index_policy"], "safe")
            self.assertTrue(row.get("redacted"))

    def test_legacy_migration_is_previewable_idempotent_and_non_destructive(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            legacy = pp.memory_dir / "decisions.jsonl"
            legacy.write_text(json.dumps({"id": "decision-1", "kind": "decision", "text": "Use the continuity journal."}) + "\n", encoding="utf-8")
            preview = continuity_migration.migrate(root, "demo", apply=False)
            self.assertEqual(preview["candidate_count"], 1)
            self.assertTrue(legacy.exists())
            applied = continuity_migration.migrate(root, "demo", apply=True)
            self.assertEqual(applied["appended_count"], 1)
            again = continuity_migration.migrate(root, "demo", apply=True)
            self.assertEqual(again["appended_count"], 0)
            self.assertTrue(legacy.exists())
            rows = project_workspace.read_jsonl(pp.continuity)
            self.assertEqual(len([row for row in rows if row.get("id") == "decision-1"]), 1)


    def test_capture_kind_is_flexible_and_evidence_strictness_is_finding_specific(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)

            neutral = project_capture("Remember this ordinary project note.", paths=paths)
            self.assertEqual(neutral["kind"], "observation")
            self.assertEqual(neutral["confidence"], "medium")
            self.assertTrue(str(neutral["id"]).startswith("cont_"))

            saved = project_capture(
                "A custom research note.",
                kind="Research Note",
                confidence="high",
                sources=[{"type": "file", "path": "notes/thoughts.md", "password": "must-drop"}],
                paths=paths,
            )
            self.assertEqual(saved["kind"], "research_note")
            self.assertEqual(saved["original_kind"], "Research Note")
            self.assertNotIn("password", saved["sources"][0])

            fact = project_capture(
                "The staging issuer is separate.",
                kind="fact",
                confidence="high",
                paths=paths,
            )
            self.assertEqual(fact["confidence"], "high")
            self.assertNotEqual((fact.get("metadata") or {}).get("confidence_adjustment"), "downgraded_missing_source")

            finding = project_capture(
                "The request path reaches the payment sink.",
                kind="finding",
                confidence="high",
                paths=paths,
            )
            self.assertEqual(finding["confidence"], "medium")
            self.assertEqual(finding["metadata"]["confidence_adjustment"], "downgraded_missing_source")

    def test_correction_supersedes_active_record_without_deleting_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            old = project_capture("The issuer is tenant A.", kind="finding", paths=paths)
            correction = project_capture(
                "The issuer is tenant B.",
                kind="correction",
                supersedes=[old["id"]],
                sources=["reports/issuer-check.md"],
                confidence="high",
                paths=paths,
            )
            pp = project_workspace.paths_for(root, "demo")
            raw = project_workspace.read_jsonl(pp.continuity)
            self.assertTrue(any(row.get("id") == old["id"] for row in raw))
            active = project_workspace.continuity_records(pp)
            self.assertFalse(any(row.get("id") == old["id"] for row in active))
            self.assertTrue(any(row.get("id") == correction["id"] for row in active))
            resumed = project_resume("demo", paths=paths)
            self.assertNotIn("The issuer is tenant A", resumed["handoff"])
            self.assertIn("The issuer is tenant B", resumed["handoff"])

    def test_symlink_is_never_indexed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            outside = root / "outside-secret.md"
            outside.write_text("password=outside-secret", encoding="utf-8")
            link = pp.project_dir / "reports" / "linked-report.md"
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks are unavailable in this environment")
            preview = project_index_preview("demo", include_artifacts=True, paths=paths)
            row = next(item for item in preview["excluded"] if item.get("path", "").endswith("linked-report.md"))
            self.assertEqual(row["reason"], "symlink_not_allowed")
            self.assertNotIn("outside-secret", json.dumps(preview))

    def test_project_policy_caps_explicit_code_index_request(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            code = pp.project_dir / "repo" / "src" / "app.py"
            code.parent.mkdir(parents=True, exist_ok=True)
            code.write_text("def continuity_policy_marker(): return True\n", encoding="utf-8")
            blocked = project_index_preview("demo", include_code=True, paths=paths)
            self.assertFalse(any(item.get("path", "").endswith("src/app.py") for item in blocked["included"]))
            self.assertTrue(any(item.get("reason") == "project_policy:index_code=false" for item in blocked["excluded"]))
            meta = json.loads(pp.project_json.read_text(encoding="utf-8"))
            meta["rag"]["index_code"] = True
            pp.project_json.write_text(json.dumps(meta), encoding="utf-8")
            allowed = project_index_preview("demo", include_code=True, paths=paths)
            self.assertTrue(any(item.get("path", "").endswith("src/app.py") for item in allowed["included"]))

    def test_safe_capture_is_immediately_searchable_in_exact_index(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            saved = project_capture(
                "The refresh rotation marker is cobalt-albatross.",
                kind="finding",
                sources=["reports/refresh.md"],
                paths=paths,
            )
            self.assertIn(saved.get("exact_index_sync", {}).get("status"), {"incrementally_indexed", "indexed"})
            hits = rag_backend.search_fts(project_fts_db(paths, project_id="demo"), "cobalt albatross", scope="project", limit=5)
            self.assertTrue(hits)

    def test_stale_dirty_session_can_be_recovered_non_destructively(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            session_id = "stale-session"
            project_create("demo", session_id=session_id, paths=paths)
            opencode_events.record_activity(root, session_id, event_type="file.edited", path=str(root / "src" / "auth.py"))
            state_path = project_workspace.session_state_path(root, session_id)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["last_activity_at"] = "2020-01-01T00:00:00Z"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            preview = project_workspace.recover_stale_sessions(root, stale_after_hours=1, apply=False)
            self.assertEqual(preview["stale_count"], 1)
            applied = project_workspace.recover_stale_sessions(root, stale_after_hours=1, apply=True)
            self.assertEqual(applied["recovered_count"], 1)
            self.assertIsNone(project_workspace.current_project_id(root, session_id=session_id))
            pp = project_workspace.paths_for(root, "demo")
            self.assertTrue(any("Recovered stale session" in str(row.get("summary")) for row in project_workspace.continuity_records(pp)))

    def test_migration_advances_generation_and_returns_index_preview(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            before = json.loads(pp.project_json.read_text(encoding="utf-8"))["continuity"]["workspace_generation"]
            (pp.memory_dir / "facts.jsonl").write_text(json.dumps({"id": "legacy-one", "text": "Legacy safe fact"}) + "\n", encoding="utf-8")
            applied = continuity_migration.migrate(root, "demo", apply=True)
            after = json.loads(pp.project_json.read_text(encoding="utf-8"))["continuity"]["workspace_generation"]
            self.assertEqual(after, before + 1)
            self.assertEqual(applied["index_preview"]["status"], "preview")
            self.assertIn("record_index_preview", continuity_migration.migrate(root, "demo", apply=False))

    def test_generated_handoff_contains_bounded_git_state_and_safe_materials(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            repo = pp.project_dir / "repo"
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "tests@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Awoki Tests"], check=True)
            source = repo / "app.py"
            source.write_text("print('one')\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
            source.write_text("print('two')\n", encoding="utf-8")
            report = pp.project_dir / "reports" / "safe-summary.md"
            report.write_text("A safe project summary.", encoding="utf-8")
            project_workspace.refresh_project_files(root, "demo")
            handoff = pp.handoff.read_text(encoding="utf-8")
            self.assertIn("## Repository state", handoff)
            self.assertIn("repo/app.py", handoff)
            self.assertIn("reports/safe-summary.md", handoff)

    def test_generated_views_are_deterministic_and_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            for index in range(30):
                record = project_workspace.continuity.make_record(
                    "demo",
                    f"Finding {index}: " + ("bounded continuity detail " * 90),
                    kind="finding",
                    sources=[f"reports/finding-{index}.md"],
                    confidence="high",
                )
                saved = project_workspace.continuity.append_record(pp.continuity, record)
                if saved.get("_write_status") == "appended":
                    project_workspace.register_appended_record(root, "demo", saved)
            project_workspace.refresh_project_files(root, "demo")
            situation_one = pp.situation.read_text(encoding="utf-8")
            handoff_one = pp.handoff.read_text(encoding="utf-8")
            project_workspace.refresh_project_files(root, "demo")
            self.assertEqual(situation_one, pp.situation.read_text(encoding="utf-8"))
            self.assertEqual(handoff_one, pp.handoff.read_text(encoding="utf-8"))
            self.assertLessEqual(len(situation_one), 12_000)
            self.assertLessEqual(len(handoff_one), 32_000)
            self.assertTrue(situation_one.endswith("\n"))
            self.assertTrue(handoff_one.endswith("\n"))

    def test_incremental_index_honors_changed_project_policy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            pp = project_workspace.paths_for(root, "demo")
            meta = json.loads(pp.project_json.read_text(encoding="utf-8"))
            meta["rag"].update({
                "index_memory": False,
                "index_situation": False,
                "index_handoff": False,
                "index_notes": False,
            })
            pp.project_json.write_text(json.dumps(meta), encoding="utf-8")
            result = project_capture(
                "Policy-hidden marker is ultraviolet-marmot.",
                name="demo",
                kind="finding",
                sources=["reports/policy.md"],
                paths=paths,
            )
            self.assertIn(result.get("exact_index_sync", {}).get("status"), {"indexed", "incrementally_indexed"})
            hits = rag_backend.search_fts(
                project_fts_db(paths, project_id="demo"),
                "ultraviolet marmot",
                scope="project",
                limit=5,
            )
            self.assertEqual(hits, [])
            preview = project_index_preview("demo", paths=paths)
            self.assertFalse(preview["project_policy"]["index_memory"])
            self.assertTrue(any(item.get("reason") == "project_policy:index_memory=false" for item in preview["excluded"]))
            project_refresh(name="demo", include_qdrant=False, paths=paths)
            manifest = indexing_policy.read_index_manifest(pp.index_manifest)
            self.assertEqual(
                manifest["document_count"],
                rag_backend.fts_document_count(project_fts_db(paths, project_id="demo"), scope="project"),
            )

    def test_opencode_plugin_declares_supported_sanitized_event_hooks(self):
        plugin = (Path(__file__).resolve().parents[2] / ".opencode" / "plugins" / "awoki-continuity.ts").read_text(encoding="utf-8")
        for hook in ["tool.execute.before", "tool.execute.after", "file.edited", "session.idle", "session.compacted", "experimental.session.compacting"]:
            self.assertIn(hook, plugin)
        self.assertNotIn("tool.output", plugin)
        self.assertNotIn("conversation.text", plugin)

    def test_opencode_plugin_does_not_append_system_messages(self):
        plugin = (Path(__file__).resolve().parents[2] / ".opencode" / "plugins" / "awoki-continuity.ts").read_text(encoding="utf-8")
        self.assertNotIn("experimental.chat.system.transform", plugin)
        self.assertNotIn("output.system.push", plugin)
        self.assertNotIn("reliabilitySystemRule", plugin)





class DurableWorkLedgerTests(unittest.TestCase):
    def make_paths(self, root: Path) -> HarnessPaths:
        return HarnessPaths(root=root, global_root=root / "global")

    def test_todos_survive_without_project_and_new_user_turn_marks_review(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            session_id = "adhoc-session"
            saved = opencode_events.sync_todos(root, session_id, [
                {"id": "1", "content": "Run acceptance Test 6", "status": "in_progress", "priority": "high"},
                {"id": "2", "content": "Write final report", "status": "pending", "priority": "medium"},
            ])
            self.assertEqual(saved["status"], "saved")
            self.assertEqual(saved["project_id"], "")
            status = session_work_status(session_id=session_id, paths=paths)
            self.assertEqual(status["status"], "ok")
            self.assertEqual(len(status["todos"]), 2)
            marked = opencode_events.mark_user_turn(root, session_id, message_id="msg-user-1")
            self.assertTrue(marked["todos_need_review"])
            context = opencode_events.compaction_context(root, session_id, max_chars=8_000)
            self.assertIn("Awoki active session work", context["context"])
            self.assertIn("needs review: true", context["context"])
            self.assertIn("Run acceptance Test 6", context["context"])
            refreshed = opencode_events.sync_todos(root, session_id, [
                {"id": "3", "content": "Follow the user's newer direction", "status": "in_progress", "priority": "high"},
            ])
            self.assertEqual(refreshed["status"], "saved")
            self.assertFalse(session_work_status(session_id=session_id, paths=paths)["todos_need_review"])

    def test_todo_mirror_redacts_high_confidence_secret_values(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            opencode_events.sync_todos(root, "session", [{"id": "1", "content": "Call API with Authorization: Bearer abcdefghijklmnop", "status": "pending", "priority": "high"}])
            state = work_ledger.status(root, "session")
            self.assertIn("<REDACTED>", state["todos"][0]["content"])
            self.assertTrue(state["todos"][0]["redacted"])
            self.assertNotIn("abcdefghijklmnop", json.dumps(state))

    def test_duplicate_message_updated_does_not_advance_user_generation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            opencode_events.sync_todos(root, "session", [{"id": "1", "content": "A", "status": "pending", "priority": "low"}])
            first = opencode_events.mark_user_turn(root, "session", message_id="same-message")
            second = opencode_events.mark_user_turn(root, "session", message_id="same-message")
            self.assertEqual(first["user_turn_generation"], second["user_turn_generation"])
            self.assertEqual(second["status"], "unchanged")

    def test_compaction_generation_is_durable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            opencode_events.mark_compacted(root, "session")
            opencode_events.mark_compacted(root, "session")
            state = work_ledger.status(root, "session")
            self.assertEqual(state["compaction_generation"], 2)

    def test_compaction_budget_preserves_operational_ledgers_before_large_project_prose(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            opencode_events.sync_todos(root, "session", [{"id": "1", "content": "Continue acceptance", "status": "in_progress", "priority": "high"}])
            with (
                mock.patch.object(project_workspace, "current_project_id", return_value="demo"),
                mock.patch.object(project_workspace, "project_handoff", return_value={"situation": "S" * 20_000, "handoff": "H" * 20_000}),
                mock.patch.object(acceptance_runs, "compact_context", return_value="## Awoki acceptance-run continuity\nrun_id: acr_demo"),
            ):
                context = opencode_events.compaction_context(root, "session", max_chars=5_000)["context"]
            self.assertIn("Awoki active session work", context)
            self.assertIn("Awoki acceptance-run continuity", context)
            self.assertIn("Awoki reliability invariants", context)
            self.assertLessEqual(len(context), 5_000)

    def test_todo_sync_cli_reads_payload_from_stdin_not_argv(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = dict(os.environ)
            env["AWOKI_ROOT"] = str(root)
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "opencode_events.py"), "todo-sync", "--session-id", "cli-session"],
                input=json.dumps({"todos": [{"id": "1", "content": "Persist me", "status": "pending", "priority": "high"}]}),
                text=True, capture_output=True, env=env, check=True,
            )
            result = json.loads(proc.stdout)
            self.assertEqual(result["status"], "saved")
            state = work_ledger.status(root, "cli-session")
            self.assertEqual(state["todos"][0]["content"], "Persist me")


    def test_todo_ids_are_ledger_owned_and_stable_without_opencode_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = opencode_events.sync_todos(root, "session", [
                {"id": "", "content": "alpha", "status": "pending", "priority": "medium"},
                {"id": "", "content": "beta", "status": "pending", "priority": "medium"},
                {"id": "", "content": "beta", "status": "pending", "priority": "medium"},
            ])
            self.assertEqual(first["status"], "saved")
            state1 = work_ledger.status(root, "session")
            ids1 = [row["id"] for row in state1["todos"]]
            self.assertEqual(len(ids1), len(set(ids1)))
            self.assertTrue(all(value.startswith("atd_") for value in ids1))

            opencode_events.sync_todos(root, "session", [
                {"id": "", "content": "beta", "status": "in_progress", "priority": "medium"},
                {"id": "", "content": "alpha", "status": "completed", "priority": "medium"},
                {"id": "", "content": "beta", "status": "pending", "priority": "medium"},
            ])
            state2 = work_ledger.status(root, "session")
            alpha2 = next(row for row in state2["todos"] if row["content"] == "alpha")
            alpha1 = next(row for row in state1["todos"] if row["content"] == "alpha")
            self.assertEqual(alpha2["id"], alpha1["id"])
            self.assertEqual({row["id"] for row in state2["todos"]}, set(ids1))

            # Same-length single-item rename keeps the remaining ledger identity.
            before_by_id = {row["id"]: row["content"] for row in state2["todos"]}
            opencode_events.sync_todos(root, "session", [
                {"id": "", "content": "beta", "status": "in_progress", "priority": "medium"},
                {"id": "", "content": "alpha-renamed", "status": "completed", "priority": "medium"},
                {"id": "", "content": "beta", "status": "pending", "priority": "medium"},
            ])
            state3 = work_ledger.status(root, "session")
            renamed = next(row for row in state3["todos"] if row["content"] == "alpha-renamed")
            self.assertEqual(renamed["id"], alpha1["id"])
            self.assertEqual(set(before_by_id), {row["id"] for row in state3["todos"]})

    def test_status_migrates_legacy_empty_todo_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_id = "legacy-session"
            path = work_ledger._path(root, session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "schema": "awoki-session-work/v1",
                "session_key": work_ledger._key(session_id),
                "todo_generation": 2,
                "todos": [
                    {"id": "", "content": "legacy-a", "status": "in_progress", "priority": "medium", "redacted": False},
                    {"id": "", "content": "legacy-b", "status": "pending", "priority": "medium", "redacted": False},
                ],
            }), encoding="utf-8")
            state = work_ledger.status(root, session_id)
            self.assertEqual(state["schema"], "awoki-session-work/v3")
            self.assertTrue(all(row["id"].startswith("atd_") for row in state["todos"]))
            self.assertEqual(len({row["id"] for row in state["todos"]}), 2)


class AcceptanceRunPersistenceTests(unittest.TestCase):
    def make_paths(self, root: Path) -> HarnessPaths:
        return HarnessPaths(root=root, global_root=root / "global")

    def _repo(self, root: Path, paths: HarnessPaths) -> None:
        if not shutil.which("git"):
            self.skipTest("git unavailable")
        project_create("demo", paths=paths)
        repo = project_workspace.paths_for(root, "demo").project_dir / "repo" / "service"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Awoki Tests"], cwd=repo, check=True)
        (repo / "app.go").write_text("package app\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.go"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
        added = project_repo_add("service", "repo/service", name="demo", make_default=True, paths=paths)
        self.assertIn(added["status"], {"registered", "updated"})

    def test_acceptance_run_rejects_raw_blob_and_survives_compaction_with_compact_observation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1", "test-2"],
                name="demo", repo="service", session_id="accept-session", paths=paths,
            )
            run_id = started["run_id"]
            rejected = acceptance_run_record(
                run_id, "test-1", "pass", name="demo", query="bearer token",
                targets=["TestAuthenticatorBearerToken"],
                evidence={"raw_payload": "BEGIN_RAW\npackage app\n" + ("x" * 5000)},
                session_id="accept-session", paths=paths,
            )
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(rejected["reason"], "acceptance_evidence_invalid")
            self.assertTrue(any("raw_payload" in item for item in rejected["errors"]))

            first = acceptance_run_record(
                run_id, "test-1", "pass", name="demo", query="bearer token",
                targets=["TestAuthenticatorBearerToken"],
                evidence={"final_rank": 5, "backend_note": "Authorization: Bearer abcdefghijklmnop"},
                session_id="accept-session", paths=paths,
            )
            self.assertEqual(first["status"], "recorded")
            context = opencode_events.compaction_context(root, "accept-session", max_chars=12_000)
            self.assertIn("Awoki acceptance-run continuity", context["context"])
            self.assertIn("test-2", context["context"])
            state = acceptance_run_status(run_id, name="demo", session_id="accept-session", paths=paths)
            self.assertEqual(state["records"]["test-1"]["evidence"]["final_rank"], 5)
            self.assertIn("<REDACTED>", state["records"]["test-1"]["evidence"]["backend_note"])
            self.assertNotIn("abcdefghijklmnop", json.dumps(state))

            incomplete = acceptance_run_finalize(run_id, name="demo", session_id="accept-session", paths=paths)
            self.assertEqual(incomplete["status"], "rejected")
            self.assertEqual(incomplete["reason"], "acceptance_incomplete")
            self.assertEqual(incomplete["missing_tests"], ["test-2"])
            self.assertEqual(acceptance_run_status(run_id, name="demo", paths=paths)["run_status"], "running")

    def test_acceptance_run_typed_candidates_reference_content_addressed_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1"],
                name="demo", repo="service", session_id="accept-session", paths=paths,
            )
            run_id = started["run_id"]
            search = codebase_search(
                "package app", name="demo", repo="service", mode="lexical",
                use_qdrant=False, use_reranker=False, capture_evidence=True,
                acceptance_run_id=run_id, session_id="accept-session", paths=paths,
            )
            self.assertEqual(search["status"], "ok")
            capture = search["evidence_capture"]
            self.assertEqual(capture["status"], "stored")
            evidence_ref = capture["evidence_ref"]
            self.assertTrue(evidence_ref.startswith("ev_"))
            self.assertGreaterEqual(capture["candidate_count"], 1)
            candidate_id = capture["candidate_index"][0]["candidate_id"]

            record = acceptance_run_record(
                run_id, "test-1", "pass", name="demo", query="package app",
                targets=["app"], evidence_refs=[evidence_ref], candidate_ids=[candidate_id],
                primary_candidate_id=candidate_id, evidence={"target_found": True},
                session_id="accept-session", paths=paths,
            )
            self.assertEqual(record["status"], "recorded")
            state = acceptance_run_status(run_id, name="demo", paths=paths)
            row = state["records"]["test-1"]
            self.assertEqual(row["primary_candidate_id"], candidate_id)
            self.assertEqual(row["candidates"][0]["candidate_id"], candidate_id)
            self.assertEqual(row["candidates"][0]["final_rank"], 1)
            self.assertEqual(row["evidence_refs"][0]["evidence_ref"], evidence_ref)

            retrieved = acceptance_evidence_get(
                evidence_ref, run_id=run_id, name="demo", selector="payload.hits", limit=5, paths=paths,
            )
            self.assertEqual(retrieved["status"], "ok")
            self.assertEqual(retrieved["kind"], "list")
            self.assertGreaterEqual(retrieved["total"], 1)
            self.assertIn("preview", retrieved["value"][0])  # rich evidence remains recoverable on demand

            contradictory = acceptance_run_record(
                run_id, "test-1", "pass", name="demo", evidence_refs=[evidence_ref],
                candidate_ids=[candidate_id], evidence={"final_rank": 999}, paths=paths,
            )
            self.assertEqual(contradictory["status"], "rejected")
            self.assertEqual(contradictory["reason"], "acceptance_evidence_invalid")

    def test_captured_diagnostics_preserve_deep_trace_beyond_process_handle(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            repo = project_workspace.paths_for(root, "demo").project_dir / "repo" / "service"
            (repo / "app.go").write_text("package app\nfunc Authenticate() {}\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.go"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "add symbol"], cwd=repo, check=True)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1"],
                name="demo", repo="service", paths=paths,
            )
            result = codebase_search(
                "Authenticate", name="demo", repo="service", mode="lexical", view="diagnostics",
                use_qdrant=False, use_reranker=False, capture_evidence=True,
                acceptance_run_id=started["run_id"], paths=paths,
            )
            capture = result["evidence_capture"]
            self.assertEqual(capture["status"], "stored")
            trace_descriptor = (result.get("details") or {}).get("candidate_trace") or {}
            self.assertEqual(trace_descriptor.get("storage"), "mcp_process_memory")
            retrieved = acceptance_evidence_get(
                capture["evidence_ref"], name="demo",
                selector="payload._captured_diagnostic_trace.candidate_trace.rows", limit=5, paths=paths,
            )
            self.assertEqual(retrieved["status"], "ok")
            self.assertEqual(retrieved["kind"], "list")
            self.assertGreaterEqual(retrieved["total"], 1)

    def test_reranker_diagnostic_selector_is_derived_without_mutating_payload(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = {
                "status": "ok",
                "details": {
                    "retrieval": {
                        "reranker": {
                            "attempted": True,
                            "applied": True,
                            "backend": "tei",
                            "timeout_seconds": 20.0,
                            "timeout_source": "AWOKI_RERANK_TIMEOUT_SECONDS",
                            "scores_returned_to_awoki": 30,
                            "failure_class": "none",
                            "degraded": False,
                        },
                        "rerank_attempted": True,
                        "rerank_applied": True,
                        "rerank_backend": "tei",
                        "rerank_timeout_seconds": 20.0,
                        "rerank_timeout_source": "AWOKI_RERANK_TIMEOUT_SECONDS",
                        "rerank_scores_returned_to_awoki": 30,
                        "rerank_failure_class": "none",
                        "rerank_degraded": False,
                    }
                },
            }
            original = json.loads(json.dumps(payload))
            stored = evidence_store.put(
                root, "demo", kind="code_search", tool="codebase_search", payload=payload,
                scope_identity={"project_id": "demo", "repository_revision": "abc"},
                run_id="acr_demo", test_id="T13", session_id="ses_demo",
            )
            self.assertEqual(stored["status"], "stored")
            self.assertEqual(payload, original)
            selected = evidence_store.get(
                root, "demo", stored["evidence_ref"], selector="backend_observations.reranker"
            )
            self.assertEqual(selected["status"], "ok")
            self.assertEqual(selected["kind"], "dict")
            self.assertEqual(selected["value"]["backend"], "tei")
            self.assertEqual(selected["value"]["scores_returned"], 30)
            self.assertEqual(selected["value"]["timeout_seconds"], 20.0)
            reread = evidence_store.get(root, "demo", stored["evidence_ref"], selector="payload")
            self.assertEqual(reread["value"], original)

    def test_evidence_artifact_is_non_rag_and_integrity_checked(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1"],
                name="demo", repo="service", paths=paths,
            )
            search = codebase_search(
                "package app", name="demo", repo="service", mode="lexical",
                use_qdrant=False, use_reranker=False, capture_evidence=True,
                acceptance_run_id=started["run_id"], paths=paths,
            )
            ref = search["evidence_capture"]["evidence_ref"]
            artifact_path = evidence_store._path(root, "demo", ref)
            project_dir = project_workspace.paths_for(root, "demo").project_dir
            relative = artifact_path.relative_to(project_dir).as_posix()
            self.assertIn("/raw/", f"/{relative}/")
            with self.assertRaisesRegex(ValueError, "outside raw directories"):
                indexing_policy.register_safe_artifact(project_workspace.paths_for(root, "demo").index_dir, relative)

            meta = evidence_store.metadata(root, "demo", ref)
            self.assertEqual(meta["status"], "ok")
            self.assertRegex(meta["artifact_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(meta["payload_sha256"], r"^[0-9a-f]{64}$")

            envelope = json.loads(gzip.decompress(artifact_path.read_bytes()).decode("utf-8"))
            envelope["payload"]["status"] = "tampered"
            artifact_path.write_bytes(gzip.compress(
                json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"),
                mtime=0,
            ))
            integrity = evidence_store.metadata(root, "demo", ref)
            self.assertEqual(integrity["status"], "integrity_error")
            fetched = acceptance_evidence_get(ref, name="demo", paths=paths)
            self.assertEqual(fetched["status"], "integrity_error")
            record = acceptance_run_record(
                started["run_id"], "test-1", "pass", name="demo", evidence_refs=[ref], paths=paths,
            )
            self.assertEqual(record["status"], "rejected")
            self.assertEqual(record["reason"], "acceptance_evidence_invalid")

    def test_acceptance_run_legacy_v1_record_migrates_without_losing_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1"],
                name="demo", repo="service", paths=paths,
            )
            run_id = started["run_id"]
            run_path = acceptance_runs._path(root, "demo", run_id)
            state = json.loads(run_path.read_text(encoding="utf-8"))
            state["schema"] = "awoki-acceptance-run/v1"
            run_path.write_text(json.dumps(state), encoding="utf-8")
            recorded = acceptance_run_record(
                run_id, "test-1", "pass", name="demo", evidence={"final_rank": 1}, paths=paths,
            )
            self.assertEqual(recorded["status"], "recorded")
            migrated = acceptance_run_status(run_id, name="demo", paths=paths)
            self.assertEqual(migrated["schema"], "awoki-acceptance-run/v4")
            self.assertEqual(migrated["records"]["test-1"]["evidence"]["final_rank"], 1)

    def test_legacy_v1_raw_evidence_is_moved_out_of_compact_ledger_without_loss(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1"],
                name="demo", repo="service", paths=paths,
            )
            run_id = started["run_id"]
            run_path = acceptance_runs._path(root, "demo", run_id)
            state = json.loads(run_path.read_text(encoding="utf-8"))
            state["schema"] = "awoki-acceptance-run/v1"
            state["records"] = {
                "test-1": {
                    "test_id": "test-1", "outcome": "pass", "query": "q", "targets": ["target"],
                    "evidence": {
                        "final_rank": 5,
                        "TestAuthenticatorBearerToken_final_rank": 7,
                        "raw_payload": "BEGIN_RAW\npackage example\nEND_RAW",
                    },
                }
            }
            run_path.write_text(json.dumps(state), encoding="utf-8")

            migrated = acceptance_run_status(run_id, name="demo", paths=paths)
            self.assertEqual(migrated["schema"], "awoki-acceptance-run/v4")
            row = migrated["records"]["test-1"]
            self.assertEqual(row["evidence"], {"final_rank": 5})
            self.assertNotIn("raw_payload", json.dumps(row["evidence"]))
            self.assertNotIn("TestAuthenticatorBearerToken_final_rank", json.dumps(row["evidence"]))
            self.assertEqual(row["authority"], "legacy_recorded_observation")
            self.assertEqual(len(row["legacy_evidence_refs"]), 1)
            legacy_ref = row["legacy_evidence_refs"][0]["evidence_ref"]
            recovered = acceptance_evidence_get(
                legacy_ref, name="demo", selector="payload.legacy_evidence.raw_payload", paths=paths,
            )
            self.assertEqual(recovered["status"], "ok")
            self.assertIn("BEGIN_RAW", recovered["value"])

    def test_compact_observation_key_matching_does_not_reject_innocent_draw_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1"], name="demo", repo="service", paths=paths
            )
            recorded = acceptance_run_record(
                started["run_id"], "test-1", "pass", name="demo",
                evidence={"draw_count": 2, "payload_sha256": "abc"}, paths=paths,
            )
            self.assertEqual(recorded["status"], "recorded")
            state = acceptance_run_status(started["run_id"], name="demo", paths=paths)
            self.assertEqual(state["records"]["test-1"]["evidence"]["draw_count"], 2)

    def test_acceptance_run_complete_requires_all_tests_and_invariants(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1"], expected_invariants=["tei-completeness"],
                name="demo", repo="service", session_id="accept-session", paths=paths,
            )
            run_id = started["run_id"]
            acceptance_run_record(run_id, "test-1", "pass", name="demo", evidence={"tei_scores": "30/30"}, session_id="accept-session", paths=paths)
            acceptance_run_record_invariant(run_id, "tei-completeness", "hold", name="demo", evidence={"selected": 30, "scored": 30}, session_id="accept-session", paths=paths)
            final = acceptance_run_finalize(run_id, name="demo", session_id="accept-session", paths=paths)
            self.assertEqual(final["ledger_outcome"], "complete")
            self.assertTrue(final["finalized"])
            self.assertIn("does not convert model-recorded evidence", final["assessment_basis"] if "assessment_basis" in final else "")

    def test_acceptance_run_incomplete_finalize_is_rejected_and_resumable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1", "test-2"], expected_invariants=["tei-completeness"],
                name="demo", repo="service", session_id="accept-session", paths=paths,
            )
            run_id = started["run_id"]
            acceptance_run_record(run_id, "test-1", "pass", name="demo", evidence={"final_rank": 1}, session_id="accept-session", paths=paths)
            final = acceptance_run_finalize(run_id, name="demo", session_id="accept-session", paths=paths)
            self.assertEqual(final["status"], "rejected")
            self.assertEqual(final["reason"], "acceptance_incomplete")
            self.assertEqual(final["missing_tests"], ["test-2"])
            self.assertEqual(final["missing_invariants"], ["tei-completeness"])
            self.assertFalse(final["finalized"])
            self.assertEqual(acceptance_run_status(run_id, name="demo", paths=paths)["run_status"], "running")
            # The same run remains writable after the premature finalize attempt.
            second = acceptance_run_record(run_id, "test-2", "pass", name="demo", evidence={"final_rank": 2}, paths=paths)
            self.assertEqual(second["status"], "recorded")
            acceptance_run_record_invariant(run_id, "tei-completeness", "hold", name="demo", evidence={"selected": 30, "scored": 30}, paths=paths)
            finished = acceptance_run_finalize(run_id, name="demo", paths=paths)
            self.assertEqual(finished["status"], "finalized")
            self.assertEqual(finished["ledger_outcome"], "complete")

    def test_acceptance_run_completed_but_failing_evidence_is_terminal_not_passed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1"], expected_invariants=["tei-completeness"],
                name="demo", repo="service", session_id="accept-session", paths=paths,
            )
            run_id = started["run_id"]
            acceptance_run_record(run_id, "test-1", "fail", name="demo", evidence={"reason_code": "rank_regression"}, paths=paths)
            acceptance_run_record_invariant(run_id, "tei-completeness", "hold", name="demo", evidence={"selected": 30, "scored": 30}, paths=paths)
            final = acceptance_run_finalize(run_id, name="demo", paths=paths)
            self.assertEqual(final["status"], "finalized")
            self.assertEqual(final["ledger_outcome"], "not_passed")
            self.assertEqual(final["nonpassing_tests"], ["test-1"])

    def test_candidate_specific_metric_aliases_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start("retrieval-holdout", expected_tests=["test-1"], name="demo", repo="service", paths=paths)
            result = acceptance_run_record(
                started["run_id"], "test-1", "pass", name="demo",
                evidence={"TestAuthenticatorBearerToken_final_rank": 5}, paths=paths,
            )
            self.assertEqual(result["status"], "rejected")
            self.assertTrue(any("candidate-specific metric aliases" in item for item in result["errors"]))

    def test_acceptance_run_rejects_revision_drift_before_recording_more_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "retrieval-holdout", expected_tests=["test-1"], name="demo", repo="service",
                session_id="accept-session", paths=paths,
            )
            run_id = started["run_id"]
            repo = project_workspace.paths_for(root, "demo").project_dir / "repo" / "service"
            (repo / "app.go").write_text("package app\n// changed\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.go"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-qm", "change during acceptance"], cwd=repo, check=True)
            result = acceptance_run_record(
                run_id, "test-1", "pass", name="demo", evidence={"final_rank": 1},
                session_id="accept-session", paths=paths,
            )
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["reason"], "acceptance_scope_drift")
            self.assertEqual(result["scope_guard"]["status"], "scope_drift")


class AgentRuntimeBoundaryTests(unittest.TestCase):
    def make_paths(self, root: Path) -> HarnessPaths:
        return HarnessPaths(root=root, global_root=root / "global")

    def test_reasoning_only_terminal_turn_is_structurally_detected_without_reasoning_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            result = opencode_events.record_agent_terminal_turn(
                root, "session-1", message_id="msg-a", finish_reason="stop",
                has_reasoning=True, has_text=False, has_tool=False,
                provider_id="openai-compatible", model_id="qwen3.8-27b", agent_mode="build",
                error_type="", step_finish_seen=True, input_tokens=64765, output_tokens=4096, reasoning_tokens=4096,
            )
            self.assertEqual(result["runtime_state"], "degraded")
            self.assertTrue(result["unresolved_anomaly"])
            self.assertEqual(result["last_anomaly"]["classification"], "reasoning_only_terminal_turn")
            serialized = json.dumps(result)
            self.assertNotIn("reasoning_text", serialized.lower())
            self.assertNotIn("chain_of_thought", serialized.lower())
            self.assertEqual(
                set(result["last_anomaly"]),
                {"classification", "message_id", "finish_reason", "reasoning_present", "text_present", "tool_present", "provider_id", "model_id", "agent_mode", "error_type", "step_finish_seen", "input_tokens", "output_tokens", "reasoning_tokens", "tool_executions_completed", "observed_at"},
            )
            status = session_runtime_status("session-1", paths=paths)
            self.assertEqual(status["last_anomaly"]["model_id"], "qwen3.8-27b")
            self.assertEqual(status["last_anomaly"]["provider_id"], "openai-compatible")
            self.assertTrue(status["last_anomaly"]["step_finish_seen"])
            self.assertEqual(status["last_anomaly"]["reasoning_tokens"], 4096)
            self.assertEqual(status["automatic_recovery_attempts"], 0)
            self.assertIn("never persisted", status["privacy"])

    def test_session_runtime_status_exposes_only_recorded_opencode_version_tuple(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            manifest = root / "opencode-runtime.json"
            manifest.write_text(json.dumps({
                "schema": 1, "install_mode": "latest", "channel_state": "latest_untested",
                "requested_safe_version": "", "resolved_cli": "9.9.9",
                "resolved_plugin": "9.9.9", "resolved_sdk": "9.9.9",
                "should_not_escape": "secret-like-extra-field",
            }), encoding="utf-8")
            with mock.patch.dict(os.environ, {"AWOKI_OPENCODE_RUNTIME_MANIFEST": str(manifest)}):
                status = session_runtime_status("session-manifest", paths=paths)
            self.assertEqual(status["opencode_runtime"]["install_mode"], "latest")
            self.assertEqual(status["opencode_runtime"]["channel_state"], "latest_untested")
            self.assertEqual(status["opencode_runtime"]["resolved_cli"], "9.9.9")
            self.assertEqual(status["opencode_runtime"]["resolved_plugin"], "9.9.9")
            self.assertEqual(status["opencode_runtime"]["resolved_sdk"], "9.9.9")
            self.assertNotIn("should_not_escape", status["opencode_runtime"]
            )

    def test_manual_followup_recovery_accounting_is_separate_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            opencode_events.record_agent_terminal_turn(
                root, "session-2", message_id="msg-a", finish_reason="stop",
                has_reasoning=True, has_text=False, has_tool=False,
            )
            first = opencode_events.mark_user_turn(root, "session-2", message_id="user-recover")
            second = opencode_events.mark_user_turn(root, "session-2", message_id="user-recover")
            self.assertEqual(first["agent_runtime"]["agent_turn_recovery_attempts"], 1)
            self.assertEqual(second["agent_runtime"]["agent_turn_recovery_attempts"], 1)
            recovered = opencode_events.record_agent_terminal_turn(
                root, "session-2", message_id="msg-b", finish_reason="stop",
                has_reasoning=True, has_text=True, has_tool=False,
            )
            self.assertFalse(recovered["unresolved_anomaly"])
            self.assertEqual(recovered["recovered_count"], 1)
            self.assertEqual(recovered["automatic_recovery_attempts"], 0)

            # A later anomaly is not considered recovered merely because an older
            # anomaly had a manual follow-up. Each incident needs its own user turn.
            opencode_events.record_agent_terminal_turn(
                root, "session-2", message_id="msg-c", finish_reason="stop",
                has_reasoning=True, has_text=False, has_tool=False,
            )
            no_followup = opencode_events.record_agent_terminal_turn(
                root, "session-2", message_id="msg-d", finish_reason="stop",
                has_reasoning=False, has_text=True, has_tool=False,
            )
            self.assertTrue(no_followup["unresolved_anomaly"])
            self.assertEqual(no_followup["recovered_count"], 1)

    def test_completed_tool_without_text_followup_is_separate_runtime_anomaly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            result = opencode_events.record_agent_terminal_turn(
                root, "session-tool-stall", message_id="msg-tool", finish_reason="stop",
                has_reasoning=True, has_text=False, has_tool=True,
                tool_executions_completed=1,
            )
            self.assertEqual(result["runtime_state"], "degraded")
            self.assertEqual(result["last_anomaly"]["classification"], "tool_execution_without_followup")
            self.assertEqual(result["last_anomaly"]["tool_executions_completed"], 1)
            self.assertEqual(result["automatic_recovery_attempts"], 0)


class ReferenceCatalogTests(unittest.TestCase):
    def make_paths(self, root: Path) -> HarnessPaths:
        return HarnessPaths(root=root, global_root=root / "global")

    def _repo(self, root: Path, paths: HarnessPaths) -> None:
        project_create("demo", paths=paths)
        repo = project_workspace.paths_for(root, "demo").project_dir / "repo" / "service"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "awoki@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Awoki"], cwd=repo, check=True)
        (repo / "app.go").write_text("package app\nfunc Authenticate() {}\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.go"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
        project_repo_add("service", "repo/service", name="demo", make_default=True, paths=paths)

    def test_human_reference_metadata_keeps_stable_id_authoritative(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            session_id = "reference-session"
            project_open("demo", session_id=session_id, paths=paths)
            started = acceptance_run_start(
                "reference-suite", title="Compaction continuity acceptance", expected_tests=["T1"],
                name="demo", repo="service", session_id=session_id, paths=paths,
            )
            run_desc = reference_describe(started["run_id"], name="demo", session_id=session_id, paths=paths)
            self.assertEqual(run_desc["status"], "ok")
            self.assertEqual(run_desc["kind"], "acceptance_run")
            self.assertEqual(run_desc["label"], "Compaction continuity acceptance")
            self.assertIn("why_saved", run_desc)

            scope = acceptance_runs.scope_identity(dict(started["scope"]))
            stored = evidence_store.put(
                root, "demo", kind="code_search_result", tool="codebase_search",
                payload={"hits": [{"path": "app.go", "start_line": 2, "symbol_name": "Authenticate", "final_rank": 1}]},
                scope_identity=scope, run_id=started["run_id"], session_id=session_id,
            )
            ev = stored["evidence_ref"]
            annotated = reference_annotate(
                ev, label="Bearer-token authentication retrieval",
                why_saved="Used to verify retrieval and reranker behavior without rerunning the query.",
                aliases=["the bearer token evidence", "reranker evidence"], linked_refs=[started["run_id"]],
                name="demo", session_id=session_id, paths=paths,
            )
            self.assertEqual(annotated["reference_id"], ev)
            self.assertEqual(annotated["label"], "Bearer-token authentication retrieval")
            self.assertIn(started["run_id"], annotated["linked_refs"])
            self.assertIn("Stable ID is authoritative", annotated["reference_contract"])

            resolved = reference_resolve("the reranker evidence", name="demo", session_id=session_id, paths=paths)
            self.assertEqual(resolved["status"], "ok")
            self.assertTrue(resolved["matches"])
            self.assertEqual(resolved["matches"][0]["reference_id"], ev)
            self.assertIn("navigation only", resolved["resolution_boundary"])

            candidate_id = stored["candidate_index"][0]["candidate_id"]
            candidate_desc = reference_describe(candidate_id, name="demo", session_id=session_id, paths=paths)
            self.assertEqual(candidate_desc["status"], "ok")
            self.assertEqual(candidate_desc["kind"], "retrieval_candidate")
            self.assertEqual(candidate_desc["linked_refs"], [ev])

            reliability_dir = project_workspace.paths_for(root, "demo").project_dir / "reports" / "reliability"
            reliability_dir.mkdir(parents=True, exist_ok=True)
            (reliability_dir / "reference-run.json").write_text(json.dumps({
                "run_id": "reliability-reference-run",
                "verification_checkpoints": [{"checkpoint_id": "vrf_reference123", "iteration": 1, "result": "VERIFIED"}],
                "relations": [{"relation_id": "rel_reference123", "from_node_id": "asn_source123", "type": "supports", "to_node_id": "asn_target123"}],
                "assessments": [{"node_id": "asn_source123", "kind": "hypothesis", "statement": "Authentication selection is configuration-driven.", "evidence_refs": []}],
            }, sort_keys=True), encoding="utf-8")
            checkpoint_desc = reference_describe("vrf_reference123", name="demo", session_id=session_id, paths=paths)
            relation_desc = reference_describe("rel_reference123", name="demo", session_id=session_id, paths=paths)
            assessment_desc = reference_describe("asn_source123", name="demo", session_id=session_id, paths=paths)
            self.assertEqual(checkpoint_desc["kind"], "verification_checkpoint")
            self.assertEqual(relation_desc["kind"], "relation")
            self.assertEqual(assessment_desc["kind"], "assessment")
            self.assertEqual(relation_desc["linked_refs"], ["asn_source123", "asn_target123"])


    def test_reference_resolution_refuses_ambiguous_natural_language_match(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start("refs", expected_tests=["T1"], name="demo", repo="service", paths=paths)
            scope = acceptance_runs.scope_identity(dict(started["scope"]))
            first = evidence_store.put(root, "demo", kind="one", tool="unit", payload={"hits": [{"path":"a.go","start_line":1,"symbol_name":"A"}]}, scope_identity=scope)
            second = evidence_store.put(root, "demo", kind="two", tool="unit", payload={"hits": [{"path":"b.go","start_line":1,"symbol_name":"B"}]}, scope_identity=scope)
            for ev in (first["evidence_ref"], second["evidence_ref"]):
                reference_annotate(ev, label="Reranker evidence", aliases=["shared reranker evidence"], name="demo", paths=paths)
            resolved = reference_resolve("shared reranker evidence", name="demo", paths=paths)
            self.assertEqual(resolved["status"], "ok")
            self.assertEqual(resolved["resolution"]["status"], "ambiguous")
            self.assertEqual(resolved["resolved_reference_id"], "")
            self.assertGreaterEqual(len(resolved["matches"]), 2)

    def test_compaction_injects_only_current_session_reference_working_set(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            first_session = "reference-old-session"
            second_session = "reference-current-session"
            project_open("demo", session_id=first_session, paths=paths)
            started = acceptance_run_start(
                "refs", expected_tests=["T1"], name="demo", repo="service",
                session_id=first_session, paths=paths,
            )
            scope = acceptance_runs.scope_identity(dict(started["scope"]))
            stored = evidence_store.put(
                root, "demo", kind="search", tool="codebase_search",
                payload={"hits": [{"path":"app.go","start_line":1,"symbol_name":"Authenticate"}]},
                scope_identity=scope,
            )
            ev = stored["evidence_ref"]
            reference_annotate(
                ev, label="Old-session bearer evidence", why_saved="Old investigation reference.",
                name="demo", session_id=first_session, paths=paths,
            )

            project_open("demo", session_id=second_session, paths=paths)
            before = opencode_events.compaction_context(root, second_session, max_chars=12_000)["context"]
            self.assertNotIn("Old-session bearer evidence", before)
            self.assertNotIn(ev, before)

            described = reference_describe(ev, name="demo", session_id=second_session, paths=paths)
            self.assertEqual(described["status"], "ok")
            after = opencode_events.compaction_context(root, second_session, max_chars=12_000)["context"]
            self.assertIn("Awoki current-session references", after)
            self.assertIn("reference_set_needs_review: false", after)
            self.assertIn("Old-session bearer evidence", after)
            self.assertIn(ev, after)

            opencode_events.mark_user_turn(root, second_session, message_id="new-direction")
            review = opencode_events.compaction_context(root, second_session, max_chars=12_000)["context"]
            self.assertIn("reference_set_needs_review: true", review)
            self.assertIn("Reconcile them with the newest direction", review)

    def test_reference_review_flag_remains_until_all_stale_session_refs_are_reused(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            session_id = "reference-review-session"
            work_ledger.touch_reference(root, session_id, project_id="demo", reference_id="ev_one", label="one")
            work_ledger.touch_reference(root, session_id, project_id="demo", reference_id="ev_two", label="two")
            work_ledger.mark_user_turn(root, session_id, message_id="new-user-turn")
            state = work_ledger.status(root, session_id)
            self.assertTrue(state["references_need_review"])

            work_ledger.touch_reference(root, session_id, project_id="demo", reference_id="ev_one", label="one")
            state = work_ledger.status(root, session_id)
            self.assertTrue(state["references_need_review"])

            work_ledger.touch_reference(root, session_id, project_id="demo", reference_id="ev_two", label="two")
            state = work_ledger.status(root, session_id)
            self.assertFalse(state["references_need_review"])

    def test_candidate_reference_distinguishes_first_materialization_from_later_occurrences(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start("refs", expected_tests=["T1"], name="demo", repo="service", paths=paths)
            scope = acceptance_runs.scope_identity(dict(started["scope"]))
            base_hit = {"path":"app.go","start_line":1,"symbol_name":"Authenticate","final_rank":1}
            first = evidence_store.put(root, "demo", kind="search-one", tool="codebase_search", payload={"hits":[dict(base_hit)]}, scope_identity=scope)
            second_hit = dict(base_hit); second_hit["final_score"] = 0.9
            second = evidence_store.put(root, "demo", kind="search-two", tool="codebase_search", payload={"hits":[second_hit]}, scope_identity=scope)
            self.assertNotEqual(first["evidence_ref"], second["evidence_ref"])
            candidate_id = first["candidate_index"][0]["candidate_id"]
            self.assertEqual(candidate_id, second["candidate_index"][0]["candidate_id"])
            desc = reference_describe(candidate_id, name="demo", paths=paths)
            self.assertEqual(desc["origin"]["first_materialized_in"], first["evidence_ref"])
            self.assertEqual(desc["origin"]["observed_in"], [first["evidence_ref"], second["evidence_ref"]])
            self.assertEqual(desc["origin"]["occurrence_count"], 2)
            self.assertFalse(desc["origin"]["occurrence_scan_truncated"])
            self.assertEqual(desc["linked_refs"], [first["evidence_ref"], second["evidence_ref"]])


class AcceptanceRunProgressionTests(unittest.TestCase):
    def make_paths(self, root: Path) -> HarnessPaths:
        return HarnessPaths(root=root, global_root=root / "global")

    def _repo(self, root: Path, paths: HarnessPaths) -> None:
        project_create("demo", paths=paths)
        repo = project_workspace.paths_for(root, "demo").project_dir / "repo" / "service"
        repo.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "awoki@example.invalid"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Awoki"], cwd=repo, check=True)
        (repo / "app.go").write_text("package app\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.go"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=repo, check=True)
        project_repo_add("service", "repo/service", name="demo", make_default=True, paths=paths)

    def test_acceptance_run_next_returns_only_next_bounded_step(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "suite", expected_tests=["T1", "T2"], name="demo", repo="service", paths=paths,
                test_plan=[
                    {"test_id":"T1", "objective":"Check one", "allowed_actions":["read"], "forbidden_actions":["write"], "stop_after":False},
                    {"test_id":"T2", "objective":"Compaction boundary", "stop_after":True},
                ],
            )
            first = acceptance_run_next(started["run_id"], name="demo", paths=paths)
            self.assertEqual(first["status"], "ready")
            self.assertEqual(first["test_id"], "T1")
            self.assertEqual(first["allowed_actions"], ["read"])
            self.assertIn("reliability ledger", first["corrective_budget_state"])
            self.assertIn("not authorization grants", first["policy_enforcement"])
            acceptance_run_record(started["run_id"], "T1", "pass", name="demo", evidence={"observed": True}, paths=paths)
            second = acceptance_run_next(started["run_id"], name="demo", paths=paths)
            self.assertEqual(second["test_id"], "T2")
            self.assertTrue(second["stop_after"])
            self.assertEqual(second["completed_tests"], ["T1"])

    def test_durable_contract_survives_compaction_with_generation_and_execution_invariants(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            session_id = "accept-contract-session"
            project_open("demo", session_id=session_id, paths=paths)
            started = acceptance_run_start(
                "suite", expected_tests=["T1"], name="demo", repo="service", session_id=session_id, paths=paths,
                test_plan=[{
                    "test_id": "T1", "objective": "Exact bounded contract",
                    "required_interfaces": ["session_runtime_status"],
                    "required_observations": ["corrective_actions_used"],
                    "pass_requirements": [{"field": "corrective_actions_used", "op": "gte", "value": 1}],
                    "evidence_scope": "current_acceptance_run", "min_evidence_refs": 1,
                    "allowed_native_tools": ["bash"], "native_tool_limits": {"bash": 1},
                    "forbidden_tool_classes": ["other_mcp"], "stop_after": True,
                }],
            )
            self.assertEqual(started["compaction_generation"], 0)
            first = acceptance_run_next(started["run_id"], session_id=session_id, paths=paths)
            self.assertEqual(first["required_interfaces"], ["session_runtime_status"])
            self.assertEqual(first["evidence_scope"], "current_acceptance_run")
            self.assertEqual(first["native_tool_limits"], {"bash": 1})
            compacted = opencode_events.mark_compacted(root, session_id)
            self.assertEqual(compacted["compaction_generation"], 1)
            state = acceptance_run_status(started["run_id"], session_id=session_id, paths=paths)
            self.assertEqual(state["compaction_generation"], 1)
            self.assertEqual(state["compaction_count"], 1)
            context = opencode_events.compaction_context(root, session_id, max_chars=12_000)["context"]
            self.assertIn("Awoki execution invariants", context)
            self.assertIn("native rg through Bash", context)
            self.assertIn("native-tool restrictions override normal investigation ergonomics", context)
            self.assertIn("Outside an active machine-enforced contract", context)
            self.assertIn("Current durable test contract", context)
            self.assertIn("corrective_actions_used", context)
            self.assertIn("current_acceptance_run", context)
            self.assertIn("Awoki current-session references", context)
            self.assertIn(started["run_id"], context)

    def test_machine_protocol_enforcement_downgrades_native_tool_deviation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            session_id = "protocol-session"
            project_open("demo", session_id=session_id, paths=paths)
            started = acceptance_run_start(
                "suite", expected_tests=["T1"], name="demo", repo="service", session_id=session_id, paths=paths,
                test_plan=[{
                    "test_id": "T1", "objective": "Use one bash only",
                    "allowed_native_tools": ["bash"], "native_tool_limits": {"bash": 1},
                }],
            )
            acceptance_runs.record_tool_event(root, session_id, tool="read", tool_class="native", phase="started")
            acceptance_runs.record_tool_event(root, session_id, tool="read", tool_class="native", phase="completed")
            recorded = acceptance_run_record(
                started["run_id"], "T1", "pass", name="demo", evidence={"observed": True}, session_id=session_id, paths=paths,
            )
            self.assertEqual(recorded["status"], "recorded")
            self.assertEqual(recorded["claimed_outcome"], "pass")
            self.assertEqual(recorded["outcome"], "protocol_deviation")
            self.assertIn("native_tool_not_allowed:read", recorded["protocol_violations"])
            state = acceptance_run_status(started["run_id"], name="demo", paths=paths)
            self.assertEqual(state["records"]["T1"]["outcome"], "protocol_deviation")

    def test_machine_pass_requirements_downgrade_unsatisfied_pass_to_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "suite", expected_tests=["T1"], name="demo", repo="service", paths=paths,
                test_plan=[{
                    "test_id": "T1", "objective": "Consume one corrective action",
                    "required_observations": ["corrective_actions_used"],
                    "pass_requirements": [{"field": "corrective_actions_used", "op": "gte", "value": 1}],
                }],
            )
            recorded = acceptance_run_record(
                started["run_id"], "T1", "pass", name="demo", evidence={"corrective_actions_used": 0}, paths=paths,
            )
            self.assertEqual(recorded["outcome"], "incomplete")
            self.assertTrue(any("pass_requirement_unmet" in row for row in recorded["incomplete_reasons"]))

    def test_current_run_evidence_provenance_is_enforced_without_changing_stable_ev_identity(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            first = acceptance_run_start("suite-a", expected_tests=["T1"], name="demo", repo="service", paths=paths)
            scope = acceptance_runs.scope_identity(dict(first["scope"]))
            stored_a = evidence_store.put(
                root, "demo", kind="test", tool="unit", payload={"ok": True}, scope_identity=scope, run_id=first["run_id"],
            )
            second = acceptance_run_start(
                "suite-b", expected_tests=["T1"], name="demo", repo="service", paths=paths,
                test_plan=[{"test_id": "T1", "objective": "Current run evidence", "evidence_scope": "current_acceptance_run", "min_evidence_refs": 1}],
            )
            bad = acceptance_run_record(second["run_id"], "T1", "pass", name="demo", evidence_refs=[stored_a["evidence_ref"]], paths=paths)
            self.assertEqual(bad["outcome"], "protocol_deviation")
            self.assertTrue(any("evidence_not_captured_in_current_run" in row for row in bad["protocol_violations"]))

            # A fresh run using the same exact payload keeps the content-addressed ev_ ID
            # while the capture sidecar records that the evidence was observed in this run.
            third = acceptance_run_start(
                "suite-c", expected_tests=["T1"], name="demo", repo="service", paths=paths,
                test_plan=[{"test_id": "T1", "objective": "Current run evidence", "evidence_scope": "current_acceptance_run", "min_evidence_refs": 1}],
            )
            stored_c = evidence_store.put(
                root, "demo", kind="test", tool="unit", payload={"ok": True}, scope_identity=scope, run_id=third["run_id"],
            )
            self.assertEqual(stored_c["evidence_ref"], stored_a["evidence_ref"])
            good = acceptance_run_record(third["run_id"], "T1", "pass", name="demo", evidence_refs=[stored_c["evidence_ref"]], paths=paths)
            self.assertEqual(good["outcome"], "pass")

    def test_orchestration_provenance_is_separate_and_required_scheduler_calls_can_pass(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            session_id = "orchestration-provenance-session"
            project_open("demo", session_id=session_id, paths=paths)
            started = acceptance_run_start(
                "suite", expected_tests=["T1"], name="demo", repo="service", session_id=session_id, paths=paths,
                test_plan=[{
                    "test_id": "T1", "objective": "Recover through scheduler",
                    # Backward-compatible form: acceptance_run_next is moved into
                    # the orchestration domain by contract sanitization.
                    "required_interfaces": ["acceptance_run_next", "session_runtime_status"],
                    "required_observations": ["recovered"],
                    "pass_requirements": [{"field": "recovered", "op": "eq", "value": True}],
                }],
            )
            step = acceptance_run_next(started["run_id"], session_id=session_id, paths=paths)
            self.assertEqual(step["required_interfaces"], ["session_runtime_status"])
            self.assertEqual(step["required_orchestration_interfaces"], ["acceptance_run_next"])
            orchestration = acceptance_runs.record_tool_event(
                root, session_id, tool="acceptance_run_next", tool_class="awoki_mcp", phase="completed"
            )
            execution = acceptance_runs.record_tool_event(
                root, session_id, tool="session_runtime_status", tool_class="awoki_mcp", phase="completed"
            )
            self.assertEqual(orchestration["provenance_domain"], "orchestration")
            self.assertEqual(execution["provenance_domain"], "execution")
            recorded = acceptance_run_record(
                started["run_id"], "T1", "pass", name="demo", evidence={"recovered": True},
                session_id=session_id, paths=paths,
            )
            self.assertEqual(recorded["outcome"], "pass")
            state = acceptance_run_status(started["run_id"], name="demo", paths=paths)
            self.assertIn("acceptance_run_next", state["orchestration_provenance"]["T1"]["invocations"])
            self.assertIn("session_runtime_status", state["execution_provenance"]["T1"]["invocations"])
            self.assertNotIn("acceptance_run_next", state["execution_provenance"]["T1"]["invocations"])

    def test_acceptance_control_tools_cannot_self_prove_current_test(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            session_id = "control-not-proof-session"
            project_open("demo", session_id=session_id, paths=paths)
            started = acceptance_run_start(
                "suite", expected_tests=["T1"], name="demo", repo="service", session_id=session_id, paths=paths,
                test_plan=[{"test_id": "T1", "objective": "No circular proof"}],
            )
            ignored = acceptance_runs.record_tool_event(
                root, session_id, tool="acceptance_run_record", tool_class="awoki_mcp", phase="completed"
            )
            self.assertEqual(ignored["status"], "ignored")
            self.assertEqual(ignored["reason"], "acceptance_control_tool_not_self_proving")
            state = acceptance_run_status(started["run_id"], name="demo", paths=paths)
            self.assertEqual(state["orchestration_provenance"], {})

    def test_compaction_history_retains_bounded_event_sequence(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            session_id = "compaction-history-session"
            project_open("demo", session_id=session_id, paths=paths)
            started = acceptance_run_start(
                "suite", expected_tests=["T1"], name="demo", repo="service", session_id=session_id, paths=paths,
            )
            for _ in range(3):
                opencode_events.mark_compacted(root, session_id)
            state = acceptance_run_status(started["run_id"], session_id=session_id, paths=paths)
            self.assertEqual(state["compaction_count"], 3)
            self.assertEqual([row["generation"] for row in state["compaction_events"]], [1, 2, 3])
            self.assertTrue(all(row["observed_at"] for row in state["compaction_events"]))
            runtime = session_runtime_status(session_id=session_id, paths=paths)
            self.assertEqual(len(runtime["acceptance_compaction"]["events"]), 3)


    def test_acceptance_attempt_history_is_immutable_and_referenceable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "suite", expected_tests=["T1"], name="demo", repo="service", paths=paths,
                test_plan=[{
                    "test_id": "T1", "objective": "Bookkeeping correction",
                    "required_observations": ["bound"],
                    "pass_requirements": [{"field": "bound", "op": "eq", "value": True}],
                }],
            )
            first = acceptance_run_record(
                started["run_id"], "T1", "pass", name="demo", evidence={"bound": False}, paths=paths,
            )
            self.assertEqual(first["outcome"], "incomplete")
            self.assertEqual(first["attempt_number"], 1)
            self.assertTrue(first["attempt_id"].startswith("aat_"))
            second = acceptance_run_record(
                started["run_id"], "T1", "pass", name="demo", evidence={"bound": True}, paths=paths,
            )
            self.assertEqual(second["outcome"], "pass")
            self.assertEqual(second["attempt_number"], 2)
            self.assertEqual(second["supersedes_attempt_id"], first["attempt_id"])
            state = acceptance_run_status(started["run_id"], name="demo", paths=paths)
            history = state["attempt_history"]["T1"]
            self.assertEqual([row["outcome"] for row in history], ["incomplete", "pass"])
            self.assertEqual(history[0]["attempt_id"], first["attempt_id"])
            self.assertIn("does not consume reliability corrective_budget", history[1]["correction_scope"])
            desc = reference_describe(first["attempt_id"], name="demo", paths=paths)
            self.assertEqual(desc["status"], "ok")
            self.assertEqual(desc["kind"], "acceptance_attempt")
            self.assertEqual(desc["origin"]["effective_outcome"], "incomplete")

    def test_acceptance_record_returns_bounded_prior_attempt_context_without_self_reference(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start(
                "suite", expected_tests=["T1"], name="demo", repo="service", paths=paths,
                test_plan=[{
                    "test_id": "T1", "objective": "Bookkeeping correction without self-reference",
                    "required_observations": ["bound"],
                    "pass_requirements": [{"field": "bound", "op": "eq", "value": True}],
                    "prior_attempt_requirements": [
                        {"field": "count", "op": "gte", "value": 1},
                        {"field": "last.effective_outcome", "op": "eq", "value": "incomplete"},
                    ],
                }],
            )
            first = acceptance_run_record(
                started["run_id"], "T1", "pass", name="demo", evidence={"bound": False}, paths=paths,
            )
            self.assertEqual(first["outcome"], "incomplete")
            self.assertTrue(any(row.startswith("prior_attempt_requirement_unmet:count") for row in first["incomplete_reasons"]))
            self.assertEqual(first["prior_attempt_count"], 0)
            self.assertEqual(first["prior_attempt_id"], "")
            self.assertEqual(first["prior_attempt_effective_outcome"], "")
            self.assertEqual(first["attempt_summary"]["effective_outcome"], "incomplete")

            second = acceptance_run_record(
                started["run_id"], "T1", "pass", name="demo", evidence={"bound": True}, paths=paths,
            )
            self.assertEqual(second["outcome"], "pass")
            self.assertEqual(second["incomplete_reasons"], [])
            self.assertEqual(second["prior_attempt_count"], 1)
            self.assertEqual(second["prior_attempt_id"], first["attempt_id"])
            self.assertEqual(second["prior_attempt_effective_outcome"], "incomplete")
            self.assertEqual(second["prior_attempt_claimed_outcome"], "pass")
            self.assertEqual(second["attempt_summary"]["supersedes_attempt_id"], first["attempt_id"])
            self.assertEqual(second["attempt_summary"]["prior_attempt_count"], 1)
            self.assertEqual(second["attempt_summary"]["prior_attempt_effective_outcome"], "incomplete")
            state = acceptance_run_status(started["run_id"], name="demo", paths=paths)
            self.assertEqual(state["test_plan"][0]["prior_attempt_requirements"][1]["field"], "last.effective_outcome")
            self.assertEqual(state["records"]["T1"]["protocol_evaluation"]["prior_attempt_context"]["last"]["effective_outcome"], "incomplete")

    def test_interface_invocation_limit_applies_to_awoki_mcp_not_only_native_tools(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            session_id = "interface-limit-session"
            project_open("demo", session_id=session_id, paths=paths)
            started = acceptance_run_start(
                "suite", expected_tests=["T1"], name="demo", repo="service", session_id=session_id, paths=paths,
                test_plan=[{"test_id": "T1", "objective": "One retrieval only", "interface_limits": {"codebase_search": 1}}],
            )
            for _ in range(2):
                acceptance_runs.record_tool_event(root, session_id, tool="codebase_search", tool_class="awoki_mcp", phase="started")
                acceptance_runs.record_tool_event(root, session_id, tool="codebase_search", tool_class="awoki_mcp", phase="completed")
            recorded = acceptance_run_record(
                started["run_id"], "T1", "pass", name="demo", evidence={"observed": True}, session_id=session_id, paths=paths,
            )
            self.assertEqual(recorded["outcome"], "protocol_deviation")
            self.assertIn("interface_limit_exceeded:codebase_search:2>1", recorded["protocol_violations"])
            step = acceptance_run_status(started["run_id"], name="demo", paths=paths)["test_plan"][0]
            self.assertEqual(step["interface_limits"], {"codebase_search": 1})

    def test_compaction_history_records_auto_vs_explicit_trigger_without_guessing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            session_id = "compaction-trigger-session"
            project_open("demo", session_id=session_id, paths=paths)
            started = acceptance_run_start("suite", expected_tests=["T1"], name="demo", repo="service", session_id=session_id, paths=paths)
            opencode_events.mark_compaction_trigger(root, session_id, trigger="automatic_context_pressure", source="compaction.auto")
            opencode_events.mark_compacted(root, session_id)
            opencode_events.mark_compaction_trigger(root, session_id, trigger="explicit_request", source="compaction.auto")
            opencode_events.mark_compacted(root, session_id)
            # Missing signal stays unknown instead of being inferred from timing.
            opencode_events.mark_compacted(root, session_id)
            state = acceptance_run_status(started["run_id"], session_id=session_id, paths=paths)
            self.assertEqual(
                [row["trigger"] for row in state["compaction_events"]],
                ["automatic_context_pressure", "explicit_request", "unknown"],
            )
            runtime = session_runtime_status(session_id=session_id, paths=paths)
            self.assertEqual(runtime["compaction"]["last_trigger"], "unknown")

    def test_oversized_acceptance_note_rejection_explains_where_rich_context_belongs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            self._repo(root, paths)
            started = acceptance_run_start("suite", expected_tests=["T1"], name="demo", repo="service", paths=paths)
            rejected = acceptance_run_record(
                started["run_id"], "T1", "pass", name="demo", notes="x" * 900, paths=paths,
            )
            self.assertEqual(rejected["status"], "rejected")
            self.assertTrue(any("800 characters or 4 newlines" in row for row in rejected["errors"]))
            self.assertIn("machine-checkable facts", rejected["guidance"])
            self.assertIn("ev_ evidence", rejected["guidance"])
            self.assertIn("labels/why_saved", rejected["guidance"])

    def test_harness_self_check_is_bounded_allowlisted_mcp_regression_runner(self):
        paths = HarnessPaths(root=Path(__file__).resolve().parents[2], global_root=Path("/tmp/awoki-test-global"))
        rejected = harness_self_check("arbitrary-shell", paths=paths)
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["available_checks"], ["compaction_acceptance_boundaries", "detached_self_resume_bounds", "reference_navigation_boundaries"])
        contract = harness_self_check("compaction_acceptance_boundaries", paths=paths)
        self.assertEqual(contract["status"], "passed")
        self.assertEqual(contract["test_count"], 15)
        self.assertEqual(contract["returncode"], 0)
        references = harness_self_check("reference_navigation_boundaries", paths=paths)
        self.assertEqual(references["status"], "passed")
        self.assertEqual(references["test_count"], 3)
        self.assertEqual(references["returncode"], 0)
        passed = harness_self_check("detached_self_resume_bounds", paths=paths)
        self.assertEqual(passed["status"], "passed")
        self.assertEqual(passed["test_count"], 3)
        self.assertEqual(passed["returncode"], 0)



class DurableContinuationTests(unittest.TestCase):
    def make_paths(self, root: Path) -> HarnessPaths:
        return HarnessPaths(root=root, global_root=root / "global")

    def _make_due(self, root: Path, session_id: str) -> None:
        path = project_workspace.session_state_path(root, session_id)
        state = json.loads(path.read_text(encoding="utf-8"))
        state["continuation"]["not_before"] = "2000-01-01T00:00:00Z"
        path.write_text(json.dumps(state), encoding="utf-8")

    def test_repository_readiness_can_schedule_explicit_project_while_session_unattached(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            result = project_continuation_schedule(
                workflow="repository-readiness", phase="vector_wait",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_demo", wait_seconds=30,
                name="demo", repo="repo1", resume_goal="review auth flow",
                session_id="unattached-session", paths=paths,
            )
            self.assertEqual(result["status"], "scheduled")
            cont = result["continuation"]
            self.assertEqual(cont["project_id"], "demo")
            self.assertEqual(cont["scope_kind"], "managed_project")
            self.assertEqual(cont["origin_project_id"], "")
            self.assertIsNone(project_workspace.current_project_id(root, session_id="unattached-session"))

    def test_detached_index_vector_continuation_refuses_true_ad_hoc_scope(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            for workflow, wait_tool, job_id in (
                ("repository-readiness", "code_vector_refresh_status", "cvr_demo"),
                ("generic", "code_index_refresh_status", "cir_future"),
            ):
                denied = project_continuation_schedule(
                    workflow=workflow, phase="wait", wait_tool=wait_tool,
                    wait_job_id=job_id, wait_seconds=30, session_id="adhoc", paths=paths,
                )
                self.assertEqual(denied["status"], "rejected")
                self.assertIn("managed project", denied["reason"])
                self.assertIn("true ad-hoc", denied["reason"])

    def test_repository_prepare_parent_is_valid_continuation_wait_tool(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            session_id = "parent-poll-session"
            scheduled = project_continuation_schedule(
                workflow="repository-readiness", phase="repository_prepare_wait",
                wait_tool="repository_prepare_status", wait_job_id="rpr_demo", wait_seconds=5,
                name="demo", repo="repo1", resume_goal="continue review",
                session_id=session_id, paths=paths,
            )
            self.assertEqual(scheduled["status"], "scheduled", scheduled)
            self._make_due(root, session_id)
            running = {
                "status": "ok", "job": {"status": "running"},
                "progress": {
                    "phase": "vector_refresh", "outcome": "PREPARATION_RUNNING",
                    "scope_type": "repository", "scope_id": "repo1", "mode": "full",
                    "child_kind": "vector", "child_job_id": "cvr_1",
                    "vectors_persisted": 3328, "vectors_remaining": 737,
                },
                "recommended_poll_after_seconds": 30,
            }
            with mock.patch.object(continuations, "_poll_job", return_value=running):
                polled = continuations.poll_due(root, session_id)
            self.assertEqual(polled["status"], "waiting")
            progress = polled["continuation"]["last_progress"]
            self.assertEqual(progress["child_kind"], "vector")
            self.assertEqual(progress["vectors_remaining"], 737)
            self._make_due(root, session_id)
            blocked = {
                "status": "ok", "job": {"status": "blocked"},
                "progress": {"phase": "blocked", "outcome": "PRECONDITION_FAILED", "reason": "embedding timeout"},
            }
            with mock.patch.object(continuations, "_poll_job", return_value=blocked):
                polled = continuations.poll_due(root, session_id)
            self.assertEqual(polled["status"], "ready")
            self.assertEqual(polled["continuation"]["last_job_status"], "blocked")

    def test_local_poll_reschedules_running_job_then_marks_terminal_ready(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            session_id = "poll-session"
            project_continuation_schedule(
                workflow="repository-readiness", phase="vector_wait",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_demo", wait_seconds=2,
                name="demo", repo="repo1", session_id=session_id, paths=paths,
            )
            self._make_due(root, session_id)
            running = {
                "status": "ok", "job": {"status": "running"},
                "progress": {"phase": "embedding", "chunks_total": 100, "chunks_ready": 55},
                "recommended_poll_after_seconds": 30,
            }
            with mock.patch.object(continuations, "_poll_job", return_value=running):
                result = continuations.poll_due(root, session_id)
            self.assertEqual(result["status"], "waiting")
            self.assertEqual(result["continuation"]["last_job_status"], "running")
            self.assertEqual(result["continuation"]["last_progress"]["chunks_ready"], 55)
            self._make_due(root, session_id)
            completed = {
                "status": "ok", "job": {"status": "completed"},
                "progress": {"phase": "completed", "chunks_total": 100, "chunks_ready": 100},
            }
            with mock.patch.object(continuations, "_poll_job", return_value=completed):
                result = continuations.poll_due(root, session_id)
            self.assertEqual(result["status"], "ready")
            claim = continuations.claim_due(root, session_id)
            self.assertEqual(claim["status"], "due")

    def test_continuation_is_preserved_across_project_attachment_and_scope_conflict_blocks_resume(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("alpha", paths=paths)
            project_create("beta", paths=paths)
            sid = "switch-session"
            project_continuation_schedule(
                workflow="repository-readiness", phase="vector_wait",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_alpha", wait_seconds=30,
                name="alpha", session_id=sid, paths=paths,
            )
            project_open("beta", session_id=sid, paths=paths)
            saved = project_continuation_status(session_id=sid, paths=paths)
            self.assertEqual(saved["continuation"]["project_id"], "alpha")
            # Force terminal-ready state without depending on an actual job.
            state_path = project_workspace.session_state_path(root, sid)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["continuation"]["status"] = "ready"
            state["continuation"]["not_before"] = ""
            state_path.write_text(json.dumps(state), encoding="utf-8")
            claim = continuations.claim_due(root, sid)
            self.assertEqual(claim["status"], "scope_conflict")
            self.assertEqual(claim["current_project_id"], "beta")

    def test_auto_resume_claims_are_bounded_and_deadline_is_enforced_at_claim_time(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            sid = "bounded-resume-session"
            project_continuation_schedule(
                workflow="repository-readiness", phase="vector_wait",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_demo", wait_seconds=30,
                name="demo", session_id=sid, paths=paths,
            )
            state_path = project_workspace.session_state_path(root, sid)

            # A continuation that becomes ready after its 48-hour lifetime must not
            # be claimed merely because the polling phase already ended.
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["continuation"]["status"] = "ready"
            state["continuation"]["not_before"] = ""
            state["continuation"]["deadline_at"] = "2000-01-01T00:00:00Z"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            expired = continuations.claim_due(root, sid)
            self.assertEqual(expired["status"], "blocked")
            self.assertEqual(expired["continuation"]["blocked_reason"], "continuation_deadline_exceeded")

            # A fresh generation may be claimed only MAX_AUTO_RESUME_ATTEMPTS times.
            project_continuation_schedule(
                workflow="repository-readiness", phase="vector_wait",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_demo2", wait_seconds=30,
                name="demo", session_id=sid, paths=paths,
            )
            for expected_attempt in range(1, continuations.MAX_AUTO_RESUME_ATTEMPTS + 1):
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["continuation"]["status"] = "ready"
                state["continuation"]["not_before"] = ""
                state["continuation"]["lease_until"] = ""
                state_path.write_text(json.dumps(state), encoding="utf-8")
                claimed = continuations.claim_due(root, sid)
                self.assertEqual(claimed["status"], "due")
                self.assertEqual(claimed["continuation"]["attempts"], expected_attempt)

            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["continuation"]["status"] = "ready"
            state["continuation"]["not_before"] = ""
            state["continuation"]["lease_until"] = ""
            state_path.write_text(json.dumps(state), encoding="utf-8")
            blocked = continuations.claim_due(root, sid)
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["continuation"]["blocked_reason"], "auto_resume_attempt_limit")
            self.assertEqual(blocked["continuation"]["attempts"], continuations.MAX_AUTO_RESUME_ATTEMPTS)

    def test_rescheduling_active_continuation_preserves_chain_budget_but_terminal_restart_resets_it(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            sid = "continuation-chain-session"
            project_continuation_schedule(
                workflow="repository-readiness", phase="wait-a",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_a", wait_seconds=30,
                name="demo", session_id=sid, paths=paths,
            )
            state_path = project_workspace.session_state_path(root, sid)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["continuation"]["status"] = "ready"
            state["continuation"]["not_before"] = ""
            state_path.write_text(json.dumps(state), encoding="utf-8")
            first = continuations.claim_due(root, sid)
            self.assertEqual(first["continuation"]["attempts"], 1)
            original_deadline = first["continuation"]["deadline_at"]

            # Advancing the same live workflow is the same bounded recovery chain.
            advanced = project_continuation_schedule(
                workflow="repository-readiness", phase="wait-b",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_b", wait_seconds=30,
                name="demo", session_id=sid, paths=paths,
            )
            self.assertEqual(advanced["continuation"]["attempts"], 1)
            self.assertEqual(advanced["continuation"]["deadline_at"], original_deadline)

            project_continuation_finalize(reason="done", session_id=sid, paths=paths)
            restarted = project_continuation_schedule(
                workflow="repository-readiness", phase="wait-c",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_c", wait_seconds=30,
                name="demo", session_id=sid, paths=paths,
            )
            self.assertEqual(restarted["continuation"]["attempts"], 0)
            self.assertNotEqual(restarted["continuation"]["continuation_id"], advanced["continuation"]["continuation_id"])

    def test_rescheduling_expired_active_chain_does_not_refresh_lifetime(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            sid = "expired-chain-reschedule-session"
            project_continuation_schedule(
                workflow="repository-readiness", phase="wait-a",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_a", wait_seconds=30,
                name="demo", session_id=sid, paths=paths,
            )
            state_path = project_workspace.session_state_path(root, sid)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["continuation"]["deadline_at"] = "2000-01-01T00:00:00Z"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            advanced = project_continuation_schedule(
                workflow="repository-readiness", phase="wait-b",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_b", wait_seconds=30,
                name="demo", session_id=sid, paths=paths,
            )
            self.assertEqual(advanced["continuation"]["deadline_at"], "2000-01-01T00:00:00Z")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["continuation"]["status"] = "ready"
            state["continuation"]["not_before"] = ""
            state_path.write_text(json.dumps(state), encoding="utf-8")
            blocked = continuations.claim_due(root, sid)
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["continuation"]["blocked_reason"], "continuation_deadline_exceeded")

    def test_expired_claim_lease_becomes_ready_and_claimable_again(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            sid = "lease-recovery-session"
            project_continuation_schedule(
                workflow="repository-readiness", phase="vector_wait",
                wait_tool="code_vector_refresh_status", wait_job_id="cvr_demo", wait_seconds=30,
                name="demo", session_id=sid, paths=paths,
            )
            state_path = project_workspace.session_state_path(root, sid)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["continuation"]["status"] = "claimed"
            state["continuation"]["lease_until"] = "2000-01-01T00:00:00Z"
            state["continuation"]["claimed_at"] = "2000-01-01T00:00:00Z"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            claim = continuations.claim_due(root, sid)
            self.assertEqual(claim["status"], "due")
            self.assertEqual(claim["continuation"]["status"], "claimed")
            self.assertNotEqual(claim["continuation"]["lease_until"], "2000-01-01T00:00:00Z")

    def test_continuation_mcp_wrappers_finalize_and_cancel_without_worker_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = self.make_paths(root)
            project_create("demo", paths=paths)
            sid = "finalize-session"
            project_continuation_schedule(
                workflow="repository-readiness", phase="index_wait",
                wait_tool="code_index_refresh_status", wait_job_id="cir_demo", wait_seconds=30,
                name="demo", session_id=sid, paths=paths,
            )
            finalized = project_continuation_finalize(reason="FULL_READY", session_id=sid, paths=paths)
            self.assertEqual(finalized["status"], "finalized")
            self.assertEqual(finalized["continuation"]["status"], "done")
            # A later new generation may be cancelled independently.
            project_continuation_schedule(
                workflow="repository-readiness", phase="index_wait",
                wait_tool="code_index_refresh_status", wait_job_id="cir_demo2", wait_seconds=30,
                name="demo", session_id=sid, paths=paths,
            )
            cancelled = project_continuation_cancel(reason="user changed direction", session_id=sid, paths=paths)
            self.assertEqual(cancelled["status"], "cancelled")



if __name__ == "__main__":
    unittest.main()
