from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import tomllib
import zipfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


class NilesError(Exception):
    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_timestamp(value: str | None) -> str:
    if value is None:
        return utc_now()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).isoformat()
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise NilesError("invalid_timestamp", f"Invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def loads(value: str | None, default: Any) -> Any:
    if value in (None, ""):
        return default
    return json.loads(value)


def unique(values: list[str]) -> list[str]:
    return sorted({value for value in values if value})


EXPORT_SCHEMA_VERSION = "niles.export.v1"
EXPORT_MANIFEST = "niles-export-manifest.json"
PROJECT_SCHEMA_VERSION = "niles.project.v1"
EVENT_SCHEMA_VERSION = "niles.event.v1"
SUPPORTED_EVENT_TYPES = {
    "contact_created",
    "contact_updated",
    "contact_archived",
    "contacts_merged",
    "note_created",
    "task_created",
    "task_done",
    "task_updated",
    "org_context_set",
    "material_added",
    "teammate_added",
    "event_reverted",
    "form_published",
    "form_closed",
    "submission_received",
    "submission_reviewed",
    "recommendation_imported",
    "recommendation_reviewed",
}


@dataclass
class Project:
    root: Path

    @property
    def state_dir(self) -> Path:
        return self.root / ".niles"

    @property
    def events_dir(self) -> Path:
        return self.state_dir / "events"

    @property
    def index_path(self) -> Path:
        return self.state_dir / "index" / "niles.sqlite"

    @property
    def exchange_dir(self) -> Path:
        """Private, disposable files used to hand work to and from EP."""
        return self.state_dir / "exchange"

    @property
    def manifest_path(self) -> Path:
        return self.state_dir / "manifest.json"

    @classmethod
    def init(cls, root: Path) -> "Project":
        project = cls(root.resolve())
        project.events_dir.mkdir(parents=True, exist_ok=True)
        (project.state_dir / "index").mkdir(parents=True, exist_ok=True)
        (project.state_dir / "surveys").mkdir(parents=True, exist_ok=True)
        (project.state_dir / "reports").mkdir(parents=True, exist_ok=True)
        config = project.state_dir / "config.toml"
        if not config.exists():
            config.write_text('format_version = 1\n', encoding="utf-8")
        if not project.manifest_path.exists():
            project.manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "project_id": new_id("proj"),
                        "created_at": utc_now(),
                        "storage": {
                            "source_of_truth": ".niles/events/",
                            "derived": [".niles/index/niles.sqlite"],
                        },
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        niles_gitignore = project.state_dir / ".gitignore"
        if not niles_gitignore.exists():
            niles_gitignore.write_text("index/\nexchange/\n", encoding="utf-8")
        from .surveys import TEMPLATES, template_definition

        for survey_name in TEMPLATES:
            survey_path = project.state_dir / "surveys" / f"{survey_name}.json"
            if not survey_path.exists():
                survey_path.write_text(json.dumps(template_definition(survey_name), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        project.rebuild_index()
        return project

    @classmethod
    def open(cls, start: Path) -> "Project":
        current = start.resolve()
        for path in [current, *current.parents]:
            if (path / ".niles").is_dir():
                project = cls(path)
                if not project.index_path.exists():
                    project.rebuild_index()
                return project
        raise NilesError(
            "not_initialized",
            "No .niles directory found. Run `niles init` first.",
            {"start": str(start)},
        )

    @classmethod
    def import_archive(cls, root: Path, archive_path: Path, replace: bool = False) -> dict[str, Any]:
        destination = root.resolve()
        state_dir = destination / ".niles"
        archive = archive_path.expanduser().resolve()
        if not archive.is_file():
            raise NilesError("archive_not_found", f"Archive not found: {archive_path}", {"path": str(archive_path)})
        if state_dir.exists() and not replace:
            raise NilesError(
                "project_exists",
                "A .niles directory already exists. Use --replace to overwrite it.",
                {"project_root": str(destination), "state_dir": str(state_dir)},
            )

        try:
            zf_context = zipfile.ZipFile(archive, "r")
        except zipfile.BadZipFile as exc:
            raise NilesError("invalid_archive", "Archive is not a readable zip file.", {"path": str(archive)}) from exc

        with zf_context as zf:
            names = zf.namelist()
            if EXPORT_MANIFEST not in names:
                raise NilesError("invalid_archive", f"Missing {EXPORT_MANIFEST}.", {"path": str(archive)})
            try:
                manifest = json.loads(zf.read(EXPORT_MANIFEST).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise NilesError("invalid_archive", "Archive manifest is not valid JSON.", {"path": str(archive)}) from exc
            if manifest.get("schema_version") != EXPORT_SCHEMA_VERSION:
                raise NilesError(
                    "unsupported_archive",
                    "Archive schema is not supported.",
                    {"schema_version": manifest.get("schema_version"), "supported": EXPORT_SCHEMA_VERSION},
                )
            members = [name for name in names if name != EXPORT_MANIFEST and not name.endswith("/")]
            for name in members:
                validate_archive_member(name)
            if ".niles/manifest.json" not in members and ".niles/config.toml" not in members:
                raise NilesError("invalid_archive", "Archive is missing .niles/manifest.json.", {"path": str(archive)})
            if state_dir.exists():
                shutil.rmtree(state_dir)
            state_dir.mkdir(parents=True, exist_ok=True)
            for name in members:
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(name))

        project = cls(destination)
        project.events_dir.mkdir(parents=True, exist_ok=True)
        (project.state_dir / "surveys").mkdir(parents=True, exist_ok=True)
        (project.state_dir / "reports").mkdir(parents=True, exist_ok=True)
        (project.state_dir / "index").mkdir(parents=True, exist_ok=True)
        niles_gitignore = project.state_dir / ".gitignore"
        if not niles_gitignore.exists():
            niles_gitignore.write_text("index/\n", encoding="utf-8")
        project.rebuild_index()
        return {
            "project_root": str(destination),
            "archive": str(archive),
            "manifest": manifest,
            "counts": project.counts(),
        }

    def append_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.events_dir.mkdir(parents=True, exist_ok=True)
        seq = self._next_sequence()
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "event_id": new_id("evt"),
            "sequence": seq,
            "created_at": utc_now(),
            "type": event_type,
            "payload": payload,
        }
        path = self.events_dir / f"{seq:012d}.json"
        path.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.apply_event(event)
        return event

    def _next_sequence(self) -> int:
        existing = sorted(self.events_dir.glob("*.json"))
        if not existing:
            return 1
        return int(existing[-1].stem) + 1

    def export_archive(self, archive_path: Path) -> dict[str, Any]:
        archive = archive_path.expanduser()
        if archive.suffix.lower() != ".zip":
            archive = archive.with_suffix(archive.suffix + ".zip") if archive.suffix else archive.with_suffix(".zip")
        if not archive.is_absolute():
            archive = self.root / archive
        archive.parent.mkdir(parents=True, exist_ok=True)

        event_files = sorted(self.events_dir.glob("*.json"))
        survey_files = sorted(path for path in (self.state_dir / "surveys").rglob("*") if path.is_file())
        report_files = sorted(path for path in (self.state_dir / "reports").rglob("*") if path.is_file())
        config_path = self.state_dir / "config.toml"
        manifest_path = self.manifest_path
        niles_gitignore = self.state_dir / ".gitignore"
        manifest = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "created_at": utc_now(),
            "project_root_name": self.root.name,
            "includes": {
                "manifest": manifest_path.exists(),
                "config": config_path.exists(),
                "events": len(event_files),
                "surveys": len(survey_files),
                "reports": len(report_files),
                "index": False,
            },
            "counts": self.counts(),
        }

        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(EXPORT_MANIFEST, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            zf.writestr(".niles/events/", "")
            zf.writestr(".niles/surveys/", "")
            zf.writestr(".niles/reports/", "")
            if manifest_path.exists():
                zf.write(manifest_path, ".niles/manifest.json")
            if niles_gitignore.exists():
                zf.write(niles_gitignore, ".niles/.gitignore")
            if config_path.exists():
                zf.write(config_path, ".niles/config.toml")
            for path in event_files:
                zf.write(path, path.relative_to(self.root).as_posix())
            for path in survey_files:
                zf.write(path, path.relative_to(self.root).as_posix())
            for path in report_files:
                zf.write(path, path.relative_to(self.root).as_posix())

        return {
            "archive": str(archive),
            "manifest": manifest,
            "portable": True,
            "restore_command": f"niles import {archive}",
        }

    def connect(self) -> sqlite3.Connection:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.index_path)
        conn.row_factory = sqlite3.Row
        self.ensure_schema(conn)
        return conn

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            create table if not exists contacts (
              id text primary key,
              slug text not null,
              name text not null,
              emails text not null,
              phones text not null,
              company text,
              role text,
              traits text not null,
              tags text not null,
              cadence_days integer,
              archived integer not null default 0,
              created_at text not null
            );
            create table if not exists notes (
              id text primary key,
              contact_id text not null,
              created_at text not null,
              event_sequence integer not null default 0,
              kind text not null,
              text text not null,
              source text not null
            );
            create table if not exists tasks (
              id text primary key,
              contact_id text,
              assignee text,
              due_date text,
              text text not null,
              status text not null,
              tags text not null,
              source text not null,
              created_at text not null,
              done_note text
            );
            create table if not exists org_context (
              id integer primary key check (id = 1),
              name text,
              context text,
              traits text not null,
              updated_at text not null
            );
            create table if not exists materials (
              id text primary key,
              title text not null,
              path text,
              url text,
              description text,
              tags text not null,
              created_at text not null
            );
            create table if not exists teammates (
              id text primary key,
              name text not null,
              aliases text not null,
              email text,
              role text,
              created_at text not null
            );
            create virtual table if not exists crm_search using fts5(
              entity_type unindexed,
              entity_id unindexed,
              contact_id unindexed,
              label,
              body
            );
            create table if not exists forms (
              id text primary key,
              kind text not null,
              survey_name text not null,
              remote_uuid text not null,
              respondent_url text,
              admin_url text,
              contact_id text,
              recipient text,
              status text not null,
              created_at text not null
            );
            create table if not exists submissions (
              id text primary key,
              form_id text not null,
              remote_id text,
              answers text not null,
              status text not null,
              matched_contact_id text,
              received_at text not null,
              reviewed_at text,
              review_note text
            );
            create table if not exists recommendations (
              id text primary key,
              contact_id text not null,
              text text not null,
              rationale text,
              source_path text not null,
              status text not null,
              imported_at text not null,
              reviewed_at text
            );
            """
        )
        note_columns = {row["name"] for row in conn.execute("pragma table_info(notes)").fetchall()}
        if "event_sequence" not in note_columns:
            conn.execute("alter table notes add column event_sequence integer not null default 0")
        conn.commit()

    def _refresh_search_index(self, conn: sqlite3.Connection) -> None:
        conn.execute("delete from crm_search")
        for row in conn.execute("select * from contacts where archived = 0").fetchall():
            contact = self._contact_from_row(row)
            body = " ".join(
                [contact["name"], contact.get("company") or "", contact.get("role") or "", dumps(contact["traits"]), " ".join(contact["tags"])]
            )
            conn.execute("insert into crm_search values (?, ?, ?, ?, ?)", ("contact", contact["id"], contact["id"], contact["name"], body))
        for row in conn.execute("select notes.*, contacts.name as contact_name from notes join contacts on notes.contact_id = contacts.id").fetchall():
            conn.execute("insert into crm_search values (?, ?, ?, ?, ?)", ("note", row["id"], row["contact_id"], row["text"], row["text"]))
        for row in conn.execute("select tasks.*, contacts.name as contact_name from tasks left join contacts on tasks.contact_id = contacts.id").fetchall():
            conn.execute("insert into crm_search values (?, ?, ?, ?, ?)", ("task", row["id"], row["contact_id"], row["text"], row["text"]))

    def rebuild_index(self) -> None:
        if self.index_path.exists():
            self.index_path.unlink()
        events = self._read_events()
        reverted = {event["payload"]["target_event_id"] for event in events if event["type"] == "event_reverted"}
        with self.connect() as conn:
            for event in events:
                if event["event_id"] not in reverted:
                    self._apply_event(conn, event)

    def rebuild_index_report(self) -> dict[str, Any]:
        self.rebuild_index()
        return {
            "project_root": str(self.root),
            "rebuilt": str(self.index_path),
            "counts": self.counts(),
        }

    def sync(self, message: str, push: bool = True, dry_run: bool = False) -> dict[str, Any]:
        durable_paths = [
            "README.md",
            ".niles/manifest.json",
            ".niles/.gitignore",
            ".niles/config.toml",
            ".niles/events",
            ".niles/surveys",
            ".niles/reports",
        ]
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            raise NilesError("not_git_repository", "Niles sync requires a git repository.")
        git_root = Path(probe.stdout.strip()).resolve()
        if git_root != self.root:
            raise NilesError(
                "git_root_mismatch",
                "The Niles project must be the git repository root for sync.",
                {"project_root": str(self.root), "git_root": str(git_root)},
            )
        projection = self.readme_projection()
        projection_changed = not self.readme_path.exists() or self.readme_path.read_text(encoding="utf-8") != projection
        if not dry_run and projection_changed:
            self.readme_path.write_text(projection, encoding="utf-8")
        status = subprocess.run(
            ["git", "status", "--short", "--", *durable_paths],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if status.returncode != 0:
            raise NilesError("git_status_failed", status.stderr.strip() or "Could not inspect durable Niles state.")
        changes = [line for line in status.stdout.splitlines() if line]
        plan = {
            "durable_paths": durable_paths,
            "excluded": ["rebuildable index", "managed exchange files"],
            "changes": changes,
            "message": message,
            "push": push,
            "readme_projection": {"path": str(self.readme_path), "changed": projection_changed},
        }
        if dry_run:
            return {**plan, "dry_run": True, "committed": False, "pushed": False}
        add = subprocess.run(
            ["git", "add", "--", *durable_paths],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if add.returncode != 0:
            raise NilesError("git_add_failed", add.stderr.strip() or "Could not stage durable Niles state.")
        staged_names = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--", *durable_paths],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if staged_names.returncode != 0:
            raise NilesError("git_diff_failed", "Could not inspect staged Niles state.")
        staged_files = [line for line in staged_names.stdout.splitlines() if line]
        committed = bool(staged_files)
        commit_hash = None
        if committed:
            commit = subprocess.run(
                ["git", "commit", "-m", message, "--", *staged_files],
                cwd=self.root,
                text=True,
                capture_output=True,
                check=False,
            )
            if commit.returncode != 0:
                raise NilesError("git_commit_failed", commit.stderr.strip() or commit.stdout.strip())
            commit_hash = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=self.root, text=True, capture_output=True, check=True
            ).stdout.strip()
        pushed = False
        if push:
            push_result = subprocess.run(
                ["git", "push"], cwd=self.root, text=True, capture_output=True, check=False
            )
            if push_result.returncode != 0:
                raise NilesError(
                    "git_push_failed",
                    push_result.stderr.strip() or push_result.stdout.strip(),
                    {"committed": committed, "commit": commit_hash},
                )
            pushed = True
        return {**plan, "dry_run": False, "committed": committed, "commit": commit_hash, "pushed": pushed}

    @property
    def readme_path(self) -> Path:
        return self.root / "README.md"

    def readme_projection(self) -> str:
        """Return the deterministic, human-facing projection tracked by sync."""
        contacts = self.list_contacts()
        tasks = self.list_tasks(status="open")
        notes_by_contact = {contact["id"]: self.list_notes(contact["id"]) for contact in contacts}
        tasks_by_contact: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            tasks_by_contact.setdefault(task.get("contact_id") or "", []).append(task)

        def cell(value: Any) -> str:
            text = str(value if value not in (None, "") else "—")
            return text.replace("|", "\\|").replace("\n", " ")

        def kind(contact: dict[str, Any]) -> str:
            traits = contact.get("traits", {})
            declared = str(traits.get("entity_type") or "").lower()
            tags = {str(tag).lower() for tag in contact.get("tags", [])}
            if declared in {"person", "individual"} or tags & {"person", "individual"} or contact.get("company"):
                return "person"
            if declared in {"organization", "organisation", "company", "account"} or tags & {"organization", "organisation", "company", "account", "prospect", "target", "lead", "customer", "client", "lost", "dead"} or traits.get("stage"):
                return "organization"
            return "ambiguous"

        def stage(contact: dict[str, Any]) -> str:
            tags = {str(tag).lower() for tag in contact.get("tags", [])}
            return str(contact.get("traits", {}).get("stage") or next((item for item in ("target", "engaged", "demo", "pilot", "contracting", "won", "stalled", "lost") if item in tags), "unspecified"))

        def current_status(contact: dict[str, Any]) -> str:
            explicit = contact.get("traits", {}).get("current_status")
            notes = notes_by_contact.get(contact["id"], [])
            return str(explicit or (notes[0]["text"] if notes else "No status recorded"))

        organizations = [item for item in contacts if kind(item) == "organization"]
        people = [item for item in contacts if kind(item) == "person"]
        ambiguous = [item for item in contacts if kind(item) == "ambiguous"]
        active = [item for item in organizations if stage(item) not in {"won", "lost"} and not ({"lost", "dead"} & {str(tag).lower() for tag in item.get("tags", [])})]
        active.sort(key=lambda item: (stage(item), item["name"].lower()))

        pipeline_rows = []
        for account in active:
            next_task = (tasks_by_contact.get(account["id"]) or [None])[0]
            pipeline_rows.append(
                f"| {cell(account['name'])} | {cell(stage(account))} | {cell(account.get('traits', {}).get('priority'))} | {cell(current_status(account))} | {cell(next_task.get('text') if next_task else None)} | {cell(next_task.get('assignee') if next_task else None)} | {cell(next_task.get('due_date') if next_task else None)} |"
            )
        relationship_rows = [
            f"| {cell(person['name'])} | {cell(person.get('company'))} | {cell(person.get('role') or ', '.join(person.get('tags', [])))} | {cell(current_status(person))} |"
            for person in sorted(people, key=lambda item: item["name"].lower())
        ]
        action_rows = [
            f"| {cell(task.get('assignee'))} | {cell(task.get('due_date'))} | {cell(task.get('contact'))} | {cell(task['text'])} |"
            for task in tasks
        ]
        warnings = [f"- {cell(item['name'])}: entity type is ambiguous; tag as `person` or `company`." for item in ambiguous]
        org = self.get_org_context()
        title = org.get("name") or "CRM"
        managed = (
            "<!-- niles:projection:start -->\n"
            "> This section is generated by `niles sync`. Update CRM data with `niles` commands; do not edit it by hand.\n\n"
            f"**{len(active)} active accounts · {len(people)} people · {len(tasks)} open actions**\n\n"
            "## Active pipeline\n\n| Account | Stage | Priority | Current status | Next action | Owner | Due |\n|---|---|---:|---|---|---|---|\n"
            + ("\n".join(pipeline_rows) or "| — | — | — | No active accounts | — | — | — |")
            + "\n\n## Actions\n\n| Owner | Due | Account | Action |\n|---|---|---|---|\n"
            + ("\n".join(action_rows) or "| — | — | — | No open actions |")
            + "\n\n## Relationship network\n\n| Person | Organization | Role | Current status |\n|---|---|---|---|\n"
            + ("\n".join(relationship_rows) or "| — | — | — | No people recorded |")
            + "\n\n## Data quality\n\n"
            + ("\n".join(warnings) or "- No ambiguous entity types detected.")
            + "\n<!-- niles:projection:end -->\n"
        )
        if not self.readme_path.exists():
            return f"# {title}\n\n{managed}"
        existing = self.readme_path.read_text(encoding="utf-8")
        start_marker = "<!-- niles:projection:start -->"
        end_marker = "<!-- niles:projection:end -->"
        start = existing.find(start_marker)
        end = existing.find(end_marker)
        if start >= 0 and end >= start:
            end += len(end_marker)
            return existing[:start] + managed.rstrip("\n") + existing[end:]
        separator = "" if not existing or existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        return existing + separator + managed

    def fsck(self) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        if not self.manifest_path.exists():
            warnings.append({"code": "missing_manifest", "message": ".niles/manifest.json is missing."})
        else:
            try:
                manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if manifest.get("schema_version") != PROJECT_SCHEMA_VERSION:
                    errors.append(
                        {
                            "code": "unsupported_project_schema",
                            "message": "Unsupported project manifest schema.",
                            "schema_version": manifest.get("schema_version"),
                        }
                    )
            except json.JSONDecodeError as exc:
                errors.append({"code": "invalid_manifest_json", "message": str(exc)})

        events: list[dict[str, Any]] = []
        event_ids: set[str] = set()
        event_paths = sorted(self.events_dir.glob("*.json"))
        for expected_sequence, path in enumerate(event_paths, start=1):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                errors.append({"code": "invalid_event_json", "path": str(path), "message": str(exc)})
                continue
            if event.get("schema_version") != EVENT_SCHEMA_VERSION:
                errors.append(
                    {
                        "code": "unsupported_event_schema",
                        "path": str(path),
                        "schema_version": event.get("schema_version"),
                    }
                )
            if event.get("type") not in SUPPORTED_EVENT_TYPES:
                errors.append(
                    {
                        "code": "unsupported_event_type",
                        "path": str(path),
                        "event_type": event.get("type"),
                    }
                )
            if event.get("sequence") != expected_sequence:
                errors.append(
                    {
                        "code": "event_sequence_gap",
                        "path": str(path),
                        "expected": expected_sequence,
                        "actual": event.get("sequence"),
                    }
                )
            if path.name != f"{event.get('sequence', 0):012d}.json":
                errors.append(
                    {
                        "code": "event_filename_mismatch",
                        "path": str(path),
                        "sequence": event.get("sequence"),
                    }
                )
            event_id = event.get("event_id")
            if event_id in event_ids:
                errors.append({"code": "duplicate_event_id", "event_id": event_id, "path": str(path)})
            if event_id:
                event_ids.add(event_id)
            events.append(event)

        replay_errors = self._replay_errors(events)
        errors.extend(replay_errors)
        ok = not errors
        return {
            "ok": ok,
            "project_root": str(self.root),
            "event_count": len(event_paths),
            "index_path": str(self.index_path),
            "index_is_derived": True,
            "errors": errors,
            "warnings": warnings,
        }

    def _replay_errors(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if any(event.get("schema_version") != EVENT_SCHEMA_VERSION for event in events):
            return []
        reverted = {
            event["payload"]["target_event_id"]
            for event in events
            if event.get("type") == "event_reverted"
        }
        with TemporaryDirectory(prefix="niles-fsck-") as tmp:
            probe = Project(Path(tmp))
            probe.events_dir.mkdir(parents=True, exist_ok=True)
            (probe.state_dir / "index").mkdir(parents=True, exist_ok=True)
            try:
                with probe.connect() as conn:
                    for event in events:
                        if event["event_id"] not in reverted:
                            probe._apply_event(conn, event)
                    orphan_notes = conn.execute(
                        """
                        select count(*)
                          from notes
                          left join contacts on notes.contact_id = contacts.id
                         where contacts.id is null
                        """
                    ).fetchone()[0]
                    orphan_tasks = conn.execute(
                        """
                        select count(*)
                          from tasks
                          left join contacts on tasks.contact_id = contacts.id
                         where tasks.contact_id is not null and contacts.id is null
                        """
                    ).fetchone()[0]
                    orphan_submissions = conn.execute(
                        "select count(*) from submissions left join forms on submissions.form_id = forms.id where forms.id is null"
                    ).fetchone()[0]
                    orphan_recommendations = conn.execute(
                        "select count(*) from recommendations left join contacts on recommendations.contact_id = contacts.id where contacts.id is null"
                    ).fetchone()[0]
            except Exception as exc:  # noqa: BLE001 - fsck reports replay failures as data.
                return [{"code": "replay_failed", "message": str(exc)}]
        replay_errors = []
        if orphan_notes:
            replay_errors.append({"code": "orphan_notes", "count": orphan_notes})
        if orphan_tasks:
            replay_errors.append({"code": "orphan_tasks", "count": orphan_tasks})
        if orphan_submissions:
            replay_errors.append({"code": "orphan_submissions", "count": orphan_submissions})
        if orphan_recommendations:
            replay_errors.append({"code": "orphan_recommendations", "count": orphan_recommendations})
        return replay_errors

    def apply_event(self, event: dict[str, Any]) -> None:
        with self.connect() as conn:
            self._apply_event(conn, event)

    def _apply_event(self, conn: sqlite3.Connection, event: dict[str, Any]) -> None:
        kind = event["type"]
        payload = event["payload"]
        if kind == "contact_created":
            conn.execute(
                """
                insert or replace into contacts
                  (id, slug, name, emails, phones, company, role, traits, tags, cadence_days, archived, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["slug"],
                    payload["name"],
                    dumps(payload.get("emails", [])),
                    dumps(payload.get("phones", [])),
                    payload.get("company"),
                    payload.get("role"),
                    dumps(payload.get("traits", {})),
                    dumps(payload.get("tags", [])),
                    payload.get("cadence_days"),
                    0,
                    payload["created_at"],
                ),
            )
        elif kind == "note_created":
            conn.execute(
                """
                insert or replace into notes
                  (id, contact_id, created_at, event_sequence, kind, text, source)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["contact_id"],
                    payload["created_at"],
                    event.get("sequence", 0),
                    payload["kind"],
                    payload["text"],
                    payload.get("source", "user"),
                ),
            )
        elif kind == "contact_updated":
            contact = self._contact_by_id(conn, payload["id"])
            merged = {
                **contact,
                **{key: value for key, value in payload.get("fields", {}).items() if value is not None},
            }
            traits = {**contact.get("traits", {}), **payload.get("traits", {})}
            tags = set(contact.get("tags", []))
            tags.update(payload.get("add_tags", []))
            tags.difference_update(payload.get("remove_tags", []))
            emails = contact.get("emails", [])
            emails.extend(payload.get("add_emails", []))
            phones = contact.get("phones", [])
            phones.extend(payload.get("add_phones", []))
            conn.execute(
                """
                update contacts
                   set slug = ?, name = ?, emails = ?, phones = ?, company = ?, role = ?,
                       traits = ?, tags = ?, cadence_days = ?
                 where id = ?
                """,
                (
                    merged.get("slug") or slugify(merged["name"]),
                    merged["name"],
                    dumps(unique(emails)),
                    dumps(unique(phones)),
                    merged.get("company"),
                    merged.get("role"),
                    dumps(traits),
                    dumps(unique(list(tags))),
                    merged.get("cadence_days"),
                    payload["id"],
                ),
            )
        elif kind == "contact_archived":
            conn.execute("update contacts set archived = 1 where id = ?", (payload["id"],))
        elif kind == "contacts_merged":
            keep_id = payload["keep_id"]
            duplicate_id = payload["duplicate_id"]
            conn.execute("update notes set contact_id = ? where contact_id = ?", (keep_id, duplicate_id))
            conn.execute("update tasks set contact_id = ? where contact_id = ?", (keep_id, duplicate_id))
            conn.execute("update contacts set archived = 1 where id = ?", (duplicate_id,))
        elif kind == "task_created":
            conn.execute(
                """
                insert or replace into tasks
                  (id, contact_id, assignee, due_date, text, status, tags, source, created_at, done_note)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload.get("contact_id"),
                    payload.get("assignee"),
                    payload.get("due_date"),
                    payload["text"],
                    payload.get("status", "open"),
                    dumps(payload.get("tags", [])),
                    payload.get("source", "user"),
                    payload["created_at"],
                    None,
                ),
            )
        elif kind == "task_done":
            conn.execute(
                "update tasks set status = 'done', done_note = ? where id = ?",
                (payload.get("note"), payload["id"]),
            )
        elif kind == "task_updated":
            task = self._task_by_id(conn, payload["id"])
            tags = set(task.get("tags", []))
            tags.update(payload.get("add_tags", []))
            tags.difference_update(payload.get("remove_tags", []))
            fields = payload.get("fields", {})
            conn.execute(
                """
                update tasks
                   set assignee = ?, due_date = ?, text = ?, status = ?, tags = ?, done_note = ?
                 where id = ?
                """,
                (
                    fields.get("assignee", task.get("assignee")),
                    fields.get("due_date", task.get("due_date")),
                    fields.get("text", task["text"]),
                    fields.get("status", task["status"]),
                    dumps(unique(list(tags))),
                    fields.get("done_note", task.get("done_note")),
                    payload["id"],
                ),
            )
        elif kind == "org_context_set":
            conn.execute(
                """
                insert or replace into org_context (id, name, context, traits, updated_at)
                values (1, ?, ?, ?, ?)
                """,
                (
                    payload.get("name"),
                    payload.get("context"),
                    dumps(payload.get("traits", {})),
                    payload["updated_at"],
                ),
            )
        elif kind == "material_added":
            conn.execute(
                """
                insert or replace into materials (id, title, path, url, description, tags, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["title"],
                    payload.get("path"),
                    payload.get("url"),
                    payload.get("description"),
                    dumps(payload.get("tags", [])),
                    payload["created_at"],
                ),
            )
        elif kind == "teammate_added":
            conn.execute(
                "insert or replace into teammates (id, name, aliases, email, role, created_at) values (?, ?, ?, ?, ?, ?)",
                (payload["id"], payload["name"], dumps(payload.get("aliases", [])), payload.get("email"), payload.get("role"), payload["created_at"]),
            )
        elif kind == "event_reverted":
            pass
        elif kind == "form_published":
            conn.execute(
                "insert or replace into forms (id, kind, survey_name, remote_uuid, respondent_url, admin_url, contact_id, recipient, status, created_at) values (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (payload["id"], payload["kind"], payload["survey_name"], payload["remote_uuid"], payload.get("respondent_url"), payload.get("admin_url"), payload.get("contact_id"), payload.get("recipient"), payload["created_at"]),
            )
        elif kind == "form_closed":
            conn.execute("update forms set status = 'closed' where id = ?", (payload["id"],))
        elif kind == "submission_received":
            conn.execute(
                "insert or ignore into submissions (id, form_id, remote_id, answers, status, matched_contact_id, received_at) values (?, ?, ?, ?, 'pending', ?, ?)",
                (payload["id"], payload["form_id"], payload.get("remote_id"), dumps(payload["answers"]), payload.get("matched_contact_id"), payload["received_at"]),
            )
        elif kind == "submission_reviewed":
            conn.execute(
                "update submissions set status = ?, matched_contact_id = coalesce(?, matched_contact_id), reviewed_at = ?, review_note = ? where id = ?",
                (payload["status"], payload.get("matched_contact_id"), payload["reviewed_at"], payload.get("note"), payload["id"]),
            )
        elif kind == "recommendation_imported":
            conn.execute("insert or replace into recommendations (id, contact_id, text, rationale, source_path, status, imported_at) values (?, ?, ?, ?, ?, 'pending', ?)", (payload["id"], payload["contact_id"], payload["text"], payload.get("rationale"), payload["source_path"], payload["imported_at"]))
        elif kind == "recommendation_reviewed":
            conn.execute("update recommendations set status = ?, reviewed_at = ? where id = ?", (payload["status"], payload["reviewed_at"], payload["id"]))
        self._refresh_search_index(conn)
        conn.commit()

    def _read_events(self) -> list[dict[str, Any]]:
        return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(self.events_dir.glob("*.json"))]

    def history(self, contact_ref: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        events = self._read_events()
        if contact_ref:
            contact_id = self.resolve_contact(contact_ref)["id"]
            keys = ("id", "contact_id", "keep_id", "duplicate_id")
            events = [event for event in events if contact_id in {event["payload"].get(key) for key in keys}]
        events.reverse()
        return events[:limit] if limit else events

    def undo(self, event_id: str) -> dict[str, Any]:
        events = self._read_events()
        target = next((event for event in events if event["event_id"] == event_id), None)
        if target is None:
            raise NilesError("unknown_event", f"No event matched '{event_id}'.", {"event_id": event_id})
        if target["type"] == "event_reverted":
            raise NilesError("cannot_undo_revert", "A compensation event cannot itself be undone.")
        if any(event["type"] == "event_reverted" and event["payload"]["target_event_id"] == event_id for event in events):
            raise NilesError("already_reverted", "That event has already been reverted.", {"event_id": event_id})
        reverted_ids = {event["payload"]["target_event_id"] for event in events if event["type"] == "event_reverted"}
        if target["type"] == "contact_created":
            contact_id = target["payload"]["id"]
            dependent = [event["event_id"] for event in events if event["event_id"] not in reverted_ids and event["event_id"] != event_id and contact_id in event["payload"].values()]
            if dependent:
                raise NilesError("undo_has_dependents", "Undo dependent contact events first.", {"dependent_event_ids": dependent})
        event = self.append_event("event_reverted", {"target_event_id": event_id, "target_type": target["type"], "created_at": utc_now()})
        self.rebuild_index()
        return {"reverted_event_id": event_id, "compensation_event_id": event["event_id"], "events_written": 1}

    def add_contact(
        self,
        name: str,
        emails: list[str],
        phones: list[str],
        company: str | None,
        role: str | None,
        traits: dict[str, Any],
        tags: list[str],
        cadence_days: int | None,
    ) -> dict[str, Any]:
        contact_id = new_id("con")
        payload = {
            "id": contact_id,
            "slug": slugify(name),
            "name": name,
            "emails": emails,
            "phones": phones,
            "company": company,
            "role": role,
            "traits": traits,
            "tags": sorted(set(tags)),
            "cadence_days": cadence_days,
            "created_at": utc_now(),
        }
        event = self.append_event("contact_created", payload)
        return {"contact": payload, "events_written": 1, "event_id": event["event_id"]}

    def update_contact(
        self,
        ref: str,
        name: str | None = None,
        company: str | None = None,
        role: str | None = None,
        cadence_days: int | None = None,
        traits: dict[str, Any] | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        add_emails: list[str] | None = None,
        add_phones: list[str] | None = None,
    ) -> dict[str, Any]:
        contact = self.resolve_contact(ref)
        fields: dict[str, Any] = {}
        if name is not None:
            fields["name"] = name
            fields["slug"] = slugify(name)
        if company is not None:
            fields["company"] = company
        if role is not None:
            fields["role"] = role
        if cadence_days is not None:
            fields["cadence_days"] = cadence_days
        payload = {
            "id": contact["id"],
            "fields": fields,
            "traits": traits or {},
            "add_tags": unique(add_tags or []),
            "remove_tags": unique(remove_tags or []),
            "add_emails": unique(add_emails or []),
            "add_phones": unique(add_phones or []),
            "created_at": utc_now(),
        }
        event = self.append_event("contact_updated", payload)
        return {"contact": self.resolve_contact(contact["id"]), "events_written": 1, "event_id": event["event_id"]}

    def archive_contact(self, ref: str, reason: str | None = None) -> dict[str, Any]:
        contact = self.resolve_contact(ref)
        event = self.append_event(
            "contact_archived",
            {"id": contact["id"], "reason": reason, "created_at": utc_now()},
        )
        return {"contact_id": contact["id"], "archived": True, "events_written": 1, "event_id": event["event_id"]}

    def merge_contacts(self, keep_ref: str, duplicate_ref: str, note: str | None = None) -> dict[str, Any]:
        keep = self.resolve_contact(keep_ref)
        duplicate = self.resolve_contact(duplicate_ref)
        if keep["id"] == duplicate["id"]:
            raise NilesError("same_contact", "Cannot merge a contact into itself.", {"id": keep["id"]})
        event = self.append_event(
            "contacts_merged",
            {"keep_id": keep["id"], "duplicate_id": duplicate["id"], "note": note, "created_at": utc_now()},
        )
        return {
            "kept": self.resolve_contact(keep["id"]),
            "duplicate_id": duplicate["id"],
            "events_written": 1,
            "event_id": event["event_id"],
        }

    def resolve_contact(self, ref: str) -> dict[str, Any]:
        with self.connect() as conn:
            rows = conn.execute("select * from contacts where archived = 0").fetchall()
        contacts = [self._contact_from_row(row) for row in rows]
        exact = [c for c in contacts if c["id"] == ref or ref in c["emails"] or c["slug"] == ref]
        if len(exact) == 1:
            return exact[0]
        lowered = ref.lower()
        fuzzy = [
            c
            for c in contacts
            if lowered in c["name"].lower()
            or lowered in (c.get("company") or "").lower()
            or lowered in c["slug"]
        ]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if len(fuzzy) > 1:
            raise NilesError(
                "ambiguous_reference",
                f"Reference '{ref}' matched multiple contacts. Use an exact id or email.",
                {"ref": ref, "candidates": fuzzy},
            )
        raise NilesError("unknown_contact", f"No contact matched '{ref}'.", {"ref": ref, "candidates": []})

    def get_contact(self, ref: str, with_notes: bool = False, with_tasks: bool = False) -> dict[str, Any]:
        contact = self.resolve_contact(ref)
        if with_notes:
            contact["notes"] = self.list_notes(contact["id"])
        if with_tasks:
            contact["tasks"] = self.list_tasks(contact_ref=contact["id"])
        return contact

    def list_contacts(self, tag: str | None = None, stale: bool = False) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from contacts where archived = 0 order by name").fetchall()
            contacts = [self._contact_from_row(row) for row in rows]
            for contact in contacts:
                last = conn.execute(
                    "select max(created_at) as last_touched from notes where contact_id = ?",
                    (contact["id"],),
                ).fetchone()["last_touched"]
                contact["last_touched"] = last
        if tag:
            contacts = [c for c in contacts if tag in c["tags"]]
        if stale:
            now = datetime.now(timezone.utc)
            contacts = [
                contact
                for contact in contacts
                if contact.get("cadence_days")
                and (
                    not contact.get("last_touched")
                    or (now - datetime.fromisoformat(contact["last_touched"].replace("Z", "+00:00"))).days >= contact["cadence_days"]
                )
            ]
        return contacts

    def add_note(self, ref: str, text: str, kind: str, at: str | None) -> dict[str, Any]:
        contact = self.resolve_contact(ref)
        payload = {
            "id": new_id("note"),
            "contact_id": contact["id"],
            "created_at": parse_timestamp(at),
            "kind": kind,
            "text": text,
            "source": "user",
        }
        event = self.append_event("note_created", payload)
        return {"contact": contact, "note": payload, "events_written": 1, "event_id": event["event_id"]}

    def set_contact_status(self, ref: str, status: str, at: str | None = None) -> dict[str, Any]:
        if not status.strip():
            raise NilesError("invalid_status", "Current status cannot be empty.")
        contact = self.resolve_contact(ref)
        note_result = self.add_note(contact["id"], status.strip(), "note", at)
        update_result = self.update_contact(contact["id"], traits={"current_status": status.strip()})
        return {
            "contact": update_result["contact"],
            "status_note": note_result["note"],
            "events_written": 2,
            "event_ids": [note_result["event_id"], update_result["event_id"]],
        }

    def ingest_enrichment(
        self,
        ref: str,
        text: str,
        source_url: str | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        pieces = [text]
        if source_url:
            pieces.append(f"Source: {source_url}")
        if confidence is not None:
            pieces.append(f"Confidence: {confidence}")
        return self.add_note(ref, "\n".join(pieces), "enrichment", None)

    def list_notes(self, ref: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if ref:
            contact = self.resolve_contact(ref)
            where = " where notes.contact_id = ?"
            params.append(contact["id"])
        query = (
            "select notes.*, contacts.name as contact_name from notes "
            "join contacts on notes.contact_id = contacts.id"
            f"{where} order by notes.created_at desc, notes.event_sequence desc, notes.id desc"
        )
        if limit:
            query += " limit ?"
            params.append(limit)
        with self.connect() as conn:
            return [self._note_from_row(row) for row in conn.execute(query, params).fetchall()]

    def add_task(
        self,
        ref: str | None,
        text: str,
        due_date: str | None,
        assignee: str | None,
        tags: list[str],
    ) -> dict[str, Any]:
        contact = self.resolve_contact(ref) if ref else None
        payload = {
            "id": new_id("task"),
            "contact_id": contact["id"] if contact else None,
            "assignee": assignee,
            "due_date": due_date,
            "text": text,
            "status": "open",
            "tags": sorted(set(tags)),
            "source": "user",
            "created_at": utc_now(),
        }
        event = self.append_event("task_created", payload)
        return {"contact": contact, "task": payload, "events_written": 1, "event_id": event["event_id"]}

    def list_tasks(
        self,
        status: str | None = None,
        assignee: str | None = None,
        due: str | None = None,
        contact_ref: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "select tasks.*, contacts.name as contact_name from tasks left join contacts on tasks.contact_id = contacts.id"
        clauses: list[str] = []
        params: list[Any] = []
        if contact_ref:
            contact = self.resolve_contact(contact_ref)
            clauses.append("tasks.contact_id = ?")
            params.append(contact["id"])
        if status:
            clauses.append("tasks.status = ?")
            params.append(status)
        if assignee:
            clauses.append("tasks.assignee = ?")
            params.append(assignee)
        if due:
            target = date.today().isoformat() if due == "today" else due
            clauses.append("tasks.due_date <= ?")
            params.append(target)
        if clauses:
            query += " where " + " and ".join(clauses)
        query += " order by tasks.due_date is null, tasks.due_date, tasks.created_at"
        with self.connect() as conn:
            return [self._task_from_row(row) for row in conn.execute(query, params).fetchall()]

    def done_task(self, task_id: str, note: str | None) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("select id from tasks where id = ?", (task_id,)).fetchone()
        if row is None:
            raise NilesError("unknown_task", f"No task matched '{task_id}'.", {"id": task_id})
        event = self.append_event("task_done", {"id": task_id, "note": note, "created_at": utc_now()})
        return {"task_id": task_id, "status": "done", "events_written": 1, "event_id": event["event_id"]}

    def update_task(
        self,
        task_id: str,
        text: str | None = None,
        due_date: str | None = None,
        assignee: str | None = None,
        status: str | None = None,
        add_tags: list[str] | None = None,
        remove_tags: list[str] | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        with self.connect() as conn:
            self._task_by_id(conn, task_id)
        fields = {
            key: value
            for key, value in {
                "text": text,
                "due_date": due_date,
                "assignee": assignee,
                "status": status,
                "done_note": note,
            }.items()
            if value is not None
        }
        event = self.append_event(
            "task_updated",
            {
                "id": task_id,
                "fields": fields,
                "add_tags": unique(add_tags or []),
                "remove_tags": unique(remove_tags or []),
                "created_at": utc_now(),
            },
        )
        with self.connect() as conn:
            task = self._task_by_id(conn, task_id)
        return {"task": task, "events_written": 1, "event_id": event["event_id"]}

    def suggest_tasks(self, assignee: str | None = None) -> list[dict[str, Any]]:
        suggestions: list[dict[str, Any]] = []
        contacts = self.list_contacts()
        open_tasks = self.list_tasks(status="open")
        contacts_with_open_tasks = {task["contact_id"] for task in open_tasks if task.get("contact_id")}
        for contact in contacts:
            if contact["id"] in contacts_with_open_tasks:
                continue
            notes = self.list_notes(contact["id"], limit=3)
            text = "Follow up"
            for note in notes:
                lowered = note["text"].lower()
                if any(marker in lowered for marker in ("next step", "todo", "follow up", "reach out", "waiting")):
                    text = note["text"].splitlines()[0][:160]
                    break
            if notes or contact.get("cadence_days"):
                suggestions.append(
                    {
                        "contact_id": contact["id"],
                        "contact": contact["name"],
                        "contact_ref": contact["slug"],
                        "suggested_task": text,
                        "assignee": assignee,
                        "reason": "Contact has recent notes or cadence but no open task.",
                        "create_command": " ".join(
                            part
                            for part in [
                                f'niles task add {contact["slug"]} "{text}"',
                                f"--assign {assignee}" if assignee else "",
                            ]
                            if part
                        ),
                    }
                )
        return suggestions

    def set_org_context(self, name: str | None, context: str | None, traits: dict[str, Any]) -> dict[str, Any]:
        current = self.get_org_context()
        payload = {
            "name": name if name is not None else current.get("name"),
            "context": context if context is not None else current.get("context"),
            "traits": {**current.get("traits", {}), **traits},
            "updated_at": utc_now(),
        }
        event = self.append_event("org_context_set", payload)
        return {"org": self.get_org_context(), "events_written": 1, "event_id": event["event_id"]}

    def get_org_context(self) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("select * from org_context where id = 1").fetchone()
        if row is None:
            return {"name": None, "context": None, "traits": {}, "updated_at": None}
        return {
            "name": row["name"],
            "context": row["context"],
            "traits": loads(row["traits"], {}),
            "updated_at": row["updated_at"],
        }

    def add_material(
        self,
        title: str,
        path: str | None = None,
        url: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        if not path and not url:
            raise NilesError("missing_material_location", "Material needs --path or --url.")
        payload = {
            "id": new_id("mat"),
            "title": title,
            "path": path,
            "url": url,
            "description": description,
            "tags": unique(tags or []),
            "created_at": utc_now(),
        }
        event = self.append_event("material_added", payload)
        return {"material": payload, "events_written": 1, "event_id": event["event_id"]}

    def list_materials(self, tag: str | None = None) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from materials order by title").fetchall()
        materials = [self._material_from_row(row) for row in rows]
        if tag:
            materials = [material for material in materials if tag in material["tags"]]
        return materials

    def add_teammate(self, name: str, aliases: list[str], email: str | None, role: str | None) -> dict[str, Any]:
        payload = {"id": new_id("team"), "name": name, "aliases": unique(aliases), "email": email, "role": role, "created_at": utc_now()}
        event = self.append_event("teammate_added", payload)
        return {"teammate": payload, "events_written": 1, "event_id": event["event_id"]}

    def list_teammates(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from teammates order by name").fetchall()
        return [{"id": row["id"], "name": row["name"], "aliases": loads(row["aliases"], []), "email": row["email"], "role": row["role"], "created_at": row["created_at"]} for row in rows]

    def resolve_teammate(self, ref: str) -> dict[str, Any]:
        lowered = ref.lower()
        matches = [item for item in self.list_teammates() if lowered in {item["id"].lower(), item["name"].lower(), item["email"].lower() if item["email"] else "", *(alias.lower() for alias in item["aliases"])}]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise NilesError("ambiguous_reference", f"Reference '{ref}' matched multiple teammates.", {"candidates": matches})
        raise NilesError("unknown_teammate", f"No teammate matched '{ref}'.", {"ref": ref})

    def search(self, terms: str) -> list[dict[str, Any]]:
        query = terms.strip()
        if not query:
            raise NilesError("empty_search", "Search terms cannot be empty.")
        match_query = '"' + query.replace('"', '""') + '"'
        try:
            with self.connect() as conn:
                rows = conn.execute(
                    "select entity_type, entity_id, contact_id, label, bm25(crm_search) as rank from crm_search where crm_search match ? order by rank",
                    (match_query,),
                ).fetchall()
        except sqlite3.OperationalError as exc:
            raise NilesError("invalid_search", f"Invalid search expression: {terms}") from exc
        return [{"type": row["entity_type"], "id": row["entity_id"], "contact_id": row["contact_id"], "label": row["label"], "match": terms, "rank": row["rank"]} for row in rows]

    def export_contacts(self, format_name: str, output: Path | None, tag: str | None = None) -> dict[str, Any]:
        contacts = self.list_contacts(tag=tag)
        rows = [{**contact, "emails": ";".join(contact["emails"]), "phones": ";".join(contact["phones"]), "tags": ";".join(contact["tags"]), "traits": dumps(contact["traits"])} for contact in contacts]
        if format_name == "json":
            content = json.dumps(contacts, indent=2, sort_keys=True) + "\n"
        else:
            fields = ["id", "name", "emails", "phones", "company", "role", "traits", "tags", "cadence_days", "archived", "created_at"]
            stream = io.StringIO()
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            content = stream.getvalue()
        path = None
        if output:
            path = output if output.is_absolute() else self.root / output
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return {"format": format_name, "count": len(contacts), "path": str(path) if path else None, "content": None if path else content}

    def import_csv(self, path: Path, commit: bool = False, mapping_path: Path | None = None) -> dict[str, Any]:
        source = path if path.is_absolute() else self.root / path
        if not source.is_file():
            raise NilesError("import_not_found", f"CSV file not found: {path}")
        with source.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        mapping: dict[str, str] = {}
        if mapping_path:
            mapping_source = mapping_path if mapping_path.is_absolute() else self.root / mapping_path
            if not mapping_source.is_file():
                raise NilesError("mapping_not_found", f"Mapping file not found: {mapping_path}")
            with mapping_source.open("rb") as handle:
                mapping = tomllib.load(handle).get("columns", {})
            allowed = {
                "name", "email", "emails", "company", "role", "tags", "cadence_days",
                "entity_type", "stage", "priority", "current_status", "champion", "connector",
                "deal_value", "expected_mrr", "next_action", "owner", "due_date",
                "last_interaction", "material_title", "material_url",
            }
            if not mapping or any(field not in allowed for field in mapping.values()):
                raise NilesError("invalid_mapping", "[columns] must map CSV columns to supported contact fields.")
            rows = [{mapping.get(key, key): value for key, value in row.items()} for row in rows]
        required = {"name"}
        if not rows or not required.issubset(rows[0]):
            raise NilesError("invalid_csv", "CSV requires a name column.")
        preview = []
        for row_number, row in enumerate(rows, start=2):
            if not (row.get("name") or "").strip():
                raise NilesError("invalid_csv", f"Row {row_number} has no name.")
            traits: dict[str, Any] = {}
            for field in ("entity_type", "stage", "priority", "current_status", "champion", "connector", "deal_value", "expected_mrr"):
                raw = (row.get(field) or "").strip()
                if raw:
                    try:
                        traits[field] = float(raw) if "." in raw else int(raw)
                    except ValueError:
                        traits[field] = raw
            cadence_raw = (row.get("cadence_days") or "").strip()
            try:
                cadence_days = int(cadence_raw) if cadence_raw else None
            except ValueError as exc:
                raise NilesError("invalid_csv", f"Row {row_number} has an invalid cadence_days value.") from exc
            item = {
                "name": row["name"].strip(),
                "emails": [v.strip() for v in (row.get("emails") or row.get("email") or "").split(";") if v.strip()],
                "company": row.get("company") or None,
                "role": row.get("role") or None,
                "tags": [v.strip() for v in (row.get("tags") or "").split(";") if v.strip()],
                "cadence_days": cadence_days,
                "traits": traits,
                "next_action": (row.get("next_action") or "").strip() or None,
                "owner": (row.get("owner") or "").strip() or None,
                "due_date": (row.get("due_date") or "").strip() or None,
                "last_interaction": (row.get("last_interaction") or "").strip() or None,
                "material_title": (row.get("material_title") or "").strip() or None,
                "material_url": (row.get("material_url") or "").strip() or None,
            }
            if item["due_date"]:
                try:
                    date.fromisoformat(item["due_date"])
                except ValueError as exc:
                    raise NilesError("invalid_csv", f"Row {row_number} has an invalid due_date value.") from exc
            if item["last_interaction"]:
                parse_timestamp(item["last_interaction"])
            if bool(item["material_title"]) != bool(item["material_url"]):
                raise NilesError("invalid_csv", f"Row {row_number} must provide both material_title and material_url.")
            preview.append(item)
        event_ids = []
        if commit:
            for item in preview:
                result = self.add_contact(item["name"], item["emails"], [], item["company"], item["role"], item["traits"], item["tags"], item["cadence_days"])
                event_ids.append(result["event_id"])
                contact_id = result["contact"]["id"]
                if item["last_interaction"] and item["traits"].get("current_status"):
                    note_result = self.add_note(contact_id, str(item["traits"]["current_status"]), "note", item["last_interaction"])
                    event_ids.append(note_result["event_id"])
                if item["next_action"]:
                    task_result = self.add_task(contact_id, item["next_action"], item["due_date"], item["owner"], ["imported"])
                    event_ids.append(task_result["event_id"])
                if item["material_url"]:
                    material_result = self.add_material(item["material_title"], url=item["material_url"], tags=[slugify(item["name"]), "imported"])
                    event_ids.append(material_result["event_id"])
        return {"path": str(source), "mapping": mapping, "dry_run": not commit, "count": len(preview), "contacts": preview, "events_written": len(event_ids), "event_ids": event_ids}

    def list_surveys(self) -> list[dict[str, Any]]:
        surveys = []
        for path in sorted((self.state_dir / "surveys").glob("*.json")):
            definition = self._read_survey_path(path)
            surveys.append({"name": definition["name"], "description": definition.get("description"), "version": definition.get("version"), "template": definition.get("template", False), "path": str(path)})
        return surveys

    def _read_survey_path(self, path: Path) -> dict[str, Any]:
        from .surveys import validate_survey

        try:
            definition = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise NilesError("invalid_survey_json", f"Survey is not valid JSON: {path.name}") from exc
        return validate_survey(definition)

    def get_survey(self, name: str) -> dict[str, Any]:
        path = self.state_dir / "surveys" / f"{slugify(name)}.json"
        if not path.is_file():
            raise NilesError("unknown_survey", f"No survey matched '{name}'.")
        return self._read_survey_path(path)

    def copy_survey(self, source_name: str, destination_name: str) -> dict[str, Any]:
        definition = self.get_survey(source_name)
        destination = self.state_dir / "surveys" / f"{slugify(destination_name)}.json"
        if destination.exists():
            raise NilesError("survey_exists", f"Survey '{destination_name}' already exists.")
        definition.update({"name": slugify(destination_name), "template": False, "version": 1})
        destination.write_text(json.dumps(definition, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"survey": definition, "path": str(destination)}

    def run_survey(self, name: str, contact_ref: str | None, answers: dict[str, Any] | None, dry_run: bool) -> dict[str, Any]:
        definition = self.get_survey(name)
        if answers is None:
            return {"survey": definition, "requires_answers": True}
        question_names = {question["name"] for question in definition["questions"]}
        unknown = sorted(set(answers) - question_names)
        if unknown:
            raise NilesError("unknown_answers", "Answers contain unknown question names.", {"questions": unknown})
        missing = [question["name"] for question in definition["questions"] if question.get("required") and not answers.get(question["name"])]
        if missing:
            raise NilesError("missing_answers", "Required answers are missing.", {"questions": missing})
        invalid_choices = [
            question["name"]
            for question in definition["questions"]
            if question.get("type") == "choice"
            and question["name"] in answers
            and answers[question["name"]] not in question.get("options", [])
        ]
        if invalid_choices:
            raise NilesError("invalid_answers", "Choice answers must use a declared option.", {"questions": invalid_choices})
        contact = self.resolve_contact(contact_ref) if contact_ref else None
        previews = []
        routes = definition.get("routes", {})
        for question_name, route in routes.items():
            value = answers.get(question_name)
            if value in (None, ""):
                continue
            action = route["action"]
            mutation = {"action": action, "question": question_name, "value": value}
            if action in {"append_note", "set_field", "set_trait", "create_task", "add_tag", "archive"} and not contact:
                mutation["blocked"] = "contact_required"
            previews.append(mutation)
        if any(item.get("blocked") for item in previews) and not dry_run:
            raise NilesError("contact_required", "This routed survey requires --contact.")
        applied = []
        if not dry_run:
            for item in previews:
                action, value = item["action"], item["value"]
                route = routes[item["question"]]
                if action == "append_note":
                    applied.append(self.add_note(contact["id"], str(value), route.get("kind", "note"), None))
                elif action == "set_trait":
                    applied.append(self.update_contact(contact["id"], traits={route["trait"]: value}))
                elif action == "set_field":
                    field = route.get("field")
                    if field not in {"name", "company", "role", "cadence_days"}:
                        raise NilesError("protected_field", f"Survey cannot set field '{field}'.")
                    applied.append(self.update_contact(contact["id"], **{field: value}))
                elif action == "add_tag":
                    applied.append(self.update_contact(contact["id"], add_tags=[str(value)]))
                elif action == "archive":
                    applied.append(self.archive_contact(contact["id"], reason=f"survey:{name}"))
                elif action == "create_task":
                    due = next((answers.get(q) for q, r in routes.items() if r.get("action") == "task_due" and r.get("binds") == item["question"]), None)
                    owner = next((answers.get(q) for q, r in routes.items() if r.get("action") == "task_assignee" and r.get("binds") == item["question"]), None)
                    applied.append(self.add_task(contact["id"], str(value), due, owner, []))
        return {"survey": name, "contact": contact, "answers": answers, "dry_run": dry_run, "preview": previews, "applied": applied, "events_written": sum(item.get("events_written", 0) for item in applied)}

    def export_survey_edsl(self, name: str, output: Path) -> dict[str, Any]:
        definition = self.get_survey(name)
        survey = self._make_edsl_survey(definition)
        bundle = {
            "schema_version": "niles.edsl-handoff.v1",
            "kind": "survey",
            "survey": survey.to_dict(),
            "routing": definition.get("routes", {}),
            "network": False,
        }
        path = output if output.is_absolute() else self.root / output
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"path": str(path), "schema_version": bundle["schema_version"], "question_count": len(definition["questions"]), "network": False}

    def _make_edsl_survey(self, definition: dict[str, Any]):
        try:
            from edsl import QuestionFreeText, QuestionMultipleChoice, Survey
        except ImportError as exc:
            raise NilesError(
                "edsl_not_installed",
                "EDSL export requires the optional edsl dependency. Install niles[edsl].",
            ) from exc
        questions = []
        for question in definition["questions"]:
            kwargs = {"question_name": question["name"], "question_text": question["text"]}
            if question.get("type") == "choice":
                questions.append(QuestionMultipleChoice(**kwargs, question_options=question["options"]))
            else:
                questions.append(QuestionFreeText(**kwargs))
        return Survey(questions=questions, name=definition["name"])

    def _exchange_path(self, filename: str) -> Path:
        niles_gitignore = self.state_dir / ".gitignore"
        ignored = niles_gitignore.read_text(encoding="utf-8") if niles_gitignore.exists() else ""
        lines = ignored.splitlines()
        if "exchange/" not in lines:
            niles_gitignore.write_text(ignored + ("" if not ignored or ignored.endswith("\n") else "\n") + "exchange/\n", encoding="utf-8")
        self.exchange_dir.mkdir(parents=True, exist_ok=True)
        return self.exchange_dir / filename

    def export_human_update(self, output: Path) -> dict[str, Any]:
        """Build an offline EDSL survey that asks for updates on every entity."""
        try:
            from edsl import QuestionFreeText, QuestionMultipleChoice, Survey
        except ImportError as exc:
            raise NilesError(
                "edsl_not_installed",
                "Human update export requires the optional edsl dependency. Install niles[edsl].",
            ) from exc

        contacts = sorted(self.list_contacts(), key=lambda item: item["name"].casefold())
        if not contacts:
            raise NilesError("no_entities", "Add at least one contact or organization before exporting a human update.")

        questions = []
        routing: dict[str, dict[str, str]] = {}
        for contact in contacts:
            notes = self.list_notes(contact["id"], limit=1)
            status = str(contact.get("traits", {}).get("current_status") or (notes[0]["text"] if notes else "No status recorded"))
            prefix = contact["id"].replace("-", "_")
            changed_name = f"changed_{prefix}"
            notes_name = f"notes_{prefix}"
            changed = QuestionMultipleChoice(
                question_name=changed_name,
                question_text=f"{contact['name']}\n\nCurrent status: {status}\n\nHas anything changed?",
                question_options=["No change", "Update"],
            )
            update = QuestionFreeText(
                question_name=notes_name,
                question_text=f"What changed for {contact['name']}? Add the status update or notes.",
            )
            questions.extend([changed, update])
            routing[notes_name] = {"contact_id": contact["id"], "contact_name": contact["name"], "action": "append_note"}

        survey = Survey(questions=questions, name="niles-human-update")
        for contact in contacts:
            prefix = contact["id"].replace("-", "_")
            survey = survey.add_skip_rule(
                f"notes_{prefix}",
                f"{{{{ changed_{prefix}.answer }}}} == 'No change'",
            )

        path = output if output.is_absolute() else self.root / output
        path.parent.mkdir(parents=True, exist_ok=True)
        survey.save(str(path))
        manifest_path = path.with_suffix(path.suffix + ".manifest.json")
        manifest = {
            "schema_version": "niles.human-update.v1",
            "survey_path": str(path),
            "entities": len(contacts),
            "routing": routing,
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        publish_command = f"ep humanize create --survey {shlex.quote(str(path))} --name {shlex.quote('Niles status update')}"
        return {
            "path": str(path),
            "manifest_path": str(manifest_path),
            "entities": len(contacts),
            "question_count": len(questions),
            "network": False,
            "publish_command": publish_command,
            "next_command": publish_command,
        }

    def export_form(self, kind: str, survey_name: str, output: Path | None = None) -> dict[str, Any]:
        definition = self.get_survey(survey_name)
        if kind == "intake":
            forbidden = [route["action"] for route in definition.get("routes", {}).values() if route["action"] in {"archive", "set_field"}]
            if forbidden:
                raise NilesError("unsafe_intake_route", "Intake surveys cannot archive contacts or set protected fields.")
        survey = self._make_edsl_survey(definition)
        managed = output is None
        path = self._exchange_path(f"{slugify(kind)}-{slugify(survey_name)}.survey.ep") if managed else (output if output.is_absolute() else self.root / output)
        path.parent.mkdir(parents=True, exist_ok=True)
        survey.save(str(path))
        registration_path = self._exchange_path(f"{slugify(kind)}-{slugify(survey_name)}.registration.json")
        publish_command = (
            f"ep humanize create --survey {shlex.quote(str(path))} "
            f"--name {shlex.quote(f'Niles {kind}: {survey_name}')} > {shlex.quote(str(registration_path))}"
        )
        return {
            "kind": kind,
            "survey": survey_name,
            "path": str(path),
            "registration_path": str(registration_path),
            "managed": managed,
            "network": False,
            "publish_command": publish_command,
            "next_command": publish_command,
        }

    def register_form(
        self,
        kind: str,
        survey_name: str,
        registration_path: Path | None = None,
        contact_ref: str | None = None,
        recipient: str | None = None,
    ) -> dict[str, Any]:
        self.get_survey(survey_name)
        contact = self.resolve_contact(contact_ref) if contact_ref else None
        path = self._exchange_path(f"{slugify(kind)}-{slugify(survey_name)}.registration.json") if registration_path is None else (registration_path if registration_path.is_absolute() else self.root / registration_path)
        try:
            registration = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NilesError("invalid_registration", f"Could not read EP registration: {path}") from exc
        if registration.get("status") == "ok" and isinstance(registration.get("data"), dict):
            registration = registration["data"]
        remote_uuid = registration.get("human_survey_uuid") or registration.get("uuid")
        if not remote_uuid:
            raise NilesError("invalid_registration", "EP registration has no human survey UUID.")
        payload = {
            "id": new_id("form"), "kind": kind, "survey_name": survey_name,
            "remote_uuid": str(remote_uuid), "respondent_url": registration.get("respondent_url"),
            "admin_url": registration.get("admin_url"), "contact_id": contact["id"] if contact else None,
            "recipient": recipient, "created_at": utc_now(),
        }
        event = self.append_event("form_published", payload)
        responses_path = self._exchange_path(f"{slugify(kind)}-{slugify(str(remote_uuid))}.responses.json")
        pull_command = f"ep humanize responses {shlex.quote(str(remote_uuid))} --output {shlex.quote(str(responses_path))}"
        return {"form": payload, "registration_path": str(path), "responses_path": str(responses_path), "pull_command": pull_command, "next_command": pull_command, "events_written": 1, "event_id": event["event_id"]}

    def list_forms(self, kind: str | None = None) -> list[dict[str, Any]]:
        query, params = "select * from forms", []
        if kind:
            query, params = query + " where kind = ?", [kind]
        query += " order by created_at desc"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def resolve_form(self, ref: str, kind: str | None = None) -> dict[str, Any]:
        forms = self.list_forms(kind)
        matches = [form for form in forms if ref in {form["id"], form["remote_uuid"]}]
        if len(matches) == 1:
            return matches[0]
        raise NilesError("unknown_form", f"No form matched '{ref}'.", {"ref": ref})

    def import_form(self, form_ref: str, kind: str, response_file: Path | None = None) -> dict[str, Any]:
        form = self.resolve_form(form_ref, kind)
        path = self._exchange_path(f"{slugify(kind)}-{slugify(form['remote_uuid'])}.responses.json") if response_file is None else (response_file if response_file.is_absolute() else self.root / response_file)
        try:
            if path.suffix == ".ep":
                from edsl import Results

                raw = Results.load(str(path)).to_dict()
            else:
                raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise NilesError("invalid_responses", f"Could not read EP responses: {path}") from exc
        records = raw.get("data", raw.get("responses", [])) if isinstance(raw, dict) else raw
        if not isinstance(records, list):
            raise NilesError("invalid_responses", "Responses must contain a data or responses list.")
        written, skipped = [], 0
        for record in records:
            answers = record.get("answer", record.get("answers", record)) if isinstance(record, dict) else None
            if not isinstance(answers, dict):
                continue
            digest = hashlib.sha256(dumps(answers).encode()).hexdigest()[:24]
            remote_id = str(record.get("id") or record.get("response_id") or digest)
            with self.connect() as conn:
                exists = conn.execute("select 1 from submissions where form_id = ? and remote_id = ?", (form["id"], remote_id)).fetchone()
            if exists:
                skipped += 1
                continue
            matched = None
            email = answers.get("email")
            if email:
                with self.connect() as conn:
                    row = conn.execute("select * from contacts where emails like ? and archived = 0", (f'%"{email}"%',)).fetchone()
                matched = row["id"] if row else None
            payload = {"id": new_id("sub"), "form_id": form["id"], "remote_id": remote_id, "answers": answers, "matched_contact_id": matched, "received_at": utc_now()}
            written.append(self.append_event("submission_received", payload)["event_id"])
        return {"form": form, "responses_path": str(path), "received": len(written), "skipped": skipped, "event_ids": written, "quarantined": True, "network": False}

    def list_submissions(self, kind: str, status: str = "pending") -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select submissions.*, forms.kind, forms.survey_name, forms.contact_id from submissions join forms on submissions.form_id = forms.id where forms.kind = ? and submissions.status = ? order by received_at", (kind, status)).fetchall()
        return [{**dict(row), "answers": loads(row["answers"], {})} for row in rows]

    def review_submission(self, submission_id: str, kind: str, decision: str, merge_ref: str | None = None, note: str | None = None) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("select submissions.*, forms.kind, forms.survey_name, forms.contact_id from submissions join forms on submissions.form_id = forms.id where submissions.id = ?", (submission_id,)).fetchone()
        if row is None or row["kind"] != kind:
            raise NilesError("unknown_submission", f"No {kind} submission matched '{submission_id}'.")
        if row["status"] != "pending":
            raise NilesError("submission_reviewed", "Submission has already been reviewed.")
        answers, applied, matched = loads(row["answers"], {}), [], row["matched_contact_id"]
        if decision == "reject":
            final_status = "rejected"
        elif kind == "intake":
            if merge_ref or matched:
                contact = self.resolve_contact(merge_ref or matched)
                matched, final_status = contact["id"], "merged"
            else:
                result = self.add_contact(str(answers.get("name") or "Unnamed contact"), [answers["email"]] if answers.get("email") else [], [], answers.get("company"), None, {}, ["intake"], None)
                matched, final_status = result["contact"]["id"], "accepted"
                applied.append(result)
            applied.append(self.run_survey(row["survey_name"], matched, answers, False))
        else:
            matched = row["contact_id"]
            if not matched:
                raise NilesError("contact_required", "Status submission has no registered contact.")
            applied.append(self.run_survey(row["survey_name"], matched, answers, False))
            final_status = "accepted"
        review_event = self.append_event("submission_reviewed", {"id": submission_id, "status": final_status, "matched_contact_id": matched, "reviewed_at": utc_now(), "note": note})
        return {"submission_id": submission_id, "status": final_status, "matched_contact_id": matched, "applied": applied, "review_event_id": review_event["event_id"]}

    def close_form(self, form_ref: str, kind: str) -> dict[str, Any]:
        form = self.resolve_form(form_ref, kind)
        event = self.append_event("form_closed", {"id": form["id"], "created_at": utc_now()})
        return {"form_id": form["id"], "status": "closed", "remote_closed": False, "events_written": 1, "event_id": event["event_id"]}

    def export_recommendation_job(self, name: str, output: Path | None = None, tag: str | None = None) -> dict[str, Any]:
        try:
            from edsl import QuestionFreeText, Scenario, ScenarioList, Survey
        except ImportError as exc:
            raise NilesError("edsl_not_installed", "Recommendation export requires niles[edsl].") from exc
        contacts = self.list_contacts(tag=tag)
        scenarios = []
        for contact in contacts:
            scenarios.append({"contact_id": contact["id"], "name": contact["name"], "company": contact.get("company"), "traits": contact["traits"], "recent_notes": [note["text"] for note in self.list_notes(contact["id"], limit=5)], "open_tasks": [task["text"] for task in self.list_tasks(status="open", contact_ref=contact["id"])]})
        questions = [
            QuestionFreeText(question_name="recommended_task", question_text="Recommend the single best next relationship action for {{ name }} at {{ company }}. Context: traits={{ traits }}, recent notes={{ recent_notes }}, open tasks={{ open_tasks }}."),
            QuestionFreeText(question_name="rationale", question_text="Briefly explain why this is the best next action for {{ name }}."),
        ]
        job = Survey(questions, name=name).by(ScenarioList([Scenario(item) for item in scenarios]))
        managed = output is None
        path = self._exchange_path(f"recommend-{slugify(name)}.jobs.ep") if managed else (output if output.is_absolute() else self.root / output)
        path.parent.mkdir(parents=True, exist_ok=True)
        job.save(str(path))
        manifest = path.with_suffix(path.suffix + ".manifest.json")
        manifest.write_text(json.dumps({"schema_version": "niles.recommend-job.v1", "name": name, "job_path": str(path), "contact_ids": [item["contact_id"] for item in scenarios], "created_at": utc_now()}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        results_path = self._exchange_path(f"recommend-{slugify(name)}.results.json")
        run_command = f"ep run {shlex.quote(str(path))} --output {shlex.quote(str(results_path))}"
        return {"path": str(path), "manifest": str(manifest), "results_path": str(results_path), "contacts": len(scenarios), "managed": managed, "run_command": run_command, "next_command": run_command, "network": False}

    def import_recommendations(self, results_path: Path | None = None, name: str = "next-steps") -> dict[str, Any]:
        path = self._exchange_path(f"recommend-{slugify(name)}.results.json") if results_path is None else (results_path if results_path.is_absolute() else self.root / results_path)
        try:
            if path.suffix == ".ep":
                from edsl import Results
                raw = Results.load(str(path)).to_dict()
            else:
                raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise NilesError("invalid_recommendation_results", f"Could not read recommendation results: {path}") from exc
        records = raw.get("data", [])
        imported = []
        for record in records:
            scenario, answer = record.get("scenario", {}), record.get("answer", {})
            contact_id, task = scenario.get("contact_id"), answer.get("recommended_task")
            if not contact_id or not task:
                continue
            with self.connect() as conn:
                self._contact_by_id(conn, contact_id)
            payload = {"id": new_id("rec"), "contact_id": contact_id, "text": str(task), "rationale": answer.get("rationale"), "source_path": str(path), "imported_at": utc_now()}
            self.append_event("recommendation_imported", payload)
            imported.append(payload["id"])
        return {"path": str(path), "imported": len(imported), "recommendation_ids": imported, "quarantined": True}

    def list_recommendations(self, status: str = "pending") -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("select recommendations.*, contacts.name as contact from recommendations join contacts on recommendations.contact_id = contacts.id where recommendations.status = ? order by imported_at", (status,)).fetchall()]

    def review_recommendation(self, recommendation_id: str, accept: bool, assignee: str | None = None, due: str | None = None) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute("select * from recommendations where id = ?", (recommendation_id,)).fetchone()
        if row is None:
            raise NilesError("unknown_recommendation", f"No recommendation matched '{recommendation_id}'.")
        if row["status"] != "pending":
            raise NilesError("recommendation_reviewed", "Recommendation has already been reviewed.")
        task = self.add_task(row["contact_id"], row["text"], due, assignee, ["recommendation"]) if accept else None
        status = "accepted" if accept else "rejected"
        event = self.append_event("recommendation_reviewed", {"id": recommendation_id, "status": status, "reviewed_at": utc_now(), "task_event_id": task.get("event_id") if task else None, "source_path": row["source_path"]})
        return {"recommendation_id": recommendation_id, "status": status, "task": task, "event_id": event["event_id"]}

    def render_status_html(self, output: Path) -> dict[str, Any]:
        out = output.expanduser()
        if not out.is_absolute():
            out = self.root / out
        out.parent.mkdir(parents=True, exist_ok=True)
        contacts = self.list_contacts()
        notes = self.list_notes()
        tasks = self.list_tasks(status="open")
        org = self.get_org_context()
        materials = self.list_materials()
        html = build_status_html(self.root.name, self.counts(), org, contacts, notes, tasks, materials)
        out.write_text(html, encoding="utf-8")
        return {"path": str(out), "counts": self.counts()}

    def counts(self) -> dict[str, Any]:
        with self.connect() as conn:
            return {
                "contacts": conn.execute("select count(*) from contacts").fetchone()[0],
                "active_contacts": conn.execute("select count(*) from contacts where archived = 0").fetchone()[0],
                "archived_contacts": conn.execute("select count(*) from contacts where archived = 1").fetchone()[0],
                "notes": conn.execute("select count(*) from notes").fetchone()[0],
                "open_tasks": conn.execute("select count(*) from tasks where status = 'open'").fetchone()[0],
                "done_tasks": conn.execute("select count(*) from tasks where status = 'done'").fetchone()[0],
                "materials": conn.execute("select count(*) from materials").fetchone()[0],
                "teammates": conn.execute("select count(*) from teammates").fetchone()[0],
                "pending_intake": conn.execute("select count(*) from submissions join forms on submissions.form_id = forms.id where submissions.status = 'pending' and forms.kind = 'intake'").fetchone()[0],
                "pending_status_updates": conn.execute("select count(*) from submissions join forms on submissions.form_id = forms.id where submissions.status = 'pending' and forms.kind = 'status-request'").fetchone()[0],
                "pending_recommendations": conn.execute("select count(*) from recommendations where status = 'pending'").fetchone()[0],
            }

    def _contact_by_id(self, conn: sqlite3.Connection, contact_id: str) -> dict[str, Any]:
        row = conn.execute("select * from contacts where id = ?", (contact_id,)).fetchone()
        if row is None:
            raise NilesError("unknown_contact", f"No contact matched '{contact_id}'.", {"ref": contact_id})
        return self._contact_from_row(row)

    def _task_by_id(self, conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
        row = conn.execute(
            "select tasks.*, contacts.name as contact_name from tasks left join contacts on tasks.contact_id = contacts.id where tasks.id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise NilesError("unknown_task", f"No task matched '{task_id}'.", {"id": task_id})
        return self._task_from_row(row)

    def _contact_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "slug": row["slug"],
            "name": row["name"],
            "emails": loads(row["emails"], []),
            "phones": loads(row["phones"], []),
            "company": row["company"],
            "role": row["role"],
            "traits": loads(row["traits"], {}),
            "tags": loads(row["tags"], []),
            "cadence_days": row["cadence_days"],
            "archived": bool(row["archived"]),
            "created_at": row["created_at"],
        }

    def _note_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "contact_id": row["contact_id"],
            "contact": row["contact_name"],
            "created_at": row["created_at"],
            "event_sequence": row["event_sequence"],
            "kind": row["kind"],
            "text": row["text"],
            "source": row["source"],
        }

    def _task_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "contact_id": row["contact_id"],
            "contact": row["contact_name"],
            "assignee": row["assignee"],
            "due_date": row["due_date"],
            "text": row["text"],
            "status": row["status"],
            "tags": loads(row["tags"], []),
            "source": row["source"],
            "created_at": row["created_at"],
            "done_note": row["done_note"],
        }

    def _material_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "path": row["path"],
            "url": row["url"],
            "description": row["description"],
            "tags": loads(row["tags"], []),
            "created_at": row["created_at"],
        }


def validate_archive_member(name: str) -> None:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise NilesError("invalid_archive", f"Unsafe archive path: {name}", {"member": name})
    if not name.startswith(".niles/"):
        raise NilesError("invalid_archive", f"Unexpected archive path: {name}", {"member": name})
    if name.startswith(".niles/index/"):
        raise NilesError("invalid_archive", "Archive must not contain derived index files.", {"member": name})


def build_status_html(
    project_name: str,
    counts: dict[str, Any],
    org: dict[str, Any],
    contacts: list[dict[str, Any]],
    notes: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    materials: list[dict[str, Any]],
) -> str:
    generated_at = utc_now()
    org_name = org.get("name") or project_name
    org_context = org.get("context") or "No organization context has been set."
    org_traits = org.get("traits") or {}
    today = datetime.now(timezone.utc).date()
    pipeline_stages = ("target", "engaged", "demo", "pilot", "contracting", "won", "stalled", "lost")
    stage_rank = {stage: position for position, stage in enumerate(("contracting", "pilot", "demo", "engaged", "target", "stalled", "won", "lost"))}

    def date_label(value: str | None) -> str:
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            return value[:10]

    def badge(value: Any, css_class: str = "") -> str:
        if value in (None, ""):
            return '<span class="muted">-</span>'
        classes = f"badge {css_class}".strip()
        return f'<span class="{classes}">{escape(str(value))}</span>'

    def tag_list(tags: list[str]) -> str:
        if not tags:
            return '<span class="muted">-</span>'
        return "".join(badge(tag, "tag") for tag in tags)

    def empty_row(columns: int, text: str) -> str:
        return f'<tr><td class="empty-cell" colspan="{columns}">{escape(text)}</td></tr>'

    notes_by_contact: dict[str, list[dict[str, Any]]] = {}
    tasks_by_contact: dict[str, list[dict[str, Any]]] = {}
    for note in notes:
        notes_by_contact.setdefault(note["contact_id"], []).append(note)
    for task in tasks:
        if task.get("contact_id"):
            tasks_by_contact.setdefault(task["contact_id"], []).append(task)

    def contact_stage(contact: dict[str, Any]) -> str:
        explicit = str(contact.get("traits", {}).get("stage") or "").strip().lower()
        if explicit in pipeline_stages:
            return explicit
        tags = {str(tag).lower() for tag in contact.get("tags", [])}
        return next((stage for stage in pipeline_stages if stage in tags), "unspecified")

    def latest_note(contact: dict[str, Any]) -> dict[str, Any] | None:
        contact_notes = notes_by_contact.get(contact["id"], [])
        return contact_notes[0] if contact_notes else None

    def next_task(contact: dict[str, Any]) -> dict[str, Any] | None:
        contact_tasks = tasks_by_contact.get(contact["id"], [])
        return contact_tasks[0] if contact_tasks else None

    def overdue(task: dict[str, Any]) -> bool:
        try:
            return bool(task.get("due_date") and date.fromisoformat(task["due_date"]) < today)
        except ValueError:
            return False

    def format_currency(value: float) -> str:
        return "$0" if value == 0 else f"${value:,.0f}"

    def numeric_trait(contact: dict[str, Any], field: str) -> float:
        try:
            return float(contact.get("traits", {}).get(field) or 0)
        except (TypeError, ValueError):
            return 0

    def entity_type(contact: dict[str, Any]) -> str:
        declared = str(contact.get("traits", {}).get("entity_type") or "").strip().lower()
        tags = {str(tag).strip().lower() for tag in contact.get("tags", [])}
        if declared in {"person", "individual"} or tags & {"person", "individual"}:
            return "person"
        if declared in {"organization", "organisation", "company", "account"} or tags & {"organization", "organisation", "company", "account"}:
            return "organization"
        if contact.get("company"):
            return "person"
        if contact_stage(contact) != "unspecified" or tags & {"prospect", "target", "lead", "customer", "client"}:
            return "organization"
        return "ambiguous"

    people = [contact for contact in contacts if entity_type(contact) == "person"]
    organizations = [contact for contact in contacts if entity_type(contact) == "organization"]
    ambiguous_contacts = [contact for contact in contacts if entity_type(contact) == "ambiguous"]
    accounts = [
        contact for contact in organizations
        if contact_stage(contact) != "unspecified"
        or {str(tag).lower() for tag in contact.get("tags", [])} & {"prospect", "target", "lead", "customer", "client", "lost", "dead"}
    ]
    people_by_company: dict[str, list[dict[str, Any]]] = {}
    for person in people:
        people_by_company.setdefault(str(person["company"]).casefold(), []).append(person)

    def relationship_people(account: dict[str, Any]) -> str:
        related = people_by_company.get(account["name"].casefold(), [])
        if not related:
            return '<span class="muted">No mapped people</span>'
        return "<br>".join(
            f"<strong>{escape(person['name'])}</strong>"
            + (f" <span class=\"subtle\">{escape(person.get('role') or ', '.join(person.get('tags', [])) or 'role missing')}</span>" )
            for person in related
        )

    network_rows = "\n".join(
        "<tr>"
        f"<td><strong>{escape(person['name'])}</strong></td>"
        f"<td>{escape(person.get('company') or 'Unaffiliated')}</td>"
        f"<td>{escape(person.get('role') or ', '.join(person.get('tags', [])) or 'Role missing')}</td>"
        f"<td>{escape(date_label((latest_note(person) or {}).get('created_at')) or 'No dated interaction')}</td>"
        f"<td>{escape((next_task(person) or {}).get('text') or 'No next action')}</td>"
        "</tr>"
        for person in sorted(people, key=lambda item: item["name"].lower())
    ) or empty_row(5, "No people in the relationship network.")

    inactive_accounts = [account for account in accounts if contact_stage(account) in {"lost", "won"} or {"lost", "dead"} & {str(tag).lower() for tag in account.get("tags", [])}]
    active_accounts = [account for account in accounts if account not in inactive_accounts]
    active_accounts.sort(key=lambda account: (stage_rank.get(contact_stage(account), 99), priority_sort_key(account.get("traits", {}).get("priority")), account["name"].lower()))

    def pipeline_row(account: dict[str, Any]) -> str:
        note = latest_note(account)
        task = next_task(account)
        status = account.get("traits", {}).get("current_status") or (note.get("text") if note else "No status recorded")
        search_text = " ".join(
            str(value) for value in (
                account["name"], contact_stage(account), status,
                task.get("text") if task else "", task.get("assignee") if task else "",
                " ".join(person["name"] for person in people_by_company.get(account["name"].casefold(), [])),
            ) if value
        ).lower()
        return (
            f'<tr class="pipeline-row" data-account="{escape(account["id"], quote=True)}" data-stage="{escape(contact_stage(account), quote=True)}" data-search="{escape(search_text, quote=True)}">'
            f"<td><strong>{escape(account['name'])}</strong></td>"
            f"<td>{badge(contact_stage(account), 'stage')}</td>"
            f"<td>{badge(account.get('traits', {}).get('priority'), 'priority')}</td>"
            f"<td>{relationship_people(account)}</td>"
            f"<td>{escape(date_label(note.get('created_at')) if note else 'No dated interaction')}</td>"
            f"<td>{escape(str(status))}</td>"
            f"<td>{escape(task.get('text') if task else 'No next action')}<div class=\"subtle\">{escape(task.get('assignee') or 'Unassigned') if task else ''}{' · ' + escape(date_label(task.get('due_date')) or 'No due date') if task else ''}</div></td>"
            "</tr>"
        )

    pipeline_rows = "\n".join(pipeline_row(account) for account in active_accounts) or empty_row(7, "No active accounts.")
    inactive_rows = "\n".join(pipeline_row(account) for account in inactive_accounts) or empty_row(7, "No won, lost, or dead accounts.")
    revenue_accounts = [account for account in active_accounts if contact_stage(account) in {"contracting", "pilot", "demo"}]
    revenue_rows = "\n".join(pipeline_row(account) for account in revenue_accounts) or empty_row(7, "No accounts are explicitly staged as demo, pilot, or contracting.")
    stalled_accounts = [
        account for account in active_accounts
        if contact_stage(account) == "stalled"
        or "waiting" in str(account.get("traits", {}).get("current_status") or (latest_note(account) or {}).get("text") or "").lower()
    ]
    stalled_rows = "\n".join(pipeline_row(account) for account in stalled_accounts) or empty_row(7, "No explicitly stalled or waiting accounts.")
    stage_metrics_rows = "\n".join(
        "<tr>"
        f"<td>{badge(stage, 'stage')}</td>"
        f"<td>{len(stage_accounts)}</td>"
        f"<td>{escape(format_currency(sum(numeric_trait(account, 'deal_value') for account in stage_accounts)))}</td>"
        f"<td>{escape(format_currency(sum(numeric_trait(account, 'expected_mrr') for account in stage_accounts)))}</td>"
        "</tr>"
        for stage in pipeline_stages
        if (stage_accounts := [account for account in accounts if contact_stage(account) == stage])
    ) or empty_row(4, "No explicit pipeline stages or commercial values.")
    warm_accounts = [account for account in active_accounts if contact_stage(account) == "target" and account.get("traits", {}).get("connector")]
    warm_rows = "\n".join(
        "<tr>"
        f"<td><strong>{escape(account['name'])}</strong></td>"
        f"<td>{escape(str(account.get('traits', {}).get('connector')))}</td>"
        f"<td>{escape(str(account.get('traits', {}).get('current_status') or (latest_note(account) or {}).get('text') or 'No status recorded'))}</td>"
        f"<td>{escape((next_task(account) or {}).get('text') or 'No next action')}</td>"
        "</tr>"
        for account in warm_accounts
    ) or empty_row(4, "No target accounts have an explicit connector.")

    owner_groups: dict[str, list[dict[str, Any]]] = {}
    week_end = today + timedelta(days=7)
    for task in tasks:
        try:
            due_this_week = bool(task.get("due_date") and date.fromisoformat(task["due_date"]) <= week_end)
        except ValueError:
            due_this_week = False
        if due_this_week:
            owner_groups.setdefault(task.get("assignee") or "Unassigned", []).append(task)
    owner_sections = "\n".join(
        f'<div class="owner-group" data-search="{escape((owner + " " + " ".join((task.get("text") or "") + " " + (task.get("contact") or "") for task in owner_tasks)).lower(), quote=True)}"><h3>{escape(owner)}</h3><ul>'
        + "".join(
            f'<li class="{("overdue" if overdue(task) else "")}"><strong>{escape(task["text"])}</strong> — {escape(task.get("contact") or "No contact")} <span class="subtle">{escape(date_label(task.get("due_date")) or "No due date")}</span></li>'
            for task in owner_tasks
        )
        + "</ul></div>"
        for owner, owner_tasks in sorted(owner_groups.items())
    ) or '<p class="empty-cell">No actions are due in the next seven days. Add explicit due dates for actions currently stored only in notes.</p>'

    warning_items: list[str] = []
    for account in active_accounts:
        stage = contact_stage(account)
        task = next_task(account)
        if stage == "unspecified":
            warning_items.append(f"{account['name']}: pipeline stage missing")
        if task is None:
            warning_items.append(f"{account['name']}: next action missing")
        elif not task.get("assignee") or not task.get("due_date"):
            missing = "owner and due date" if not task.get("assignee") and not task.get("due_date") else ("owner" if not task.get("assignee") else "due date")
            warning_items.append(f"{account['name']}: next action is missing {missing}")
        if not people_by_company.get(account["name"].casefold()):
            warning_items.append(f"{account['name']}: no people mapped to this account")
    for person in people:
        if not person.get("role"):
            warning_items.append(f"{person['name']}: relationship role missing")
        if len(person["name"].split()) == 1:
            warning_items.append(f"{person['name']}: possibly incomplete or ambiguous person name")
    for contact in ambiguous_contacts:
        warning_items.append(f"{contact['name']}: entity type is ambiguous; tag as person or company")
    warnings_html = "".join(f"<li>{escape(item)}</li>" for item in warning_items) or "<li>No structural warnings detected.</li>"

    history_blocks = "\n".join(
        f'<details><summary><strong>{escape(account["name"])}</strong> — {len(notes_by_contact.get(account["id"], []))} notes</summary><ol>'
        + "".join(f'<li><span class="subtle">{escape(date_label(note["created_at"]))}</span> {escape(note["text"])}</li>' for note in notes_by_contact.get(account["id"], []))
        + "</ol></details>"
        for account in accounts
        if notes_by_contact.get(account["id"])
    ) or '<p class="empty-cell">No account history yet.</p>'

    material_rows = "\n".join(
        "<tr>"
        f"<td><strong>{escape(material['title'])}</strong><div class=\"subtle\">{escape(material.get('description') or '')}</div></td>"
        f"<td>{material_link(material)}</td>"
        f"<td>{tag_list(material.get('tags', []))}</td>"
        f"<td>{escape(date_label(material.get('created_at')))}</td>"
        "</tr>"
        for material in materials
    ) or empty_row(4, "No materials yet.")

    trait_items = "\n".join(
        f"<dt>{escape(str(key))}</dt><dd>{escape(str(value))}</dd>"
        for key, value in sorted(org_traits.items())
    )
    trait_block = f'<dl class="trait-list">{trait_items}</dl>' if trait_items else '<p class="muted">No organization traits set.</p>'

    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Niles Status - {escape(org_name)}</title>
    <style>
      :root {{
        --ep-green: #428a5f;
        --ep-green-light: #5ba97a;
        --ep-green-soft: rgba(66, 138, 95, 0.10);
        --ep-dark: #1a1a1a;
        --ep-gray: #666666;
        --ep-light-gray: #f5f5f5;
        --ep-border: #e0e0e0;
        --ep-amber: #b26b2a;
        --font-serif: Georgia, 'Times New Roman', serif;
        --font-sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
        --font-mono: 'SF Mono', Consolas, 'Liberation Mono', Menlo, monospace;
      }}
      * {{ box-sizing: border-box; }}
      body {{ margin: 0; font-family: var(--font-sans); color: var(--ep-dark); background: #fff; line-height: 1.5; }}
      .shell {{ max-width: 1180px; margin: 0 auto; padding: 24px 24px 52px; }}
      header {{ border-bottom: 3px solid var(--ep-green); padding-bottom: 18px; margin-bottom: 24px; }}
      .brand-row {{ display: flex; justify-content: space-between; align-items: baseline; gap: 16px; margin-bottom: 18px; }}
      .brand {{ font-family: var(--font-serif); color: var(--ep-green); font-size: 0.95rem; white-space: nowrap; }}
      .generated {{ color: var(--ep-gray); font-size: 0.78rem; }}
      h1 {{ margin: 0 0 10px; font-family: var(--font-serif); font-size: 3.6rem; line-height: 0.98; letter-spacing: 0; }}
      h2 {{ margin: 0; font-family: var(--font-serif); font-size: 1.2rem; color: var(--ep-green); }}
      p {{ margin: 0; color: var(--ep-gray); max-width: 820px; }}
      .lede {{ font-size: 1rem; }}
      .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 10px; margin-top: 20px; }}
      .stat {{ border: 1px solid var(--ep-border); border-radius: 8px; padding: 13px 14px; background: var(--ep-light-gray); min-height: 82px; }}
      .value {{ display: block; font-size: 2rem; line-height: 1.05; font-weight: 750; color: var(--ep-dark); }}
      .label {{ display: block; margin-top: 6px; color: var(--ep-gray); font-size: 0.82rem; }}
      .section {{ margin-top: 28px; }}
      .section-head {{ display: flex; justify-content: space-between; align-items: baseline; gap: 16px; border-bottom: 1px solid var(--ep-border); padding-bottom: 7px; margin-bottom: 10px; }}
      .section-note {{ color: var(--ep-gray); font-size: 0.78rem; }}
      .grid-2 {{ display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(260px, 0.8fr); gap: 18px; align-items: start; }}
      .panel {{ border: 1px solid var(--ep-border); border-radius: 8px; padding: 14px; background: #fff; }}
      .panel h3 {{ margin: 0 0 8px; font-size: 0.88rem; text-transform: uppercase; letter-spacing: 0; color: var(--ep-gray); }}
      .panel .trait-list, .panel .muted {{ margin-top: 10px; }}
      .trait-list {{ display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 6px 12px; margin: 0; font-size: 0.86rem; }}
      .trait-list dt {{ color: var(--ep-gray); }}
      .trait-list dd {{ margin: 0; font-weight: 650; }}
      .table-wrap {{ overflow-x: auto; border: 1px solid var(--ep-border); border-radius: 8px; background: #fff; }}
      table {{ width: 100%; border-collapse: collapse; min-width: 720px; }}
      th, td {{ border-bottom: 1px solid var(--ep-border); padding: 10px 12px; text-align: left; vertical-align: top; }}
      th {{ background: var(--ep-light-gray); color: var(--ep-gray); font-size: 0.73rem; text-transform: uppercase; letter-spacing: 0; font-weight: 750; }}
      td {{ font-size: 0.88rem; }}
      tr:last-child td {{ border-bottom: 0; }}
      a {{ color: var(--ep-green); text-decoration-thickness: 1px; text-underline-offset: 2px; }}
      .badge {{ display: inline-flex; align-items: center; border-radius: 4px; background: var(--ep-green-soft); color: var(--ep-green); padding: 2px 7px; margin: 0 4px 4px 0; font-size: 0.76rem; font-weight: 700; white-space: nowrap; }}
      .priority {{ background: rgba(178, 107, 42, 0.12); color: var(--ep-amber); }}
      .kind {{ background: #f2f2f2; color: var(--ep-dark); }}
      .state.active {{ background: var(--ep-green-soft); color: var(--ep-green); }}
      .state.archived {{ background: #f2f2f2; color: var(--ep-gray); }}
      .stage {{ text-transform: capitalize; }}
      .owner-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px; }}
      .owner-group {{ border: 1px solid var(--ep-border); border-radius: 8px; padding: 12px 14px; }}
      .owner-group h3 {{ margin: 0 0 8px; color: var(--ep-green); }}
      .owner-group ul, .warning-list {{ margin: 0; padding-left: 20px; }}
      .owner-group li {{ margin: 7px 0; }}
      .overdue {{ color: #9b2c24; }}
      .warning-panel {{ background: #fff8e8; border-color: #e5c27a; }}
      .report-controls {{ position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; align-items: end; gap: 10px; margin: 0 0 24px; padding: 12px; background: rgba(255,255,255,.96); border: 1px solid var(--ep-border); border-radius: 8px; box-shadow: 0 4px 14px rgba(0,0,0,.06); }}
      .report-controls label {{ display: grid; gap: 3px; color: var(--ep-gray); font-size: .72rem; font-weight: 700; text-transform: uppercase; }}
      .report-controls input, .report-controls select, .report-controls button {{ min-height: 36px; border: 1px solid var(--ep-border); border-radius: 5px; padding: 7px 9px; background: #fff; color: var(--ep-dark); font: inherit; }}
      .report-controls input {{ min-width: min(320px, 70vw); }}
      .report-controls button {{ cursor: pointer; font-weight: 700; }}
      .report-controls button:hover {{ border-color: var(--ep-green); color: var(--ep-green); }}
      .filter-count {{ margin-left: auto; color: var(--ep-gray); font-size: .78rem; }}
      th.sortable {{ cursor: pointer; user-select: none; }}
      th.sortable::after {{ content: " ↕"; color: #aaa; }}
      th.sortable[data-direction="asc"]::after {{ content: " ↑"; color: var(--ep-green); }}
      th.sortable[data-direction="desc"]::after {{ content: " ↓"; color: var(--ep-green); }}
      .filtered-out {{ display: none !important; }}
      details {{ border: 1px solid var(--ep-border); border-radius: 8px; margin: 8px 0; padding: 9px 12px; }}
      summary {{ cursor: pointer; color: var(--ep-green); }}
      .row-tags {{ margin-top: 5px; }}
      .subtle {{ color: var(--ep-gray); font-size: 0.78rem; margin-top: 2px; }}
      .muted {{ color: var(--ep-gray); }}
      .empty-cell {{ color: var(--ep-gray); text-align: center; padding: 24px; }}
      footer {{ color: var(--ep-gray); font-size: 0.78rem; margin-top: 34px; border-top: 1px solid var(--ep-border); padding-top: 12px; }}
      @media (max-width: 760px) {{
        .shell {{ padding: 18px 14px 40px; }}
        .brand-row, .section-head {{ align-items: flex-start; flex-direction: column; gap: 6px; }}
        .grid-2 {{ grid-template-columns: 1fr; }}
        h1 {{ font-size: 2.25rem; }}
        .filter-count {{ width: 100%; margin-left: 0; }}
      }}
      @media print {{ .report-controls {{ display: none; }} }}
    </style>
  </head>
  <body>
    <div class="shell">
      <header>
        <div class="brand-row">
          <span class="brand">E[&#x1f99c;] Expected Parrot</span>
          <span class="generated">Generated {escape(date_label(generated_at))} by niles</span>
        </div>
        <h1>{escape(org_name)} CRM Status</h1>
        <p class="lede">{escape(org_context)}</p>
        <div class="stats">
          <div class="stat"><span class="value">{counts['active_contacts']}</span><span class="label">active contacts</span></div>
          <div class="stat"><span class="value">{counts['archived_contacts']}</span><span class="label">archived contacts</span></div>
          <div class="stat"><span class="value">{counts['open_tasks']}</span><span class="label">open tasks</span></div>
          <div class="stat"><span class="value">{counts['notes']}</span><span class="label">notes captured</span></div>
          <div class="stat"><span class="value">{counts['materials']}</span><span class="label">sales materials</span></div>
        </div>
      </header>
      <main>
        <div class="report-controls" aria-label="Report filters">
          <label>Search accounts, people, status, or actions<input id="report-search" type="search" placeholder="e.g. contract, Smithers, Robin"></label>
          <label>Pipeline stage<select id="stage-filter"><option value="">All stages</option>{''.join(f'<option value="{stage}">{stage.title()}</option>' for stage in pipeline_stages)}<option value="unspecified">Unspecified</option></select></label>
          <button id="reset-filters" type="button">Reset</button>
          <button id="toggle-history" type="button">Expand history</button>
          <span class="filter-count" id="filter-count" aria-live="polite"></span>
        </div>
        <section class="section">
          <div class="section-head"><h2>Closest to Revenue</h2><span class="section-note">Accounts explicitly staged demo, pilot, or contracting</span></div>
          <div class="table-wrap"><table><thead><tr><th>Account</th><th>Stage</th><th>Priority</th><th>People</th><th>Latest real interaction</th><th>Current status</th><th>Next action · owner · due</th></tr></thead><tbody>{revenue_rows}</tbody></table></div>
        </section>

        <section class="section">
          <div class="section-head"><h2>Actions Due This Week</h2><span class="section-note">Grouped by owner; overdue actions shown in red</span></div>
          <div class="owner-grid">{owner_sections}</div>
        </section>

        <section class="section">
          <div class="section-head"><h2>Stalled or Waiting</h2><span class="section-note">Explicitly stalled accounts and latest statuses containing “waiting”</span></div>
          <div class="table-wrap"><table><thead><tr><th>Account</th><th>Stage</th><th>Priority</th><th>People</th><th>Latest real interaction</th><th>Current status</th><th>Next action · owner · due</th></tr></thead><tbody>{stalled_rows}</tbody></table></div>
        </section>

        <section class="section">
          <div class="section-head"><h2>Active Pipeline</h2><span class="section-note">Won, lost, and dead accounts excluded</span></div>
          <div class="table-wrap"><table><thead><tr><th>Account</th><th>Stage</th><th>Priority</th><th>People</th><th>Latest real interaction</th><th>Current status</th><th>Next action · owner · due</th></tr></thead><tbody>{pipeline_rows}</tbody></table></div>
        </section>

        <section class="section">
          <div class="section-head"><h2>Commercial View</h2><span class="section-note">Explicit values only; no inferred forecast</span></div><div class="table-wrap"><table><thead><tr><th>Stage</th><th>Accounts</th><th>Deal value</th><th>Expected MRR</th></tr></thead><tbody>{stage_metrics_rows}</tbody></table></div>
        </section>

        <section class="section">
          <div class="section-head"><h2>Warm Introductions</h2><span class="section-note">Target accounts with an explicit connector</span></div><div class="table-wrap"><table><thead><tr><th>Target</th><th>Connector</th><th>Status</th><th>Next action</th></tr></thead><tbody>{warm_rows}</tbody></table></div>
        </section>

        <section class="section">
          <div class="section-head"><h2>Relationship Network</h2><span class="section-note">People are kept separate from pipeline accounts, including unaffiliated contacts</span></div>
          <div class="table-wrap"><table><thead><tr><th>Person</th><th>Organization</th><th>Relationship role</th><th>Latest interaction</th><th>Next action</th></tr></thead><tbody>{network_rows}</tbody></table></div>
        </section>

        <section class="grid-2 section">
          <div class="panel warning-panel"><h3>Data quality and cleanup</h3><ul class="warning-list">{warnings_html}</ul></div>
          <aside class="panel"><h3>Organization</h3><p>{escape(org_context)}</p>{trait_block}</aside>
        </section>

        <section class="section">
          <details><summary><strong>Won, Lost, and Dead Accounts</strong> — {len(inactive_accounts)} excluded from active pipeline</summary>
            <div class="table-wrap"><table><thead><tr><th>Account</th><th>Stage</th><th>Priority</th><th>People</th><th>Latest real interaction</th><th>Final status</th><th>Remaining action</th></tr></thead><tbody>{inactive_rows}</tbody></table></div>
          </details>
        </section>

        <section class="section">
          <div class="section-head"><h2>Materials</h2><span class="section-note">Company context available to agents</span></div>
          <div class="table-wrap"><table><thead><tr><th>Title</th><th>Location</th><th>Tags</th><th>Added</th></tr></thead><tbody>{material_rows}</tbody></table></div>
        </section>

        <section class="section history">
          <div class="section-head"><h2>Relationship History</h2><span class="section-note">Collapsed by account; latest status is shown in the pipeline above</span></div>
          {history_blocks}
        </section>
      </main>
      <footer>Generated {escape(generated_at)} by niles &middot; Expected Parrot</footer>
    </div>
    <script>
      (() => {{
        const search = document.querySelector('#report-search');
        const stage = document.querySelector('#stage-filter');
        const count = document.querySelector('#filter-count');
        const rows = [...document.querySelectorAll('.pipeline-row')];
        const ownerGroups = [...document.querySelectorAll('.owner-group')];
        function filterReport() {{
          const query = search.value.trim().toLowerCase();
          const selectedStage = stage.value;
          const visibleAccounts = new Set();
          rows.forEach(row => {{
            const show = (!query || row.dataset.search.includes(query)) && (!selectedStage || row.dataset.stage === selectedStage);
            row.classList.toggle('filtered-out', !show);
            if (show) visibleAccounts.add(row.dataset.account);
          }});
          ownerGroups.forEach(group => group.classList.toggle('filtered-out', Boolean(query) && !group.dataset.search.includes(query)));
          count.textContent = `${{visibleAccounts.size}} matching account${{visibleAccounts.size === 1 ? '' : 's'}}`;
        }}
        search.addEventListener('input', filterReport);
        stage.addEventListener('change', filterReport);
        document.querySelector('#reset-filters').addEventListener('click', () => {{ search.value = ''; stage.value = ''; filterReport(); search.focus(); }});

        let historyOpen = false;
        const historyDetails = [...document.querySelectorAll('.history details')];
        const historyButton = document.querySelector('#toggle-history');
        historyButton.addEventListener('click', () => {{
          historyOpen = !historyOpen;
          historyDetails.forEach(detail => detail.open = historyOpen);
          historyButton.textContent = historyOpen ? 'Collapse history' : 'Expand history';
        }});

        document.querySelectorAll('table').forEach(table => {{
          const body = table.tBodies[0];
          if (!body) return;
          [...table.tHead.rows[0].cells].forEach((header, column) => {{
            header.classList.add('sortable'); header.tabIndex = 0; header.setAttribute('role', 'button');
            const sort = () => {{
              const direction = header.dataset.direction === 'asc' ? 'desc' : 'asc';
              [...header.parentElement.cells].forEach(cell => delete cell.dataset.direction);
              header.dataset.direction = direction;
              const factor = direction === 'asc' ? 1 : -1;
              [...body.rows].sort((a, b) => a.cells[column].innerText.trim().localeCompare(b.cells[column].innerText.trim(), undefined, {{numeric:true}}) * factor).forEach(row => body.appendChild(row));
            }};
            header.addEventListener('click', sort);
            header.addEventListener('keydown', event => {{ if (event.key === 'Enter' || event.key === ' ') {{ event.preventDefault(); sort(); }} }});
          }});
        }});
        filterReport();
      }})();
    </script>
  </body>
</html>
"""


def priority_sort_key(value: Any) -> tuple[int, str]:
    if value in (None, ""):
        return (1, "")
    try:
        return (0, f"{float(value):020.6f}")
    except (TypeError, ValueError):
        return (0, str(value).lower())


def material_link(material: dict[str, Any]) -> str:
    target = material.get("url") or material.get("path")
    if not target:
        return '<span class="muted">-</span>'
    safe_target = escape(str(target), quote=True)
    safe_label = escape(str(target))
    if material.get("url"):
        return f'<a href="{safe_target}">{safe_label}</a>'
    return safe_label
