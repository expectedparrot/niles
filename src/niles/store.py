from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tomllib
import zipfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
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
            niles_gitignore.write_text("index/\n", encoding="utf-8")
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
                  (id, contact_id, created_at, kind, text, source)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["contact_id"],
                    payload["created_at"],
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
            f"{where} order by notes.created_at desc"
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
            allowed = {"name", "email", "emails", "company", "role", "tags"}
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
            preview.append({"name": row["name"].strip(), "emails": [v for v in (row.get("emails") or row.get("email") or "").split(";") if v], "company": row.get("company") or None, "role": row.get("role") or None, "tags": [v for v in (row.get("tags") or "").split(";") if v]})
        event_ids = []
        if commit:
            for item in preview:
                result = self.add_contact(item["name"], item["emails"], [], item["company"], item["role"], {}, item["tags"], None)
                event_ids.append(result["event_id"])
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

    def publish_form(self, kind: str, survey_name: str, contact_ref: str | None = None, recipient: str | None = None) -> dict[str, Any]:
        if not os.environ.get("EXPECTED_PARROT_API_KEY"):
            raise NilesError("missing_ep_api_key", "Set EXPECTED_PARROT_API_KEY before publishing a humanized survey.")
        definition = self.get_survey(survey_name)
        if kind == "intake":
            forbidden = [route["action"] for route in definition.get("routes", {}).values() if route["action"] in {"archive", "set_field"}]
            if forbidden:
                raise NilesError("unsafe_intake_route", "Intake surveys cannot archive contacts or set protected fields.")
        contact = self.resolve_contact(contact_ref) if contact_ref else None
        survey = self._make_edsl_survey(definition)
        try:
            details = dict(survey.humanize(human_survey_name=f"Niles {kind}: {survey_name}", survey_description=definition.get("description")))
        except Exception as exc:  # Network/auth errors become stable Niles errors.
            raise NilesError("humanize_failed", str(exc)) from exc
        payload = {
            "id": new_id("form"), "kind": kind, "survey_name": survey_name,
            "remote_uuid": str(details["uuid"]), "respondent_url": details.get("respondent_url"),
            "admin_url": details.get("admin_url"), "contact_id": contact["id"] if contact else None,
            "recipient": recipient, "created_at": utc_now(),
        }
        event = self.append_event("form_published", payload)
        return {"form": payload, "remote": details, "events_written": 1, "event_id": event["event_id"]}

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

    def pull_form(self, form_ref: str, kind: str, response_file: Path | None = None) -> dict[str, Any]:
        form = self.resolve_form(form_ref, kind)
        if response_file:
            path = response_file if response_file.is_absolute() else self.root / response_file
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise NilesError("invalid_responses", f"Could not read responses: {response_file}") from exc
        else:
            if not os.environ.get("EXPECTED_PARROT_API_KEY"):
                raise NilesError("missing_ep_api_key", "Set EXPECTED_PARROT_API_KEY before pulling responses.")
            try:
                from edsl.coop import Coop
                raw_object = Coop().get_human_survey_responses(form["remote_uuid"])
                raw = raw_object.to_dict() if hasattr(raw_object, "to_dict") else raw_object
            except ImportError as exc:
                raise NilesError("edsl_not_installed", "Response pulling requires niles[edsl].") from exc
            except Exception as exc:
                raise NilesError("humanize_pull_failed", str(exc)) from exc
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
        return {"form": form, "received": len(written), "skipped": skipped, "event_ids": written, "quarantined": True}

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

    def export_recommendation_job(self, name: str, output: Path, tag: str | None = None) -> dict[str, Any]:
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
        path = output if output.is_absolute() else self.root / output
        path.parent.mkdir(parents=True, exist_ok=True)
        job.save(str(path))
        manifest = path.with_suffix(path.suffix + ".manifest.json")
        manifest.write_text(json.dumps({"schema_version": "niles.recommend-job.v1", "name": name, "job_path": str(path), "contact_ids": [item["contact_id"] for item in scenarios], "created_at": utc_now()}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"path": str(path), "manifest": str(manifest), "contacts": len(scenarios), "run_command": f"ep run {path} --output <results-path>", "network": False}

    def import_recommendations(self, results_path: Path) -> dict[str, Any]:
        path = results_path if results_path.is_absolute() else self.root / results_path
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
        notes = self.list_notes(limit=50)
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
    visible_contacts = sorted(
        contacts,
        key=lambda contact: (
            bool(contact.get("archived")),
            priority_sort_key(contact.get("traits", {}).get("priority")),
            contact["name"].lower(),
        ),
    )
    org_name = org.get("name") or project_name
    org_context = org.get("context") or "No organization context has been set."
    org_traits = org.get("traits") or {}

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

    task_rows = "\n".join(
        "<tr>"
        f"<td>{escape(date_label(task.get('due_date')) or 'No due date')}</td>"
        f"<td>{escape(task.get('assignee') or 'Unassigned')}</td>"
        f"<td>{escape(task.get('contact') or 'No contact')}</td>"
        f"<td><strong>{escape(task['text'])}</strong><div class=\"row-tags\">{tag_list(task.get('tags', []))}</div></td>"
        "</tr>"
        for task in tasks
    ) or empty_row(4, "No open tasks.")

    contact_rows = "\n".join(
        "<tr>"
        f"<td><strong>{escape(contact['name'])}</strong><div class=\"subtle\">{escape(contact.get('company') or contact.get('role') or contact['slug'])}</div></td>"
        f"<td>{tag_list(contact['tags'])}</td>"
        f"<td>{badge(contact.get('traits', {}).get('priority'), 'priority')}</td>"
        f"<td>{escape(date_label(contact.get('last_touched')) or 'No notes yet')}</td>"
        f"<td>{badge('Archived' if contact.get('archived') else 'Active', 'state archived' if contact.get('archived') else 'state active')}</td>"
        "</tr>"
        for contact in visible_contacts
    ) or empty_row(5, "No contacts yet.")

    note_rows = "\n".join(
        "<tr>"
        f"<td>{escape(date_label(note['created_at']))}</td>"
        f"<td>{escape(note.get('contact') or 'No contact')}</td>"
        f"<td>{badge(note['kind'], 'kind')}</td>"
        f"<td>{escape(note['text'])}</td>"
        "</tr>"
        for note in notes
    ) or empty_row(4, "No notes yet.")

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
      }}
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
        <section class="grid-2 section">
          <div>
            <div class="section-head"><h2>Next Actions</h2><span class="section-note">Open tasks sorted by due date</span></div>
            <div class="table-wrap"><table><thead><tr><th>Due</th><th>Owner</th><th>Contact</th><th>Task</th></tr></thead><tbody>{task_rows}</tbody></table></div>
          </div>
          <aside class="panel">
            <h3>Organization</h3>
            <p>{escape(org_context)}</p>
            {trait_block}
          </aside>
        </section>

        <section class="section">
          <div class="section-head"><h2>Contacts</h2><span class="section-note">Active contacts first, then archived records</span></div>
          <div class="table-wrap"><table><thead><tr><th>Name</th><th>Tags</th><th>Priority</th><th>Last Touched</th><th>State</th></tr></thead><tbody>{contact_rows}</tbody></table></div>
        </section>

        <section class="section">
          <div class="section-head"><h2>Recent Notes</h2><span class="section-note">Latest 50 notes</span></div>
          <div class="table-wrap"><table><thead><tr><th>Date</th><th>Contact</th><th>Kind</th><th>Note</th></tr></thead><tbody>{note_rows}</tbody></table></div>
        </section>

        <section class="section">
          <div class="section-head"><h2>Materials</h2><span class="section-note">Company context available to agents</span></div>
          <div class="table-wrap"><table><thead><tr><th>Title</th><th>Location</th><th>Tags</th><th>Added</th></tr></thead><tbody>{material_rows}</tbody></table></div>
        </section>
      </main>
      <footer>Generated {escape(generated_at)} by niles &middot; Expected Parrot</footer>
    </div>
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
