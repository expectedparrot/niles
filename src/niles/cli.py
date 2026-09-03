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
    sync = sub.add_parser("sync")
    sync.add_argument("--message", default="Update Niles CRM")
    sync.add_argument("--no-push", action="store_true")
    sync.add_argument("--dry-run", action="store_true")
    sub.add_parser("rebuild-index")
    sub.add_parser("fsck")
    export_parser = sub.add_parser("export")
    export_parser.add_argument("target")
    export_parser.add_argument("--output")
    export_parser.add_argument("--tag")
    import_parser = sub.add_parser("import")
    import_parser.add_argument("target")
    import_parser.add_argument("path", nargs="?")
    import_parser.add_argument("--replace", action="store_true")
    import_parser.add_argument("--commit", action="store_true")
    import_parser.add_argument("--mapping")

    history = sub.add_parser("history")
    history.add_argument("--contact")
    history.add_argument("--limit", type=int)
    undo = sub.add_parser("undo")
    undo.add_argument("event_id")
    search = sub.add_parser("search")
    search.add_argument("terms", nargs="+")

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
    contact_status = contact_sub.add_parser("status")
    contact_status.add_argument("ref")
    contact_status.add_argument("status")
    contact_status.add_argument("--at")

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

    teammate = sub.add_parser("teammate")
    teammate_sub = teammate.add_subparsers(dest="teammate_command", required=True)
    teammate_add = teammate_sub.add_parser("add")
    teammate_add.add_argument("name")
    teammate_add.add_argument("--alias", action="append", default=[])
    teammate_add.add_argument("--email")
    teammate_add.add_argument("--role")
    teammate_sub.add_parser("list")
    teammate_show = teammate_sub.add_parser("show")
    teammate_show.add_argument("ref")

    survey = sub.add_parser("survey")
    survey_sub = survey.add_subparsers(dest="survey_command", required=True)
    survey_sub.add_parser("list")
    survey_show = survey_sub.add_parser("show")
    survey_show.add_argument("name")
    survey_copy = survey_sub.add_parser("copy")
    survey_copy.add_argument("source")
    survey_copy.add_argument("destination")
    survey_export = survey_sub.add_parser("export-edsl")
    survey_export.add_argument("name")
    survey_export.add_argument("--output", required=True)
    survey_run = survey_sub.add_parser("run")
    survey_run.add_argument("name")
    survey_run.add_argument("--contact")
    survey_run.add_argument("--answers")
    survey_run.add_argument("--dry-run", action="store_true")
    survey_run.add_argument("--no-input", action="store_true")

    intake = sub.add_parser("intake")
    intake_sub = intake.add_subparsers(dest="intake_command", required=True)
    intake_export = intake_sub.add_parser("export")
    intake_export.add_argument("survey")
    intake_export.add_argument("--output")
    intake_register = intake_sub.add_parser("register")
    intake_register.add_argument("survey")
    intake_register.add_argument("registration", nargs="?")
    intake_import = intake_sub.add_parser("import")
    intake_import.add_argument("form_id")
    intake_import.add_argument("responses", nargs="?")
    intake_sub.add_parser("status")
    intake_close = intake_sub.add_parser("close")
    intake_close.add_argument("form_id")
    intake_review = intake_sub.add_parser("review")
    intake_review.add_argument("submission_id", nargs="?")
    intake_decision = intake_review.add_mutually_exclusive_group()
    intake_decision.add_argument("--accept", action="store_true")
    intake_decision.add_argument("--reject", action="store_true")
    intake_decision.add_argument("--merge")
    intake_review.add_argument("--note")

    status_request = sub.add_parser("status-request")
    status_sub = status_request.add_subparsers(dest="status_request_command", required=True)
    status_export = status_sub.add_parser("export")
    status_export.add_argument("survey")
    status_export.add_argument("--output")
    status_register = status_sub.add_parser("register")
    status_register.add_argument("survey")
    status_register.add_argument("registration", nargs="?")
    status_register.add_argument("--contact", required=True)
    status_register.add_argument("--recipient", required=True)
    status_import = status_sub.add_parser("import")
    status_import.add_argument("form_id")
    status_import.add_argument("responses", nargs="?")
    status_sub.add_parser("status")
    status_review = status_sub.add_parser("review")
    status_review.add_argument("submission_id", nargs="?")
    status_decision = status_review.add_mutually_exclusive_group()
    status_decision.add_argument("--accept", action="store_true")
    status_decision.add_argument("--reject", action="store_true")
    status_review.add_argument("--note")

    human_update = sub.add_parser("human-update")
    human_update.add_argument("--output", required=True)
    human_update.add_argument("--scope", choices=["pipeline", "people", "organizations", "all"], default="pipeline")
    human_update.add_argument("--tag", action="append", default=[])
    human_update.add_argument("--stage", action="append", default=[])
    human_update.add_argument("--entity-id", action="append", default=[])
    human_update.add_argument("--include-archived", action="store_true")

    sheet = sub.add_parser("sheet")
    sheet_sub = sheet.add_subparsers(dest="sheet_command", required=True)
    sheet_export = sheet_sub.add_parser("export")
    sheet_export.add_argument("--scope", choices=["pipeline", "people", "organizations", "all"], default="pipeline")
    sheet_export.add_argument("--output", required=True)
    sheet_import = sheet_sub.add_parser("import")
    sheet_import.add_argument("path")
    sheet_review = sheet_sub.add_parser("review")
    sheet_review.add_argument("change_set_id", nargs="?")
    sheet_decision = sheet_review.add_mutually_exclusive_group()
    sheet_decision.add_argument("--accept", action="store_true")
    sheet_decision.add_argument("--reject", action="store_true")
    sheet_review.add_argument("--note")

    recommend = sub.add_parser("recommend")
    recommend_sub = recommend.add_subparsers(dest="recommend_command", required=True)
    recommend_export = recommend_sub.add_parser("export")
    recommend_export.add_argument("name")
    recommend_export.add_argument("--tag")
    recommend_export.add_argument("--output")
    recommend_import = recommend_sub.add_parser("import")
    recommend_import.add_argument("path", nargs="?")
    recommend_import.add_argument("--name", default="next-steps")
    recommend_sub.add_parser("review")
    recommend_accept = recommend_sub.add_parser("accept")
    recommend_accept.add_argument("recommendation_id")
    recommend_accept.add_argument("--assign")
    recommend_accept.add_argument("--due")
    recommend_reject = recommend_sub.add_parser("reject")
    recommend_reject.add_argument("recommendation_id")

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
                "initialized": True,
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
    if args.command == "import" and args.target != "csv":
        data = Project.import_archive(Path.cwd(), Path(args.target), replace=args.replace)
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
    if args.command == "rebuild-index":
        return ok(
            "rebuild-index",
            argv,
            project.rebuild_index_report(),
            next_steps=[
                NextStep(
                    label="Verify filesystem state",
                    command="niles fsck",
                    mutates=False,
                )
            ],
        )
    if args.command == "fsck":
        data = project.fsck()
        if not data["ok"]:
            return Envelope(
                status="error",
                command="fsck",
                argv=argv,
                data=data,
                warnings=data["warnings"],
                errors=data["errors"],
            )
        return ok("fsck", argv, data, warnings=data["warnings"])
    if args.command == "export":
        if args.target in {"csv", "json"}:
            return ok(f"export {args.target}", argv, project.export_contacts(args.target, Path(args.output) if args.output else None, tag=args.tag))
        data = project.export_archive(Path(args.target))
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
    if args.command == "sync":
        data = project.sync(message=args.message, push=not args.no_push, dry_run=args.dry_run)
        return ok("sync", argv, data)
    if args.command == "history":
        return ok("history", argv, {"events": project.history(args.contact, args.limit)})
    if args.command == "undo":
        return ok("undo", argv, project.undo(args.event_id))
    if args.command == "search":
        return ok("search", argv, {"results": project.search(" ".join(args.terms))})
    if args.command == "import":
        if not args.path:
            raise NilesError("missing_import_path", "CSV import requires a path.")
        return ok(
            "import csv",
            argv,
            project.import_csv(
                Path(args.path),
                commit=args.commit,
                mapping_path=Path(args.mapping) if args.mapping else None,
            ),
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
    if args.command == "teammate":
        return dispatch_teammate(project, args, argv)
    if args.command == "survey":
        return dispatch_survey(project, args, argv)
    if args.command == "intake":
        return dispatch_intake(project, args, argv)
    if args.command == "status-request":
        return dispatch_status_request(project, args, argv)
    if args.command == "human-update":
        return ok(
            "human-update",
            argv,
            project.export_human_update(
                Path(args.output),
                scope=args.scope,
                tags=args.tag,
                stages=args.stage,
                entity_ids=args.entity_id,
                include_archived=args.include_archived,
            ),
        )
    if args.command == "sheet":
        if args.sheet_command == "export":
            return ok("sheet export", argv, project.export_sheet(Path(args.output), args.scope))
        if args.sheet_command == "import":
            return ok("sheet import", argv, project.import_sheet(Path(args.path)))
        if not args.change_set_id:
            return ok("sheet review", argv, {"pending": project.list_sheet_changes()})
        if not args.accept and not args.reject:
            raise NilesError("decision_required", "Choose --accept or --reject.")
        return ok("sheet review", argv, project.review_sheet_change(args.change_set_id, args.accept, args.note))
    if args.command == "recommend":
        return dispatch_recommend(project, args, argv)
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
                "Niles manages its own durable event log, rebuildable index, and temporary exchange artifacts.",
                "Agents must read and mutate CRM state only through niles commands, then inspect the JSON envelope returned on stdout.",
                "Core work is local and offline: contacts, notes, tasks, status, and reports.",
                "EP handoffs are explicit: Niles exports locally and returns an exact EP command; EP publishes, retrieves, or runs; Niles registers or imports the resulting local artifact.",
            ],
            "state_contract": {
                "storage": "managed_by_niles",
                "source_of_truth": "append_only_event_log",
                "derived_state": "rebuildable_index",
                "exchange_artifacts": "managed_and_disposable",
                "agent_rule": "never inspect or edit Niles internal storage; use CLI commands",
                "sync": "use niles sync; never stage Niles internal paths manually",
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
                "niles sync",
                "niles rebuild-index",
                "niles fsck",
                "niles contact add/show/list",
                "niles contact update/tag/archive/merge/status",
                "niles note add",
                "niles note list",
                "niles task add/list/done",
                "niles task update/reassign/cancel/suggest",
                "niles org context set/show",
                "niles material add/list",
                "niles teammate add/list/show",
                "niles search",
                "niles history/undo",
                "niles import csv --commit",
                "niles export csv|json",
                "niles survey list/show/copy/run/export-edsl",
                "niles intake export/register/import/status/close/review",
                "niles status-request export/register/import/status/review",
                "niles human-update [--scope pipeline|people|organizations|all] [--tag t] [--stage s] [--entity-id id] [--include-archived] --output <update-job.ep>",
                "niles sheet export/import/review",
                "niles recommend export/import/review/accept/reject",
                "niles report status --html <path>",
                "niles enrich ingest",
                "niles export",
                "niles import",
                "niles agent next",
            ],
            "edsl_handoff_rule": "Niles only exports, registers, and imports local artifacts. The ep CLI exclusively publishes, retrieves responses, and runs jobs. Imported data remains quarantined until an explicit Niles review decision.",
            "managed_handoffs": {
                "intake": ["niles intake export", "run data.publish_command", "niles intake register", "run data.pull_command", "niles intake import", "niles intake review"],
                "status_request": ["niles status-request export", "run data.publish_command", "niles status-request register", "run data.pull_command", "niles status-request import", "niles status-request review"],
                "human_update": ["niles human-update --scope pipeline --output update-job.ep", "run data.publish_command"],
                "spreadsheet": ["niles sheet export --output crm-review.xlsx", "edit the workbook", "niles sheet import crm-review.xlsx", "niles sheet review <change-set-id> --accept"],
                "recommendation": ["niles recommend export", "run data.run_command", "niles recommend import", "niles recommend review"],
            },
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
    if args.contact_command == "status":
        return ok("contact status", argv, project.set_contact_status(args.ref, args.status, args.at))
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


