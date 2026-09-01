from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
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

    @classmethod
    def init(cls, root: Path) -> "Project":
        project = cls(root.resolve())
        project.events_dir.mkdir(parents=True, exist_ok=True)
        (project.state_dir / "index").mkdir(parents=True, exist_ok=True)
        (project.state_dir / "surveys").mkdir(parents=True, exist_ok=True)
        config = project.state_dir / "config.toml"
        if not config.exists():
            config.write_text('format_version = 1\n', encoding="utf-8")
        project.rebuild_index()
        return project

    @classmethod
    def open(cls, start: Path) -> "Project":
        current = start.resolve()
        for path in [current, *current.parents]:
            if (path / ".niles").is_dir():
                return cls(path)
        raise NilesError(
            "not_initialized",
            "No .niles directory found. Run `niles init` first.",
            {"start": str(start)},
        )

    def append_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.events_dir.mkdir(parents=True, exist_ok=True)
        seq = self._next_sequence()
        event = {
            "schema_version": "niles.event.v1",
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
            """
        )
        conn.commit()

    def rebuild_index(self) -> None:
        if self.index_path.exists():
            self.index_path.unlink()
        with self.connect() as conn:
            for path in sorted(self.events_dir.glob("*.json")):
                event = json.loads(path.read_text(encoding="utf-8"))
                self._apply_event(conn, event)

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
        conn.commit()

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
            contacts = [c for c in contacts if c.get("cadence_days") and c.get("last_touched") is None]
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
    ) -> list[dict[str, Any]]:
        query = "select tasks.*, contacts.name as contact_name from tasks left join contacts on tasks.contact_id = contacts.id"
        clauses: list[str] = []
        params: list[Any] = []
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

    def counts(self) -> dict[str, Any]:
        with self.connect() as conn:
            return {
                "contacts": conn.execute("select count(*) from contacts").fetchone()[0],
                "active_contacts": conn.execute("select count(*) from contacts where archived = 0").fetchone()[0],
                "notes": conn.execute("select count(*) from notes").fetchone()[0],
                "open_tasks": conn.execute("select count(*) from tasks where status = 'open'").fetchone()[0],
                "done_tasks": conn.execute("select count(*) from tasks where status = 'done'").fetchone()[0],
                "pending_intake": 0,
                "pending_status_updates": 0,
            }

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
