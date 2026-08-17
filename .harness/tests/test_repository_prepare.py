from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import project_workspace
import repository_prepare_jobs
from harness_core import HarnessPaths, repository_prepare_start


class RepositoryPrepareTests(unittest.TestCase):
    def fixture(self, root: Path, project_id: str = "demo", repo_id: str = "service") -> None:
        project_workspace.project_create(root, project_id, session_id="fixture-session")
        pp = project_workspace.paths_for(root, project_id)
        repo = pp.project_dir / "repo" / repo_id
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "main.py").write_text("def entry():\n    return True\n", encoding="utf-8")
        project_workspace.project_repo_add(root, project_id, repo_id, f"repo/{repo_id}", default=True)

    def seed_parent(self, root: Path, *, mode: str = "full", project_id: str = "demo", repo_id: str = "service") -> str:
        job_id = "rpr_fixture"
        now = repository_prepare_jobs._now()
        repository_prepare_jobs._write_json(
            repository_prepare_jobs._state_path(root, project_id, job_id),
            {
                "schema_version": 1,
                "job_id": job_id,
                "project_id": project_id,
                "scope_type": "repository",
                "scope_id": repo_id,
                "scope_key": f"repository:{repo_id}|mode:{mode}",
                "mode": mode,
                "semantic_authorized": mode == "full",
                "resume_goal": "continue review",
                "origin_session_id": "",
                "status": "running",
                "outcome": "PREPARATION_RUNNING",
                "phase": "planning",
                "pid": 99999,
                "created_at": now,
                "started_at": now,
                "updated_at": now,
                "finished_at": "",
                "reason": "",
                "child": {},
                "readiness": {},
            },
        )
        return job_id

    def test_explicit_existing_project_can_start_while_session_unattached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            paths = HarnessPaths(root=root, global_root=root / "global")
            fake_proc = mock.Mock(pid=43120)
            ready = {
                "embedding": {"configuration_ready": True},
                "rerank": {"enabled": True, "configuration_ready": True},
            }
            with mock.patch.object(repository_prepare_jobs.rag_backend, "retrieval_status_snapshot", return_value=ready), \
                 mock.patch.object(repository_prepare_jobs.subprocess, "Popen", return_value=fake_proc):
                started = repository_prepare_start(name="demo", repo="service", mode="full", session_id="unattached-session", paths=paths)
            self.assertEqual(started["status"], "started", started)
            self.assertEqual(started["job"]["project_id"], "demo")
            self.assertEqual(started["job"]["scope_id"], "service")
            self.assertEqual(started["job"]["pid"], 43120)

    def test_missing_managed_scope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = HarnessPaths(root=root, global_root=root / "global")
            result = repository_prepare_start(repo="service", mode="local", session_id="ad-hoc", paths=paths)
            self.assertEqual(result["outcome"], "MANAGED_SCOPE_REQUIRED", result)
            self.assertEqual(result["status"], "managed_scope_required")

    def test_full_configuration_blocker_prevents_worker_start(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            blocked = {
                "embedding": {"configuration_ready": False},
                "rerank": {"enabled": False, "configuration_ready": False},
            }
            with mock.patch.object(repository_prepare_jobs.rag_backend, "retrieval_status_snapshot", return_value=blocked), \
                 mock.patch.object(repository_prepare_jobs.subprocess, "Popen") as popen:
                result = repository_prepare_jobs.start(root, "demo", repo="service", mode="full")
            self.assertEqual(result["outcome"], "CONFIGURATION_BLOCKED", result)
            self.assertIn("embedding_configuration_not_ready", result["blockers"])
            popen.assert_not_called()

    def test_local_parent_stops_after_structural_verification(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            job_id = self.seed_parent(root, mode="local")
            passive = {"freshness": {"lexical_current": True, "vector_current": False}}
            verified = {"freshness": {"lexical_current": True, "vector_current": False}, "verification": "ok"}
            with mock.patch.object(repository_prepare_jobs.code_search, "index_status", side_effect=[passive, verified]), \
                 mock.patch.object(repository_prepare_jobs.code_vector_jobs, "start") as vector_start:
                rc = repository_prepare_jobs._worker(root, "demo", job_id)
            self.assertEqual(rc, 0)
            state = repository_prepare_jobs._read_json(repository_prepare_jobs._state_path(root, "demo", job_id))
            self.assertEqual(state["outcome"], "LOCAL_READY")
            self.assertEqual(state["status"], "completed")
            vector_start.assert_not_called()

    def test_full_parent_owns_structural_and_vector_children_then_reaches_full_ready(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            job_id = self.seed_parent(root, mode="full")
            index_results = [
                {"freshness": {"lexical_current": False, "vector_current": False}},
                {"freshness": {"lexical_current": True, "vector_current": False}},
                {"freshness": {"lexical_current": True, "vector_current": False}},
                {"freshness": {"lexical_current": True, "vector_current": True}, "verification": "full"},
            ]
            ready = {
                "embedding": {"configuration_ready": True},
                "rerank": {"enabled": True, "configuration_ready": True},
            }
            probe = {
                "qdrant": {"available": True},
                "embedding": {"status": "ok"},
                "rerank": {"status": "ok"},
            }
            with mock.patch.object(repository_prepare_jobs.code_search, "index_status", side_effect=index_results), \
                 mock.patch.object(repository_prepare_jobs.code_index_jobs, "start", return_value={"status": "started", "job": {"job_id": "cir_1", "status": "running"}, "progress": {}}) as index_start, \
                 mock.patch.object(repository_prepare_jobs.code_vector_jobs, "start", return_value={"status": "started", "job": {"job_id": "cvr_1", "status": "running"}, "progress": {}}) as vector_start, \
                 mock.patch.object(repository_prepare_jobs, "_wait_child", side_effect=[{"job": {"status": "completed"}, "progress": {}}, {"job": {"status": "completed"}, "progress": {}}]), \
                 mock.patch.object(repository_prepare_jobs.rag_backend, "retrieval_status_snapshot", return_value=ready), \
                 mock.patch.object(repository_prepare_jobs.rag_backend, "probe_retrieval", return_value=probe):
                rc = repository_prepare_jobs._worker(root, "demo", job_id)
            self.assertEqual(rc, 0)
            index_start.assert_called_once_with(root, "demo", repo="service")
            vector_start.assert_called_once_with(root, "demo", repo="service")
            state = repository_prepare_jobs._read_json(repository_prepare_jobs._state_path(root, "demo", job_id))
            self.assertEqual(state["outcome"], "FULL_READY")
            self.assertEqual(state["status"], "completed")

    def test_vector_child_failure_blocks_without_whole_job_restart(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            job_id = self.seed_parent(root, mode="full")
            ready = {
                "embedding": {"configuration_ready": True},
                "rerank": {"enabled": True, "configuration_ready": True},
            }
            index_results = [
                {"freshness": {"lexical_current": True, "vector_current": False}},
                {"freshness": {"lexical_current": True, "vector_current": False}},
                {"freshness": {"lexical_current": True, "vector_current": False}},
            ]
            failed = {
                "job": {"job_id": "cvr_timeout", "status": "failed", "reason": "Request timed out."},
                "progress": {"vectors_persisted": 3328, "vectors_remaining": 737, "reason": "Request timed out."},
            }
            with mock.patch.object(repository_prepare_jobs.code_search, "index_status", side_effect=index_results), \
                 mock.patch.object(repository_prepare_jobs.code_vector_jobs, "start", return_value={"status": "started", "job": {"job_id": "cvr_timeout", "status": "running"}, "progress": {}}) as vector_start, \
                 mock.patch.object(repository_prepare_jobs, "_wait_child", return_value=failed), \
                 mock.patch.object(repository_prepare_jobs.rag_backend, "retrieval_status_snapshot", return_value=ready):
                rc = repository_prepare_jobs._worker(root, "demo", job_id)
            self.assertEqual(rc, 2)
            self.assertEqual(vector_start.call_count, 1)
            state = repository_prepare_jobs._read_json(repository_prepare_jobs._state_path(root, "demo", job_id))
            self.assertEqual(state["status"], "blocked")
            self.assertEqual(state["outcome"], "PRECONDITION_FAILED")
            self.assertIn("Request timed out", state["reason"])

    def test_duplicate_active_parent_is_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            ready = {
                "embedding": {"configuration_ready": True},
                "rerank": {"enabled": True, "configuration_ready": True},
            }
            fake_proc = mock.Mock(pid=10001)
            with mock.patch.object(repository_prepare_jobs.rag_backend, "retrieval_status_snapshot", return_value=ready), \
                 mock.patch.object(repository_prepare_jobs.subprocess, "Popen", return_value=fake_proc), \
                 mock.patch.object(repository_prepare_jobs, "_pid_alive", return_value=True):
                first = repository_prepare_jobs.start(root, "demo", repo="service", mode="full")
                second = repository_prepare_jobs.start(root, "demo", repo="service", mode="full")
            self.assertEqual(first["status"], "started")
            self.assertEqual(second["status"], "already_running", second)
            self.assertEqual(second["job"]["job_id"], first["job"]["job_id"])

    def test_cancel_parent_cancels_active_vector_child(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.fixture(root)
            job_id = self.seed_parent(root, mode="full")
            path = repository_prepare_jobs._state_path(root, "demo", job_id)
            state = repository_prepare_jobs._read_json(path)
            state["child"] = {"kind": "vector", "job_id": "cvr_active", "status": "running", "progress": {}}
            repository_prepare_jobs._write_json(path, state)
            with mock.patch.object(repository_prepare_jobs, "_pid_alive", return_value=True), \
                 mock.patch.object(repository_prepare_jobs.code_vector_jobs, "cancel") as child_cancel, \
                 mock.patch.object(repository_prepare_jobs.os, "kill") as kill:
                result = repository_prepare_jobs.cancel(root, "demo", job_id=job_id)
            self.assertEqual(result["status"], "cancelled")
            child_cancel.assert_called_once_with(root, "demo", job_id="cvr_active")
            kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
