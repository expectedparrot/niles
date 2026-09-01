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
    export_parser = sub.add_parser("export")
    export_parser.add_argument("archive")
    import_parser = sub.add_parser("import")
    import_parser.add_argument("archive")
    import_parser.add_argument("--replace", action="store_true")

    agent = sub.add_parser("agent")
    agent_sub = agent.add_subparsers(dest="agent_command", required=True)
    agent_sub.add_parser("next")

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
    contact_show.add_argument("--with-notes", action="store_true")
    contact_show.add_argument("--with-tasks", action="store_true")
    contact_list = contact_sub.add_parser("list")
    contact_list.add_argument("--tag")
    contact_list.add_argument("--stale", action="store_true")
    contact_update = contact_sub.add_parser("update")
    contact_update.add_argument("ref")
    contact_update.add_argument("--name")
    contact_update.add_argument("--company")
    contact_update.add_argument("--role")
    contact_update.add_argument("--cadence-days", type=int)
    contact_update.add_argument("--trait", action="append", default=[])
    contact_update.add_argument("--email", action="append", default=[])
    contact_update.add_argument("--phone", action="append", default=[])
    contact_tag = contact_sub.add_parser("tag")
    contact_tag.add_argument("ref")
    contact_tag.add_argument("--add", action="append", default=[])
    contact_tag.add_argument("--remove", action="append", default=[])
    contact_archive = contact_sub.add_parser("archive")
    contact_archive.add_argument("ref")
    contact_archive.add_argument("--reason")
    contact_merge = contact_sub.add_parser("merge")
    contact_merge.add_argument("keep")
    contact_merge.add_argument("duplicate")
    contact_merge.add_argument("--note")

    note = sub.add_parser("note")
    note_sub = note.add_subparsers(dest="note_command", required=True)
    note_add = note_sub.add_parser("add")
    note_add.add_argument("ref")
    note_add.add_argument("text")
    note_add.add_argument(
        "--kind",
        default="note",
        choices=["note", "call", "meeting", "email", "intake", "debrief", "enrichment"],
    )
    note_add.add_argument("--at")
    note_add.add_argument("--debrief", action="store_true")
    note_list = note_sub.add_parser("list")
    note_list.add_argument("ref", nargs="?")
    note_list.add_argument("--limit", type=int)

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
    task_list.add_argument("--contact")
    task_done = task_sub.add_parser("done")
    task_done.add_argument("id")
    task_done.add_argument("--note")
    task_update = task_sub.add_parser("update")
    task_update.add_argument("id")
    task_update.add_argument("--text")
    task_update.add_argument("--due")
    task_update.add_argument("--assign")
    task_update.add_argument("--status", choices=["open", "done", "blocked", "cancelled"])
    task_update.add_argument("--tag", action="append", default=[])
    task_update.add_argument("--remove-tag", action="append", default=[])
    task_reassign = task_sub.add_parser("reassign")
    task_reassign.add_argument("id")
    task_reassign.add_argument("assignee")
    task_cancel = task_sub.add_parser("cancel")
    task_cancel.add_argument("id")
    task_cancel.add_argument("--note")
    task_suggest = task_sub.add_parser("suggest")
    task_suggest.add_argument("--assignee")

    org = sub.add_parser("org")
    org_sub = org.add_subparsers(dest="org_command", required=True)
    org_context = org_sub.add_parser("context")
    org_context_sub = org_context.add_subparsers(dest="org_context_command", required=True)
    org_context_set = org_context_sub.add_parser("set")
    org_context_set.add_argument("context", nargs="?")
    org_context_set.add_argument("--name")
    org_context_set.add_argument("--trait", action="append", default=[])
    org_context_sub.add_parser("show")

    material = sub.add_parser("material")
    material_sub = material.add_subparsers(dest="material_command", required=True)
    material_add = material_sub.add_parser("add")
    material_add.add_argument("title")
    material_add.add_argument("--path")
    material_add.add_argument("--url")
    material_add.add_argument("--description")
    material_add.add_argument("--tag", action="append", default=[])
    material_list = material_sub.add_parser("list")
    material_list.add_argument("--tag")

    report = sub.add_parser("report")
    report_sub = report.add_subparsers(dest="report_command", required=True)
    report_status = report_sub.add_parser("status")
    report_status.add_argument("--html", required=True)

    enrich = sub.add_parser("enrich")
    enrich_sub = enrich.add_subparsers(dest="enrich_command", required=True)
    enrich_ingest = enrich_sub.add_parser("ingest")
    enrich_ingest.add_argument("ref")
    enrich_ingest.add_argument("text")
    enrich_ingest.add_argument("--source-url")
    enrich_ingest.add_argument("--confidence", type=float)

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
    if args.command == "agent":
        return dispatch_agent(args, argv)
    if args.command == "import":
        data = Project.import_archive(Path.cwd(), Path(args.archive), replace=args.replace)
        return ok(
            "import",
            argv,
            data,
            next_steps=[
                NextStep(
                    label="Ask what to do next",
                    command="niles agent next",
                    mutates=False,
                )
            ],
        )

    project = Project.open(Path.cwd())
    if args.command == "export":
        data = project.export_archive(Path(args.archive))
        return ok(
            "export",
            argv,
            data,
            next_steps=[
                NextStep(
                    label="Import into another directory",
                    command=f'niles import {data["archive"]}',
                    mutates=True,
                )
            ],
        )
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
    if args.command == "org":
        return dispatch_org(project, args, argv)
    if args.command == "material":
        return dispatch_material(project, args, argv)
    if args.command == "report":
        return dispatch_report(project, args, argv)
    if args.command == "enrich":
        return dispatch_enrich(project, args, argv)
    raise NilesError("unknown_command", "Unknown command.")