def dispatch_teammate(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.teammate_command == "add":
        return ok("teammate add", argv, project.add_teammate(args.name, args.alias, args.email, args.role))
    if args.teammate_command == "list":
        return ok("teammate list", argv, {"teammates": project.list_teammates()})
    if args.teammate_command == "show":
        return ok("teammate show", argv, {"teammate": project.resolve_teammate(args.ref)})
    raise NilesError("unknown_command", "Unknown teammate command.")


def dispatch_survey(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.survey_command == "list":
        return ok("survey list", argv, {"surveys": project.list_surveys()})
    if args.survey_command == "show":
        return ok("survey show", argv, {"survey": project.get_survey(args.name)})
    if args.survey_command == "copy":
        return ok("survey copy", argv, project.copy_survey(args.source, args.destination))
    if args.survey_command == "export-edsl":
        return ok("survey export-edsl", argv, project.export_survey_edsl(args.name, Path(args.output)))
    if args.survey_command == "run":
        answers = None
        if args.answers:
            from .surveys import loads_answers

            answer_path = Path(args.answers)
            if not answer_path.is_absolute():
                answer_path = project.root / answer_path
            if not answer_path.is_file():
                raise NilesError("answers_not_found", f"Answers file not found: {args.answers}")
            answers = loads_answers(answer_path.read_text(encoding="utf-8"))
        return ok("survey run", argv, project.run_survey(args.name, args.contact, answers, args.dry_run))
    raise NilesError("unknown_command", "Unknown survey command.")


def dispatch_intake(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.intake_command == "export":
        return ok("intake export", argv, project.export_form("intake", args.survey, Path(args.output) if args.output else None))
    if args.intake_command == "register":
        return ok("intake register", argv, project.register_form("intake", args.survey, Path(args.registration) if args.registration else None))
    if args.intake_command == "import":
        return ok("intake import", argv, project.import_form(args.form_id, "intake", Path(args.responses) if args.responses else None))
    if args.intake_command == "status":
        return ok("intake status", argv, {"forms": project.list_forms("intake"), "pending": project.list_submissions("intake")})
    if args.intake_command == "close":
        return ok("intake close", argv, project.close_form(args.form_id, "intake"))
    if args.intake_command == "review":
        if not args.submission_id:
            return ok("intake review", argv, {"pending": project.list_submissions("intake")})
        if not (args.accept or args.reject or args.merge):
            raise NilesError("decision_required", "Choose --accept, --reject, or --merge <contact-ref>.")
        decision = "reject" if args.reject else "accept"
        return ok("intake review", argv, project.review_submission(args.submission_id, "intake", decision, merge_ref=args.merge, note=args.note))
    raise NilesError("unknown_command", "Unknown intake command.")


def dispatch_status_request(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.status_request_command == "export":
        return ok("status-request export", argv, project.export_form("status-request", args.survey, Path(args.output) if args.output else None))
    if args.status_request_command == "register":
        return ok("status-request register", argv, project.register_form("status-request", args.survey, Path(args.registration) if args.registration else None, args.contact, args.recipient))
    if args.status_request_command == "import":
        return ok("status-request import", argv, project.import_form(args.form_id, "status-request", Path(args.responses) if args.responses else None))
    if args.status_request_command == "status":
        return ok("status-request status", argv, {"forms": project.list_forms("status-request"), "pending": project.list_submissions("status-request")})
    if args.status_request_command == "review":
        if not args.submission_id:
            return ok("status-request review", argv, {"pending": project.list_submissions("status-request")})
        if not (args.accept or args.reject):
            raise NilesError("decision_required", "Choose --accept or --reject.")
        decision = "reject" if args.reject else "accept"
        return ok("status-request review", argv, project.review_submission(args.submission_id, "status-request", decision, note=args.note))
    raise NilesError("unknown_command", "Unknown status-request command.")


def dispatch_recommend(project: Project, args: argparse.Namespace, argv: list[str]) -> Envelope:
    if args.recommend_command == "export":
        return ok("recommend export", argv, project.export_recommendation_job(args.name, Path(args.output) if args.output else None, args.tag))
    if args.recommend_command == "import":
        return ok("recommend import", argv, project.import_recommendations(Path(args.path) if args.path else None, name=args.name))
    if args.recommend_command == "review":
        return ok("recommend review", argv, {"pending": project.list_recommendations()})
    if args.recommend_command == "accept":
        return ok("recommend accept", argv, project.review_recommendation(args.recommendation_id, True, args.assign, args.due))
    if args.recommend_command == "reject":
        return ok("recommend reject", argv, project.review_recommendation(args.recommendation_id, False))
    raise NilesError("unknown_command", "Unknown recommend command.")


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
        "teammate_command",
        "survey_command",
        "intake_command",
        "status_request_command",
        "sheet_command",
        "recommend_command",
        "report_command",
        "enrich_command",
    ):
        value = getattr(args, attr, None)
        if value:
            parts.append(value)
    return " ".join(parts) or "niles"


if __name__ == "__main__":
    raise SystemExit(main())
