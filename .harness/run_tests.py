#!/usr/bin/env python3
"""Run the Awoki unittest suite in isolated parallel shards.

The canonical release command remains ``python -m unittest discover``. This
runner exists for ``make validate`` so static validation plus the complete suite
fits constrained CI command wall-time limits without weakening test coverage.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / ".harness" / "tests"


def _flatten(suite: unittest.TestSuite) -> Iterable[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _test_ids() -> list[str]:
    sys.path.insert(0, str(TEST_DIR))
    suite = unittest.defaultTestLoader.discover(str(TEST_DIR), pattern="test_*.py")
    ids = sorted(test.id() for test in _flatten(suite))
    if not ids:
        raise RuntimeError("no tests discovered")
    return ids


def _shards(ids: list[str], count: int) -> list[list[str]]:
    buckets: list[list[str]] = [[] for _ in range(max(1, min(count, len(ids))))]
    # Round-robin avoids putting all slower continuity tests in one shard.
    for index, test_id in enumerate(ids):
        buckets[index % len(buckets)].append(test_id)
    return [bucket for bucket in buckets if bucket]


def _run_shard(index: int, ids: list[str]) -> tuple[int, int, str, str]:
    env = os.environ.copy()
    pythonpath = [str(ROOT / ".harness"), str(TEST_DIR)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    completed = subprocess.run(
        [sys.executable, "-m", "unittest", *ids],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return index, completed.returncode, completed.stdout, completed.stderr


def main() -> int:
    ids = _test_ids()
    worker_count = max(1, min(int(os.environ.get("AWOKI_TEST_WORKERS", "4")), 8, len(ids)))
    shards = _shards(ids, worker_count)
    failures: list[int] = []
    results: dict[int, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=len(shards)) as executor:
        futures = {
            executor.submit(_run_shard, index, shard): index
            for index, shard in enumerate(shards, start=1)
        }
        for future in as_completed(futures):
            index, returncode, stdout, stderr = future.result()
            results[index] = (stdout, stderr)
            if returncode != 0:
                failures.append(index)
    for index in sorted(results):
        stdout, stderr = results[index]
        print(f"--- unittest shard {index}/{len(shards)} ---")
        if stdout.strip():
            print(stdout.rstrip())
        if stderr.strip():
            print(stderr.rstrip(), file=sys.stderr)
    print(f"parallel unittest validation: {len(ids)} tests across {len(shards)} isolated shards")
    if failures:
        print(f"failed shards: {', '.join(map(str, sorted(failures)))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