def dispatch_agent(args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.agent_command != "next":
        raise NilesError("unknown_command", "Unknown agent command.")

    try:
        project = Project.open(Path.cwd())
        initialized = True
        project_root = str(project.root)
        counts = project.counts()
        next_steps = agent_steps_for_counts(counts)
    except NilesError as exc:
        if exc.code != "not_initialized":
            raise
        initialized = False
        project_root = None
        counts = None
        next_steps = [
            NextStep(
                label="Initialize a CRM project",
                command="niles init",
                mutates=True,
            )
        ]

    return ok(
        "agent next",
        argv,
        {
            "initialized": initialized,
            "project_root": project_root,
            "counts": counts,
            "how_it_works": [
                "Niles is a local-first CRM CLI for agents managing relationship work for a user.",
                "Durable state lives under .niles/: append-only events are the source of truth; SQLite indexes are rebuildable projections.",
                "Agents should mutate CRM state only through niles commands, then inspect the JSON envelope returned on stdout.",
                "Agents should not read .niles/index/niles.sqlite directly; use niles note list, contact show --with-notes, task list, report status, or export instead.",
                "Core work is local and offline: contacts, notes, tasks, status, and reports.",
                "EDSL/EP work is explicit: niles exports .ep jobs or humanize requests, ep runs them, and niles imports reviewed results.",
            ],
            "state_contract": {
                "durable": [".niles/events/", ".niles/config.toml", ".niles/surveys/"],
                "derived": [".niles/index/niles.sqlite"],
                "do_not_edit_manually": [".niles/events/"],
            },
            "command_contract": {
                "stdout": "one JSON envelope",
                "success_field": "status",
                "error_fields": ["errors[].code", "errors[].message", "next_steps"],
                "reference_rule": "Use exact ids or unambiguous slugs from prior envelopes for mutations.",
            },
            "available_now": [
                "niles init",
                "niles status",
                "niles contact add/show/list",
                "niles contact update/tag/archive/merge",
                "niles note add",
                "niles note list",
                "niles task add/list/done",
                "niles task update/reassign/cancel/suggest",
                "niles org context set/show",
                "niles material add/list",
                "niles report status --html <path>",
                "niles enrich ingest",
                "niles export",
                "niles import",
                "niles agent next",
            ],
            "planned_edsl_handoff": [
                "niles status-request export/import",
                "niles recommend export/import/accept",
                "niles intake publish/pull/review",
            ],
        },
        next_steps=next_steps,
    )


def agent_steps_for_counts(counts: dict[str, Any]) -> list[NextStep]:
    if counts["contacts"] == 0:
        return [
            NextStep(
                label="Add the first contact",
                command='niles contact add "Acme Data" --tag prospect --trait priority=1',
                mutates=True,
            )
        ]
    if counts["open_tasks"] == 0:
        return [
            NextStep(
                label="Add a next-step task",
                command='niles task add <contact-ref> "Follow up" --due YYYY-MM-DD --assign john',
                mutates=True,
            )
        ]
    return [
        NextStep(
            label="Review open tasks",
            command="niles task list --status open",
            mutates=False,
        ),
        NextStep(
            label="Check CRM status",
            command="niles status",
            mutates=False,
        ),
    ]


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
        return ok(
            "contact show",
            argv,
            {"contact": project.get_contact(args.ref, with_notes=args.with_notes, with_tasks=args.with_tasks)},
        )
    if args.contact_command == "list":
        return ok(
            "contact list",
            argv,
            {"contacts": project.list_contacts(tag=args.tag, stale=args.stale)},
        )
    if args.contact_command == "update":
        return ok(
            "contact update",
            argv,
            project.update_contact(
                args.ref,
                name=args.name,
                company=args.company,
                role=args.role,
                cadence_days=args.cadence_days,
                traits=parse_key_values(args.trait),
                add_emails=args.email,
                add_phones=args.phone,
            ),
        )
    if args.contact_command == "tag":
        return ok(
            "contact tag",
            argv,
            project.update_contact(args.ref, add_tags=args.add, remove_tags=args.remove),
        )
    if args.contact_command == "archive":
        return ok("contact archive", argv, project.archive_contact(args.ref, reason=args.reason))
    if args.contact_command == "merge":
        return ok("contact merge", argv, project.merge_contacts(args.keep, args.duplicate, note=args.note))
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
    if args.note_command == "list":
        return ok("note list", argv, {"notes": project.list_notes(ref=args.ref, limit=args.limit)})
    raise NilesError("unknown_command", "Unknown note command.")


def dispatch_task(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.task_command == "add":
        data = project.add_task(args.ref, args.text, args.due, args.assign, args.tag)
        return ok("task add", argv, data)
    if args.task_command == "list":
        data = project.list_tasks(status=args.status, assignee=args.assignee, due=args.due, contact_ref=args.contact)
        return ok("task list", argv, {"tasks": data})
    if args.task_command == "done":
        data = project.done_task(args.id, args.note)
        return ok("task done", argv, data)
    if args.task_command == "update":
        data = project.update_task(
            args.id,
            text=args.text,
            due_date=args.due,
            assignee=args.assign,
            status=args.status,
            add_tags=args.tag,
            remove_tags=args.remove_tag,
        )
        return ok("task update", argv, data)
    if args.task_command == "reassign":
        return ok("task reassign", argv, project.update_task(args.id, assignee=args.assignee))
    if args.task_command == "cancel":
        return ok("task cancel", argv, project.update_task(args.id, status="cancelled", note=args.note))
    if args.task_command == "suggest":
        return ok("task suggest", argv, {"suggestions": project.suggest_tasks(assignee=args.assignee)})
    raise NilesError("unknown_command", "Unknown task command.")


def dispatch_org(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.org_command == "context":
        if args.org_context_command == "set":
            return ok(
                "org context set",
                argv,
                project.set_org_context(args.name, args.context, parse_key_values(args.trait)),
            )
        if args.org_context_command == "show":
            return ok("org context show", argv, {"org": project.get_org_context()})
    raise NilesError("unknown_command", "Unknown org command.")


def dispatch_material(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.material_command == "add":
        return ok(
            "material add",
            argv,
            project.add_material(
                args.title,
                path=args.path,
                url=args.url,
                description=args.description,
                tags=args.tag,
            ),
        )
    if args.material_command == "list":
        return ok("material list", argv, {"materials": project.list_materials(tag=args.tag)})
    raise NilesError("unknown_command", "Unknown material command.")


def dispatch_report(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.report_command == "status":
        return ok("report status", argv, project.render_status_html(Path(args.html)))
    raise NilesError("unknown_command", "Unknown report command.")


def dispatch_enrich(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.enrich_command == "ingest":
        return ok(
            "enrich ingest",
            argv,
            project.ingest_enrichment(args.ref, args.text, source_url=args.source_url, confidence=args.confidence),
        )
    raise NilesError("unknown_command", "Unknown enrich command.")


def command_name(args: argparse.Namespace) -> str:
    parts = []
    if getattr(args, "command", None):
        parts.append(args.command)
    for attr in (
        "agent_command",
        "contact_command",
        "note_command",
        "task_command",
        "org_command",
        "org_context_command",
        "material_command",
        "report_command",
        "enrich_command",
    ):
        value = getattr(args, attr, None)
        if value:
            parts.append(value)
    return " ".join(parts) or "niles"


if __name__ == "__main__":
    raise SystemExit(main())
