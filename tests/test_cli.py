from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run_niles(tmp_path, *args):
    result = subprocess.run(
        [sys.executable, "-m", "niles", *args],
        cwd=tmp_path,
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    return result.returncode, json.loads(result.stdout)


def test_init_and_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, payload = run_niles(tmp_path, "init")
    assert code == 0
    assert payload["status"] == "ok"
    assert (tmp_path / ".niles" / "events").is_dir()

    code, payload = run_niles(tmp_path, "status")
    assert code == 0
    assert payload["data"]["contacts"] == 0
    assert payload["data"]["open_tasks"] == 0


def test_agent_next_before_and_after_init(tmp_path):
    code, payload = run_niles(tmp_path, "agent", "next")
    assert code == 0
    assert payload["status"] == "ok"
    assert payload["data"]["initialized"] is False
    assert payload["next_steps"][0]["command"] == "niles init"

    assert run_niles(tmp_path, "init")[0] == 0

    code, payload = run_niles(tmp_path, "agent", "next")
    assert code == 0
    assert payload["data"]["initialized"] is True
    assert "how_it_works" in payload["data"]
    assert payload["next_steps"][0]["command"].startswith("niles contact add")


def test_contact_note_task_flow(tmp_path):
    code, payload = run_niles(
        tmp_path,
        "init",
    )
    assert code == 0

    code, payload = run_niles(
        tmp_path,
        "contact",
        "add",
        "Google Deepmind",
        "--tag",
        "prospect",
        "--trait",
        "priority=1",
        "--cadence-days",
        "7",
    )
    assert code == 0
    assert payload["data"]["contact"]["slug"] == "google-deepmind"

    code, payload = run_niles(
        tmp_path,
        "note",
        "add",
        "google-deepmind",
        "Waiting for contract!",
        "--at",
        "2026-09-01",
    )
    assert code == 0
    assert payload["data"]["note"]["created_at"].startswith("2026-09-01")

    code, payload = run_niles(
        tmp_path,
        "task",
        "add",
        "google-deepmind",
        "Check contract status",
        "--due",
        "2026-09-04",
        "--assign",
        "john",
    )
    assert code == 0
    task_id = payload["data"]["task"]["id"]

    code, payload = run_niles(tmp_path, "task", "list", "--assignee", "john")
    assert code == 0
    assert payload["data"]["tasks"][0]["text"] == "Check contract status"

    code, payload = run_niles(tmp_path, "task", "done", task_id, "--note", "Sent note")
    assert code == 0
    assert payload["data"]["status"] == "done"


def test_notes_contact_updates_tasks_and_report(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Construct Connect", "--tag", "prospect")[0] == 0
    assert run_niles(tmp_path, "note", "add", "construct-connect", "Waiting on demo follow-up", "--kind", "call")[0] == 0

    code, payload = run_niles(tmp_path, "note", "list", "construct-connect")
    assert code == 0
    assert payload["data"]["notes"][0]["text"] == "Waiting on demo follow-up"

    code, payload = run_niles(tmp_path, "contact", "show", "construct-connect", "--with-notes")
    assert code == 0
    assert payload["data"]["contact"]["notes"][0]["kind"] == "call"

    code, payload = run_niles(tmp_path, "contact", "tag", "construct-connect", "--add", "dead", "--remove", "prospect")
    assert code == 0
    assert "dead" in payload["data"]["contact"]["tags"]
    assert "prospect" not in payload["data"]["contact"]["tags"]

    code, payload = run_niles(tmp_path, "contact", "archive", "construct-connect", "--reason", "No active path")
    assert code == 0
    assert payload["data"]["archived"] is True

    assert run_niles(tmp_path, "contact", "add", "Workday", "--tag", "prospect")[0] == 0
    assert run_niles(tmp_path, "task", "add", "workday", "Reach out to Athena", "--assign", "john")[0] == 0
    code, payload = run_niles(tmp_path, "task", "list", "--assignee", "john")
    task_id = payload["data"]["tasks"][0]["id"]

    code, payload = run_niles(tmp_path, "task", "reassign", task_id, "robin")
    assert code == 0
    assert payload["data"]["task"]["assignee"] == "robin"

    code, payload = run_niles(tmp_path, "task", "cancel", task_id, "--note", "Waiting on them")
    assert code == 0
    assert payload["data"]["task"]["status"] == "cancelled"
    assert payload["data"]["task"]["done_note"] == "Waiting on them"

    report = tmp_path / "status.html"
    code, payload = run_niles(tmp_path, "report", "status", "--html", str(report))
    assert code == 0
    assert report.is_file()
    assert "Workday" in report.read_text(encoding="utf-8")


def test_org_material_enrichment_and_merge(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Salesforce", "--tag", "target")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Salesforce Inc", "--tag", "prospect")[0] == 0

    code, payload = run_niles(
        tmp_path,
        "org",
        "context",
        "set",
        "Expected Parrot sells EDSL-backed research workflows.",
        "--name",
        "Expected Parrot",
        "--trait",
        "market=research",
    )
    assert code == 0
    assert payload["data"]["org"]["name"] == "Expected Parrot"

    code, payload = run_niles(
        tmp_path,
        "material",
        "add",
        "GTM deck",
        "--url",
        "https://example.com/deck",
        "--tag",
        "sales",
    )
    assert code == 0
    assert payload["data"]["material"]["title"] == "GTM deck"

    code, payload = run_niles(
        tmp_path,
        "enrich",
        "ingest",
        "salesforce",
        "Agent found a relevant enterprise AI angle.",
        "--source-url",
        "https://example.com/source",
        "--confidence",
        "0.8",
    )
    assert code == 0
    assert payload["data"]["note"]["kind"] == "enrichment"

    code, payload = run_niles(tmp_path, "contact", "merge", "salesforce", "salesforce-inc", "--note", "Duplicate")
    assert code == 0
    assert payload["data"]["duplicate_id"].startswith("con_")


def test_export_import_round_trip(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    archive = tmp_path / "niles-export.zip"

    assert run_niles(source, "init")[0] == 0
    assert run_niles(source, "contact", "add", "Acme Data", "--tag", "prospect")[0] == 0
    assert run_niles(source, "note", "add", "acme-data", "Intro call", "--kind", "call")[0] == 0
    assert run_niles(source, "task", "add", "acme-data", "Send follow-up", "--due", "2026-09-05")[0] == 0

    code, payload = run_niles(source, "export", str(archive))
    assert code == 0
    assert payload["data"]["archive"] == str(archive)
    assert archive.is_file()

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
    assert "niles-export-manifest.json" in names
    assert ".niles/config.toml" in names
    assert not any(name.startswith(".niles/index/") for name in names)

    code, payload = run_niles(target, "import", str(archive))
    assert code == 0
    assert payload["data"]["counts"]["contacts"] == 1
    assert payload["data"]["counts"]["notes"] == 1
    assert payload["data"]["counts"]["open_tasks"] == 1

    code, payload = run_niles(target, "contact", "list")
    assert code == 0
    assert payload["data"]["contacts"][0]["slug"] == "acme-data"
    assert (target / ".niles" / "index" / "niles.sqlite").is_file()

    code, payload = run_niles(target, "import", str(archive))
    assert code == 1
    assert payload["errors"][0]["code"] == "project_exists"


def test_ambiguous_contact_ref_blocks_mutation(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Ronnie Chatterjee")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Robbie Chatterjee")[0] == 0

    code, payload = run_niles(tmp_path, "note", "add", "chatterjee", "Follow up")
    assert code == 1
    assert payload["status"] == "error"
    assert payload["errors"][0]["code"] == "ambiguous_reference"
    assert len(payload["data"]["candidates"]) == 2
