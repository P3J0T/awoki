#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import project_workspace as pw
import continuity_migration


def root() -> Path:
    return Path(os.environ.get("AWOKI_ROOT", ".")).resolve()


def print_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True))


def main() -> None:
    p = argparse.ArgumentParser(description="Awoki project workspace control")
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ["create", "resume", "status", "handoff"]:
        sp = sub.add_parser(cmd)
        sp.add_argument("name")
    sp = sub.add_parser("list")
    sp = sub.add_parser("note")
    sp.add_argument("name")
    sp.add_argument("text")
    sp = sub.add_parser("pending")
    sp.add_argument("name")
    sp.add_argument("title")
    sp.add_argument("next_action")
    sp.add_argument("--reason", default="")
    sp = sub.add_parser("mark-pending")
    sp.add_argument("name")
    sp.add_argument("--pending-id", default="")
    sp.add_argument("--status", default="done")
    sp.add_argument("--note", default="")
    sp = sub.add_parser("capture")
    sp.add_argument("name")
    sp.add_argument("summary")
    sp.add_argument("--details", default="")
    sp.add_argument("--kind", default="observation")
    sp.add_argument("--confidence", default="medium")
    sp = sub.add_parser("pause")
    sp.add_argument("name")
    sp.add_argument("--summary", default="")
    sp.add_argument("--details", default="")
    sp.add_argument("--likely-continuation", default="")
    sp = sub.add_parser("repo-add")
    sp.add_argument("name")
    sp.add_argument("repo_id")
    sp.add_argument("path")
    sp.add_argument("--default", action="store_true")
    sp = sub.add_parser("repo-remove")
    sp.add_argument("name")
    sp.add_argument("repo_id")
    sp = sub.add_parser("repo-default")
    sp.add_argument("name")
    sp.add_argument("repo_id")
    sp = sub.add_parser("repo-list")
    sp.add_argument("name")
    sp = sub.add_parser("migrate")
    sp.add_argument("name")
    sp.add_argument("--apply", action="store_true")
    args = p.parse_args()
    r = root()
    if args.cmd == "create":
        print_json(pw.project_create(r, args.name))
    elif args.cmd == "resume":
        print_json(pw.project_resume(r, args.name))
    elif args.cmd == "status":
        print_json(pw.project_status(r, args.name))
    elif args.cmd == "handoff":
        print_json(pw.project_handoff(r, args.name))
    elif args.cmd == "list":
        print_json(pw.project_list(r))
    elif args.cmd == "note":
        print_json(pw.project_note(r, args.name, args.text))
    elif args.cmd == "pending":
        print_json(pw.project_pending(r, args.name, args.title, args.next_action, reason=args.reason))
    elif args.cmd == "mark-pending":
        print_json(pw.project_mark_pending(r, args.name, pending_id=args.pending_id, status=args.status, note=args.note))
    elif args.cmd == "capture":
        print_json(pw.project_capture(r, args.name, args.summary, details=args.details, kind=args.kind, confidence=args.confidence))
    elif args.cmd == "pause":
        print_json(pw.project_pause(r, args.name, summary=args.summary, details=args.details, likely_continuation=args.likely_continuation))
    elif args.cmd == "repo-add":
        print_json(pw.project_repo_add(r, args.name, args.repo_id, args.path, default=args.default))
    elif args.cmd == "repo-remove":
        print_json(pw.project_repo_remove(r, args.name, args.repo_id))
    elif args.cmd == "repo-default":
        print_json(pw.project_repo_default(r, args.name, args.repo_id))
    elif args.cmd == "repo-list":
        rows = pw.project_repositories(r, args.name, enabled_only=False)
        print_json({"status": "ok", "project_id": pw.clean_project_id(args.name), "registry": pw.project_repository_registry(r, args.name), "repositories": [{k: v for k, v in row.items() if k != "root"} for row in rows]})
    elif args.cmd == "migrate":
        print_json(continuity_migration.migrate(r, args.name, apply=args.apply))


if __name__ == "__main__":
    main()
