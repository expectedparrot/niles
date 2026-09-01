from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .envelope import Envelope, NextStep, error, ok
from .store import NilesError, Project


def parse_key_values(values: list[str] | None) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for value in values or []:
        if "=" not in value:
            raise NilesError("invalid_key_value", f"Expected key=value, got '{value}'.")
        key, raw = value.split("=", 1)
        parsed[key] = coerce_scalar(raw)
    return parsed


def coerce_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="niles")
    parser.add_argument("--version", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init")
    sub.add_parser("status")

    contact = sub.add_parser("contact")
    contact_sub = contact.add_subparsers(dest="contact_command", required=True)
    contact_add = contact_sub.add_parser("add")
    contact_add.add_argument("name")
    contact_add.add_argument("--email", action="append", default=[])
    contact_add.add_argument("--phone", action="append", default=[])
    contact_add.add_argument("--company")
    contact_add.add_argument("--role")
    contact_add.add_argument("--trait", action="append", default=[])
    contact_add.add_argument("--tag", action="append", default=[])
    contact_add.add_argument("--cadence-days", type=int)
    contact_show = contact_sub.add_parser("show")
    contact_show.add_argument("ref")
    contact_list = contact_sub.add_parser("list")
    contact_list.add_argument("--tag")
    contact_list.add_argument("--stale", action="store_true")

    note = sub.add_parser("note")
    note_sub = note.add_subparsers(dest="note_command", required=True)
    note_add = note_sub.add_parser("add")
    note_add.add_argument("ref")
    note_add.add_argument("text")
    note_add.add_argument("--kind", default="note", choices=["note", "call", "meeting", "email", "intake", "debrief"])
    note_add.add_argument("--at")
    note_add.add_argument("--debrief", action="store_true")

    task = sub.add_parser("task")
    task_sub = task.add_subparsers(dest="task_command", required=True)
    task_add = task_sub.add_parser("add")
    task_add.add_argument("ref")
    task_add.add_argument("text")
    task_add.add_argument("--due")
    task_add.add_argument("--assign")
    task_add.add_argument("--tag", action="append", default=[])
    task_list = task_sub.add_parser("list")
    task_list.add_argument("--due")
    task_list.add_argument("--assignee")
    task_list.add_argument("--status", default="open")
    task_done = task_sub.add_parser("done")
    task_done.add_argument("id")
    task_done.add_argument("--note")

    sub.add_parser("version")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        envelope = dispatch(args, argv)
        sys.stdout.write(envelope.to_json())
        return 0 if envelope.status == "ok" else 1
    except NilesError as exc:
        command = command_name(args)
        sys.stdout.write(error(command, argv, exc.code, exc.message, exc.data).to_json())
        return 1


def dispatch(args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.version or args.command == "version":
        return ok("version", argv, {"version": __version__})
    if args.command == "init":
        project = Project.init(Path.cwd())
        return ok(
            "init",
            argv,
            {
                "project_root": str(project.root),
                "state_dir": ".niles",
                "created": [".niles/config.toml", ".niles/events/", ".niles/index/niles.sqlite", ".niles/surveys/"],
            },
            next_steps=[
                NextStep(
                    label="Add a contact",
                    command='niles contact add "Jane Doe" --email jane@example.com',
                    mutates=True,
                )
            ],
        )

    project = Project.open(Path.cwd())
    if args.command == "status":
        data = project.counts()
        return ok(
            "status",
            argv,
            data,
            next_steps=[
                NextStep(
                    label="List open tasks",
                    command="niles task list --status open",
                    mutates=False,
                )
            ],
        )
    if args.command == "contact":
        return dispatch_contact(project, args, argv)
    if args.command == "note":
        return dispatch_note(project, args, argv)
    if args.command == "task":
        return dispatch_task(project, args, argv)
    raise NilesError("unknown_command", "Unknown command.")


def dispatch_contact(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.contact_command == "add":
        data = project.add_contact(
            name=args.name,
            emails=args.email,
            phones=args.phone,
            company=args.company,
            role=args.role,
            traits=parse_key_values(args.trait),
            tags=args.tag,
            cadence_days=args.cadence_days,
        )
        return ok(
            "contact add",
            argv,
            data,
            next_steps=[
                NextStep(
                    label="Add a note",
                    command=f'niles note add {data["contact"]["slug"]} "Intro call" --kind call',
                    mutates=True,
                )
            ],
        )
    if args.contact_command == "show":
        return ok("contact show", argv, {"contact": project.resolve_contact(args.ref)})
    if args.contact_command == "list":
        return ok(
            "contact list",
            argv,
            {"contacts": project.list_contacts(tag=args.tag, stale=args.stale)},
        )
    raise NilesError("unknown_command", "Unknown contact command.")


def dispatch_note(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.note_command == "add":
        data = project.add_note(args.ref, args.text, args.kind, args.at)
        next_steps = [
            NextStep(
                label="Create a task",
                command=f'niles task add {data["contact"]["slug"]} "Follow up" --due 2026-09-05',
                mutates=True,
            )
        ]
        if args.debrief:
            data["debrief"] = {"status": "not_implemented", "message": "Survey routing will be implemented in M3."}
        return ok("note add", argv, data, next_steps=next_steps)
    raise NilesError("unknown_command", "Unknown note command.")


def dispatch_task(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.task_command == "add":
        data = project.add_task(args.ref, args.text, args.due, args.assign, args.tag)
        return ok("task add", argv, data)
    if args.task_command == "list":
        data = project.list_tasks(status=args.status, assignee=args.assignee, due=args.due)
        return ok("task list", argv, {"tasks": data})
    if args.task_command == "done":
        data = project.done_task(args.id, args.note)
        return ok("task done", argv, data)
    raise NilesError("unknown_command", "Unknown task command.")


def command_name(args: argparse.Namespace) -> str:
    parts = []
    if getattr(args, "command", None):
        parts.append(args.command)
    for attr in ("contact_command", "note_command", "task_command"):
        value = getattr(args, attr, None)
        if value:
            parts.append(value)
    return " ".join(parts) or "niles"


if __name__ == "__main__":
    raise SystemExit(main())
