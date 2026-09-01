from __future__ import annotations

import json
import os
import shutil
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
    manifest = json.loads((tmp_path / ".niles" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "niles.project.v1"
    assert manifest["storage"]["source_of_truth"] == ".niles/events/"
    assert (tmp_path / ".niles" / ".gitignore").read_text(encoding="utf-8") == "index/\n"

    code, payload = run_niles(tmp_path, "status")
    assert code == 0
    assert payload["data"]["contacts"] == 0
    assert payload["data"]["open_tasks"] == 0


def test_rebuild_index_restores_deleted_sqlite_projection(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Acme Data")[0] == 0
    assert run_niles(tmp_path, "note", "add", "acme-data", "Intro call")[0] == 0
    index = tmp_path / ".niles" / "index" / "niles.sqlite"
    assert index.is_file()
    index.unlink()

    code, payload = run_niles(tmp_path, "status")
    assert code == 0
    assert payload["data"]["contacts"] == 1
    assert payload["data"]["notes"] == 1
    assert index.is_file()

    index.unlink()
    code, payload = run_niles(tmp_path, "rebuild-index")
    assert code == 0
    assert payload["data"]["counts"]["contacts"] == 1
    assert index.is_file()


def test_filesystem_state_can_move_without_index(tmp_path):
    source = tmp_path / "source"
    clone = tmp_path / "clone"
    source.mkdir()
    clone.mkdir()
    assert run_niles(source, "init")[0] == 0
    assert run_niles(source, "contact", "add", "Acme Data", "--tag", "prospect")[0] == 0
    assert run_niles(source, "task", "add", "acme-data", "Follow up")[0] == 0

    shutil.copytree(
        source / ".niles",
        clone / ".niles",
        ignore=shutil.ignore_patterns("index"),
    )
    assert not (clone / ".niles" / "index" / "niles.sqlite").exists()

    code, payload = run_niles(clone, "status")
    assert code == 0
    assert payload["data"]["contacts"] == 1
    assert payload["data"]["open_tasks"] == 1
    assert (clone / ".niles" / "index" / "niles.sqlite").is_file()


def test_fsck_success_and_corrupt_event_failure(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Acme Data")[0] == 0

    code, payload = run_niles(tmp_path, "fsck")
    assert code == 0
    assert payload["data"]["ok"] is True
    assert payload["data"]["index_is_derived"] is True

    (tmp_path / ".niles" / "events" / "000000000002.json").write_text("{bad json\n", encoding="utf-8")
    code, payload = run_niles(tmp_path, "fsck")
    assert code == 1
    assert payload["status"] == "error"
    assert payload["errors"][0]["code"] == "invalid_event_json"


def test_fsck_detects_sequence_gap_and_unsupported_type(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Acme Data")[0] == 0
    first_event = tmp_path / ".niles" / "events" / "000000000001.json"
    event = json.loads(first_event.read_text(encoding="utf-8"))
    event["sequence"] = 3
    event["type"] = "mystery_event"
    first_event.write_text(json.dumps(event, indent=2) + "\n", encoding="utf-8")

    code, payload = run_niles(tmp_path, "fsck")
    assert code == 1
    codes = {error["code"] for error in payload["errors"]}
    assert "event_sequence_gap" in codes
    assert "event_filename_mismatch" in codes
    assert "unsupported_event_type" in codes


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
    assert run_niles(
        tmp_path,
        "org",
        "context",
        "set",
        "Expected Parrot tracks warm enterprise research leads.",
        "--name",
        "Expected Parrot",
        "--trait",
        "segment=research",
    )[0] == 0
    assert run_niles(
        tmp_path,
        "material",
        "add",
        "Buyer FAQ",
        "--url",
        "https://example.com/faq",
        "--tag",
        "sales",
    )[0] == 0
    assert run_niles(tmp_path, "note", "add", "workday", "<script>alert('x')</script>", "--kind", "note")[0] == 0
    assert run_niles(tmp_path, "task", "add", "workday", "Reach out to Athena", "--assign", "john")[0] == 0
    assert run_niles(tmp_path, "task", "add", "workday", "Send pricing deck", "--assign", "robin", "--due", "2026-09-05")[0] == 0
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
    html = report.read_text(encoding="utf-8")
    assert "E[&#x1f99c;] Expected Parrot" in html
    assert "Expected Parrot CRM Status" in html
    assert "Next Actions" in html
    assert "Send pricing deck" in html
    assert "Buyer FAQ" in html
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in html
    assert "<script>alert('x')</script>" not in html


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
    assert ".niles/manifest.json" in names
    assert ".niles/.gitignore" in names
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
