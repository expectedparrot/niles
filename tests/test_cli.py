from __future__ import annotations

import json
import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

from niles.store import Project, utc_now


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
    assert (tmp_path / ".niles" / ".gitignore").read_text(encoding="utf-8") == "index/\nexchange/\n"

    code, payload = run_niles(tmp_path, "status")
    assert code == 0
    assert payload["data"]["contacts"] == 0
    assert payload["data"]["open_tasks"] == 0


def test_sync_stages_only_durable_niles_state(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Lionel Hutz"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "hutz@example.com"], cwd=tmp_path, check=True)
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Burns Industries")[0] == 0
    (tmp_path / "unrelated.txt").write_text("do not stage me\n", encoding="utf-8")
    subprocess.run(["git", "add", "unrelated.txt"], cwd=tmp_path, check=True)

    code, payload = run_niles(tmp_path, "sync", "--dry-run", "--no-push", "--message", "Update Hutz CRM")
    assert code == 0
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["committed"] is False

    code, payload = run_niles(tmp_path, "sync", "--no-push", "--message", "Update Hutz CRM")
    assert code == 0
    assert payload["data"]["committed"] is True
    assert payload["data"]["pushed"] is False
    assert payload["data"]["readme_projection"]["changed"] is True
    tracked = subprocess.run(["git", "ls-tree", "-r", "--name-only", "HEAD"], cwd=tmp_path, check=True, text=True, capture_output=True).stdout.splitlines()
    assert "README.md" in tracked
    assert ".niles/manifest.json" in tracked
    assert any(path.startswith(".niles/events/") for path in tracked)
    assert not any(path.startswith(".niles/index/") for path in tracked)
    assert "unrelated.txt" not in tracked
    assert "A  unrelated.txt" in subprocess.run(["git", "status", "--short"], cwd=tmp_path, check=True, text=True, capture_output=True).stdout
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "## Active pipeline" in readme
    assert "No active accounts" in readme
    assert "Burns Industries: entity type is ambiguous" in readme

    code, payload = run_niles(tmp_path, "sync", "--no-push")
    assert code == 0
    assert payload["data"]["committed"] is False


def test_sync_readme_projection_preserves_user_content_and_refreshes(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Lionel Hutz"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "hutz@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# Hutz Law\n\nCall 1-800-SUE-NOW.\n", encoding="utf-8")
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Burns Industries", "--tag", "company", "--trait", "stage=engaged")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Waylon Smithers", "--tag", "person", "--company", "Burns Industries", "--role", "Executive Assistant")[0] == 0
    assert run_niles(tmp_path, "task", "add", "burns-industries", "Send retainer", "--assign", "Lionel", "--due", "2026-09-04")[0] == 0

    code, payload = run_niles(tmp_path, "sync", "--no-push", "--message", "Publish CRM view")
    assert code == 0
    assert payload["data"]["readme_projection"]["changed"] is True
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("# Hutz Law\n\nCall 1-800-SUE-NOW.")
    assert readme.count("<!-- niles:projection:start -->") == 1
    assert "| Burns Industries | engaged" in readme
    assert "| Waylon Smithers | Burns Industries | Executive Assistant" in readme
    assert "| Lionel | 2026-09-04 | Burns Industries | Send retainer |" in readme

    assert run_niles(tmp_path, "contact", "status", "burns-industries", "Retainer sent")[0] == 0
    code, payload = run_niles(tmp_path, "sync", "--no-push", "--message", "Refresh CRM view")
    assert code == 0
    refreshed = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert refreshed.count("<!-- niles:projection:start -->") == 1
    assert "Call 1-800-SUE-NOW." in refreshed
    assert "Retainer sent" in refreshed


def test_sync_requires_project_at_git_root(tmp_path):
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    assert run_niles(standalone, "init")[0] == 0
    code, payload = run_niles(standalone, "sync", "--no-push")
    assert code == 1
    assert payload["errors"][0]["code"] == "not_git_repository"

    repository = tmp_path / "repository"
    project = repository / "crm"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    assert run_niles(project, "init")[0] == 0
    code, payload = run_niles(project, "sync", "--no-push")
    assert code == 1
    assert payload["errors"][0]["code"] == "git_root_mismatch"


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
    instructions = json.dumps(payload["data"])
    assert ".niles" not in instructions
    assert "Niles publishes" not in instructions
    assert payload["data"]["state_contract"]["storage"] == "managed_by_niles"
    assert "exclusively publishes" in payload["data"]["edsl_handoff_rule"]
    assert payload["data"]["managed_handoffs"]["intake"] == [
        "niles intake export",
        "run data.publish_command",
        "niles intake register",
        "run data.pull_command",
        "niles intake import",
        "niles intake review",
    ]
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
    assert "Actions Due This Week" in html
    assert "Active Pipeline" in html
    assert "Data quality and cleanup" in html
    assert "Send pricing deck" in html
    assert "Buyer FAQ" in html
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in html
    assert "<script>alert('x')</script>" not in html


def test_status_report_synthesizes_pipeline_relationships_and_stalls(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(
        tmp_path,
        "contact",
        "add",
        "Burns Industries",
        "--trait",
        "stage=contracting",
        "--trait",
        "priority=1",
        "--trait",
        "deal_value=120000",
        "--trait",
        "expected_mrr=10000",
    )[0] == 0
    assert run_niles(
        tmp_path,
        "contact",
        "add",
        "Waylon Smithers",
        "--company",
        "Burns Industries",
        "--role",
        "champion",
    )[0] == 0
    assert run_niles(tmp_path, "note", "add", "burns-industries", "Waiting on engagement letter", "--at", "2026-08-20")[0] == 0
    assert run_niles(
        tmp_path,
        "task",
        "add",
        "burns-industries",
        "Ask Smithers for signature timing",
        "--assign",
        "lionel",
        "--due",
        "2026-09-03",
    )[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Krustylu Studios", "--tag", "lost")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Globex Corporation", "--trait", "stage=target", "--trait", "connector=Cookie")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Unidentified Courthouse Lead", "--tag", "prospect")[0] == 0

    report = tmp_path / "operating.html"
    assert run_niles(tmp_path, "report", "status", "--html", str(report))[0] == 0
    html = report.read_text(encoding="utf-8")
    assert "Closest to Revenue" in html
    assert "Stalled or Waiting" in html
    assert "Burns Industries" in html
    assert "contracting" in html
    assert "Waylon Smithers" in html
    assert "champion" in html
    assert "2026-08-20" in html
    assert "Waiting on engagement letter" in html
    assert "Ask Smithers for signature timing" in html
    assert "Actions Due This Week" in html
    assert "lionel" in html
    assert "Won, Lost, and Dead Accounts" in html
    assert "1 excluded from active pipeline" in html
    assert "Unidentified Courthouse Lead: pipeline stage missing" in html
    assert "Unidentified Courthouse Lead: next action missing" in html
    assert "Commercial View" in html
    assert "$120,000" in html
    assert "$10,000" in html
    assert "Warm Introductions" in html
    assert "Globex Corporation" in html
    assert "Cookie" in html
    assert "Relationship History" in html
    assert "<details>" in html
    assert 'id="report-search"' in html
    assert 'id="stage-filter"' in html
    assert 'id="toggle-history"' in html
    assert "matching account" in html
    assert "data-stage=\"contracting\"" in html
    assert "querySelectorAll('table')" in html


def test_report_entity_types_separate_pipeline_and_relationship_network(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Moe Szyslak", "--tag", "person")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Moe's Tavern", "--tag", "company", "--tag", "prospect")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Barney Gumble", "--company", "Moe's Tavern")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Mystery Record")[0] == 0

    report = tmp_path / "entities.html"
    assert run_niles(tmp_path, "report", "status", "--html", str(report))[0] == 0
    html = report.read_text(encoding="utf-8")
    active_pipeline = html.split("<h2>Active Pipeline</h2>", 1)[1].split("</section>", 1)[0]
    relationship_network = html.split("<h2>Relationship Network</h2>", 1)[1].split("</section>", 1)[0]
    assert "Moe&#x27;s Tavern" in active_pipeline
    assert "Moe Szyslak" not in active_pipeline
    assert "Mystery Record" not in active_pipeline
    assert "Moe Szyslak" in relationship_network
    assert "Unaffiliated" in relationship_network
    assert "Barney Gumble" in relationship_network
    assert "Mystery Record: entity type is ambiguous" in html


def test_equal_timestamp_notes_use_append_sequence_for_current_status(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Burns Industries", "--tag", "prospect")[0] == 0
    assert run_niles(tmp_path, "note", "add", "burns-industries", "Historical status", "--at", "2026-09-01")[0] == 0
    assert run_niles(tmp_path, "note", "add", "burns-industries", "Newest same-day status", "--at", "2026-09-01")[0] == 0

    notes = run_niles(tmp_path, "note", "list", "burns-industries")[1]["data"]["notes"]
    assert [note["text"] for note in notes] == ["Newest same-day status", "Historical status"]
    assert notes[0]["event_sequence"] > notes[1]["event_sequence"]

    report = tmp_path / "before.html"
    assert run_niles(tmp_path, "report", "status", "--html", str(report))[0] == 0
    pipeline = report.read_text(encoding="utf-8").split("<h2>Active Pipeline</h2>", 1)[1].split("</section>", 1)[0]
    assert "Newest same-day status" in pipeline
    assert "Historical status" not in pipeline

    assert run_niles(tmp_path, "rebuild-index")[0] == 0
    rebuilt_notes = run_niles(tmp_path, "note", "list", "burns-industries")[1]["data"]["notes"]
    assert [note["text"] for note in rebuilt_notes] == ["Newest same-day status", "Historical status"]

    code, status_payload = run_niles(tmp_path, "contact", "status", "burns-industries", "Explicit override", "--at", "2026-09-01")
    assert code == 0
    assert status_payload["data"]["events_written"] == 2
    assert status_payload["data"]["contact"]["traits"]["current_status"] == "Explicit override"
    override = tmp_path / "override.html"
    assert run_niles(tmp_path, "report", "status", "--html", str(override))[0] == 0
    override_pipeline = override.read_text(encoding="utf-8").split("<h2>Active Pipeline</h2>", 1)[1].split("</section>", 1)[0]
    assert "Explicit override" in override_pipeline
    assert "Newest same-day status" not in override_pipeline


def test_populated_hutz_law_example_generates_operating_report(tmp_path):
    target = tmp_path / "hutz-law-demo"
    result = subprocess.run(
        [str(ROOT / "examples" / "hutz-law-crm" / "populate.sh"), str(target)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "NILES_PYTHON": sys.executable},
    )
    assert result.returncode == 0, result.stderr
    report = target / "crm-operating-report.html"
    assert report.is_file()
    html = report.read_text(encoding="utf-8")
    assert "Burns Industries" in html
    assert "Globex Corporation" in html
    assert "Springfield Monorail Authority" in html
    assert "The Leftorium" in html
    assert "$210,000" in html
    assert "$60,000" in html
    assert "Smithers" in html
    assert "5 excluded from active pipeline" in html
    assert 'id="report-search"' in html

    repeated = subprocess.run(
        [str(ROOT / "examples" / "hutz-law-crm" / "populate.sh"), str(target)],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "NILES_PYTHON": sys.executable},
    )
    assert repeated.returncode == 2
    assert "Refusing to overwrite" in repeated.stderr


def test_published_hutz_law_report_is_checked_in():
    report = ROOT / "docs" / "examples" / "hutz-law-crm-report.html"
    assert report.is_file()
    html = report.read_text(encoding="utf-8")
    assert "Hutz Law CRM Status" in html
    assert "Burns Industries" in html
    assert "Globex Corporation" in html
    assert 'id="report-search"' in html
    assert "Google DeepMind" not in html
    assert "Upwork" not in html


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


def test_history_and_compensating_undo_survive_rebuild(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    code, payload = run_niles(tmp_path, "contact", "add", "Acme Data")
    contact_event = payload["data"]["event_id"]
    code, payload = run_niles(tmp_path, "note", "add", "acme-data", "Intro call")
    note_event = payload["data"]["event_id"]

    code, payload = run_niles(tmp_path, "history", "--contact", "acme-data")
    assert code == 0
    assert [event["event_id"] for event in payload["data"]["events"]] == [note_event, contact_event]

    code, payload = run_niles(tmp_path, "undo", note_event)
    assert code == 0
    assert payload["data"]["reverted_event_id"] == note_event
    assert run_niles(tmp_path, "rebuild-index")[0] == 0
    code, payload = run_niles(tmp_path, "note", "list", "acme-data")
    assert payload["data"]["notes"] == []

    code, payload = run_niles(tmp_path, "undo", note_event)
    assert code == 1
    assert payload["errors"][0]["code"] == "already_reverted"


def test_search_contacts_notes_and_tasks(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Maya Chen", "--company", "Acme Analytics", "--trait", "timezone=ET")[0] == 0
    assert run_niles(tmp_path, "note", "add", "maya-chen", "Discussed security review")[0] == 0
    assert run_niles(tmp_path, "task", "add", "maya-chen", "Send procurement packet")[0] == 0

    assert run_niles(tmp_path, "search", "Analytics")[1]["data"]["results"][0]["type"] == "contact"
    assert run_niles(tmp_path, "search", "security review")[1]["data"]["results"][0]["type"] == "note"
    assert run_niles(tmp_path, "search", "procurement")[1]["data"]["results"][0]["type"] == "task"


def test_teammates_are_event_sourced_and_resolvable(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    code, payload = run_niles(tmp_path, "teammate", "add", "John Horton", "--alias", "john", "--alias", "JJH", "--email", "john@example.com", "--role", "Founder")
    assert code == 0
    teammate_id = payload["data"]["teammate"]["id"]
    assert run_niles(tmp_path, "rebuild-index")[0] == 0

    code, payload = run_niles(tmp_path, "teammate", "show", "JJH")
    assert code == 0
    assert payload["data"]["teammate"]["id"] == teammate_id
    assert run_niles(tmp_path, "teammate", "list")[1]["data"]["teammates"][0]["role"] == "Founder"


def test_csv_import_preview_commit_and_exports(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    source = tmp_path / "contacts.csv"
    source.write_text("name,email,company,tags\nMaya Chen,maya@example.com,Acme,buyer;prospect\n", encoding="utf-8")

    code, payload = run_niles(tmp_path, "import", "csv", str(source))
    assert code == 0
    assert payload["data"]["dry_run"] is True
    assert run_niles(tmp_path, "status")[1]["data"]["contacts"] == 0

    code, payload = run_niles(tmp_path, "import", "csv", str(source), "--commit")
    assert code == 0
    assert payload["data"]["events_written"] == 1

    output = tmp_path / "export.csv"
    code, payload = run_niles(tmp_path, "export", "csv", "--output", str(output), "--tag", "buyer")
    assert code == 0
    assert payload["data"]["count"] == 1
    assert "maya@example.com" in output.read_text(encoding="utf-8")

    code, payload = run_niles(tmp_path, "export", "json")
    assert code == 0
    assert "Maya Chen" in payload["data"]["content"]


def test_csv_mapping_and_fts_search(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    source = tmp_path / "mapped.csv"
    source.write_text("Full Name,Organization,Labels\nMaya Chen,Acme Analytics,buyer\n", encoding="utf-8")
    mapping = tmp_path / "mapping.toml"
    mapping.write_text('[columns]\n"Full Name" = "name"\nOrganization = "company"\nLabels = "tags"\n', encoding="utf-8")
    code, payload = run_niles(tmp_path, "import", "csv", str(source), "--mapping", str(mapping), "--commit")
    assert code == 0
    assert payload["data"]["mapping"]["Full Name"] == "name"
    code, payload = run_niles(tmp_path, "search", "Acme Analytics")
    assert code == 0
    assert payload["data"]["results"][0]["type"] == "contact"
    assert "rank" in payload["data"]["results"][0]


def test_csv_mapping_promotes_operating_fields(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    source = tmp_path / "pipeline.csv"
    source.write_text(
        "Account,Stage,Priority,Status,Action,Owner,Due,Last Touch,Asset,Asset URL\n"
        "Burns Industries,contracting,1,Waiting on signature,Ask Smithers for timing,lionel,2026-09-08,2026-08-20,Engagement Letter,https://example.invalid/engagement\n",
        encoding="utf-8",
    )
    mapping = tmp_path / "pipeline.toml"
    mapping.write_text(
        '[columns]\nAccount = "name"\nStage = "stage"\nPriority = "priority"\n'
        'Status = "current_status"\nAction = "next_action"\nOwner = "owner"\n'
        'Due = "due_date"\n"Last Touch" = "last_interaction"\n'
        'Asset = "material_title"\n"Asset URL" = "material_url"\n',
        encoding="utf-8",
    )
    code, payload = run_niles(tmp_path, "import", "csv", str(source), "--mapping", str(mapping), "--commit")
    assert code == 0
    assert payload["data"]["events_written"] == 4
    contact = run_niles(tmp_path, "contact", "show", "burns-industries", "--with-notes", "--with-tasks")[1]["data"]["contact"]
    assert contact["traits"]["stage"] == "contracting"
    assert contact["traits"]["priority"] == 1
    assert contact["traits"]["current_status"] == "Waiting on signature"
    assert contact["notes"][0]["created_at"].startswith("2026-08-20")
    assert contact["tasks"][0]["text"] == "Ask Smithers for timing"
    assert contact["tasks"][0]["assignee"] == "lionel"
    assert contact["tasks"][0]["due_date"] == "2026-09-08"
    assert run_niles(tmp_path, "material", "list")[1]["data"]["materials"][0]["title"] == "Engagement Letter"


def test_survey_templates_copy_preview_apply_and_edsl_export(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Maya Chen")[0] == 0
    code, payload = run_niles(tmp_path, "survey", "list")
    assert code == 0
    assert {item["name"] for item in payload["data"]["surveys"]} == {"debrief", "review", "intake-basic"}

    code, payload = run_niles(tmp_path, "survey", "copy", "debrief", "sales-debrief")
    assert code == 0
    assert payload["data"]["survey"]["template"] is False
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"summary": "Strong discovery call", "sentiment": "positive", "next_step": "Send proposal", "next_by": "2026-09-10", "owner": "john"}), encoding="utf-8")

    code, payload = run_niles(tmp_path, "survey", "run", "sales-debrief", "--contact", "maya-chen", "--answers", str(answers), "--dry-run")
    assert code == 0
    assert payload["data"]["events_written"] == 0
    assert run_niles(tmp_path, "note", "list", "maya-chen")[1]["data"]["notes"] == []

    code, payload = run_niles(tmp_path, "survey", "run", "sales-debrief", "--contact", "maya-chen", "--answers", str(answers))
    assert code == 0
    assert payload["data"]["events_written"] == 3
    assert run_niles(tmp_path, "contact", "show", "maya-chen")[1]["data"]["contact"]["traits"]["last_sentiment"] == "positive"
    task = run_niles(tmp_path, "task", "list")[1]["data"]["tasks"][0]
    assert task["due_date"] == "2026-09-10"
    assert task["assignee"] == "john"

    export = tmp_path / "debrief-edsl.json"
    code, payload = run_niles(tmp_path, "survey", "export-edsl", "sales-debrief", "--output", str(export))
    if importlib.util.find_spec("edsl"):
        assert code == 0
        bundle = json.loads(export.read_text(encoding="utf-8"))
        assert bundle["schema_version"] == "niles.edsl-handoff.v1"
        assert bundle["network"] is False
    else:
        assert code == 1
        assert payload["errors"][0]["code"] == "edsl_not_installed"


def register_form(tmp_path, kind, survey_name, contact_id=None):
    project = Project.open(tmp_path)
    payload = {
        "id": f"form_{kind.replace('-', '_')}",
        "kind": kind,
        "survey_name": survey_name,
        "remote_uuid": f"remote-{kind}",
        "respondent_url": "https://example.test/respond",
        "admin_url": "https://example.test/admin",
        "contact_id": contact_id,
        "recipient": "robin",
        "created_at": utc_now(),
    }
    project.append_event("form_published", payload)
    return payload["id"]


def test_intake_pull_is_quarantined_deduplicated_and_reviewed(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    form_id = register_form(tmp_path, "intake", "intake-basic")
    responses = tmp_path / "responses.json"
    responses.write_text(json.dumps({"data": [{"id": "response-1", "answer": {"name": "Maya Chen", "email": "maya@example.com", "company": "Acme", "message": "Interested in a demo"}}]}), encoding="utf-8")

    code, payload = run_niles(tmp_path, "intake", "import", form_id, str(responses))
    assert code == 0
    assert payload["data"]["received"] == 1
    assert payload["data"]["quarantined"] is True
    assert run_niles(tmp_path, "status")[1]["data"]["contacts"] == 0
    assert run_niles(tmp_path, "intake", "import", form_id, str(responses))[1]["data"]["skipped"] == 1

    submission = run_niles(tmp_path, "intake", "review")[1]["data"]["pending"][0]
    code, payload = run_niles(tmp_path, "intake", "review", submission["id"], "--accept")
    assert code == 0
    assert payload["data"]["status"] == "accepted"
    assert run_niles(tmp_path, "contact", "show", "maya@example.com", "--with-notes")[1]["data"]["contact"]["notes"][0]["kind"] == "intake"
    assert run_niles(tmp_path, "fsck")[0] == 0


def test_status_request_submission_routes_only_after_acceptance(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    contact = run_niles(tmp_path, "contact", "add", "Maya Chen")[1]["data"]["contact"]
    form_id = register_form(tmp_path, "status-request", "debrief", contact["id"])
    responses = tmp_path / "status.json"
    responses.write_text(json.dumps({"responses": [{"response_id": "status-1", "answers": {"summary": "Renewal conversation", "sentiment": "positive", "next_step": "Send renewal", "next_by": "2026-09-12", "owner": "robin"}}]}), encoding="utf-8")
    assert run_niles(tmp_path, "status-request", "import", form_id, str(responses))[0] == 0
    assert run_niles(tmp_path, "note", "list", "maya-chen")[1]["data"]["notes"] == []
    submission_id = run_niles(tmp_path, "status-request", "review")[1]["data"]["pending"][0]["id"]
    assert run_niles(tmp_path, "status-request", "review", submission_id, "--accept")[0] == 0
    assert run_niles(tmp_path, "note", "list", "maya-chen")[1]["data"]["notes"][0]["text"] == "Renewal conversation"


def test_recommendation_import_review_accept_and_reject(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    contact = run_niles(tmp_path, "contact", "add", "Maya Chen", "--tag", "prospect")[1]["data"]["contact"]
    results = tmp_path / "recommendations.json"
    results.write_text(json.dumps({"data": [{"scenario": {"contact_id": contact["id"]}, "answer": {"recommended_task": "Send the security brief", "rationale": "They are waiting on security."}}, {"scenario": {"contact_id": contact["id"]}, "answer": {"recommended_task": "Schedule executive call", "rationale": "Build sponsorship."}}]}), encoding="utf-8")
    code, payload = run_niles(tmp_path, "recommend", "import", str(results))
    assert code == 0
    assert payload["data"]["quarantined"] is True
    pending = run_niles(tmp_path, "recommend", "review")[1]["data"]["pending"]
    assert len(pending) == 2
    assert run_niles(tmp_path, "recommend", "accept", pending[0]["id"], "--assign", "john", "--due", "2026-09-15")[0] == 0
    assert run_niles(tmp_path, "recommend", "reject", pending[1]["id"])[0] == 0
    tasks = run_niles(tmp_path, "task", "list")[1]["data"]["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["tags"] == ["recommendation"]
    assert run_niles(tmp_path, "fsck")[0] == 0

    if importlib.util.find_spec("edsl"):
        job_path = tmp_path / "next-steps.ep"
        code, payload = run_niles(tmp_path, "recommend", "export", "next-steps", "--tag", "prospect", "--output", str(job_path))
        assert code == 0
        assert payload["data"]["contacts"] == 1
        from edsl import Jobs

        assert len(Jobs.load(str(job_path)).scenarios) == 1


def test_form_export_and_ep_registration_are_offline(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    survey_path = tmp_path / "intake.ep"
    code, payload = run_niles(tmp_path, "intake", "export", "intake-basic", "--output", str(survey_path))
    assert code == 0
    assert payload["data"]["network"] is False
    assert payload["data"]["publish_command"].startswith("ep humanize create")
    assert survey_path.is_file()
    registration = tmp_path / "registration.json"
    registration.write_text(json.dumps({"human_survey_uuid": "remote-intake", "respondent_url": "https://example.test/respond", "admin_url": "https://example.test/admin"}), encoding="utf-8")
    code, payload = run_niles(tmp_path, "intake", "register", "intake-basic", str(registration))
    assert code == 0
    assert payload["data"]["form"]["remote_uuid"] == "remote-intake"


def test_human_update_exports_all_entities_with_current_status_and_skip_logic(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    first = run_niles(tmp_path, "contact", "add", "Burns Industries", "--tag", "company")[1]["data"]["contact"]
    second = run_niles(tmp_path, "contact", "add", "Waylon Smithers", "--tag", "person", "--company", "Burns Industries")[1]["data"]["contact"]
    assert run_niles(tmp_path, "contact", "status", first["id"], "Contract is with Mr. Burns")[0] == 0
    assert run_niles(tmp_path, "note", "add", second["id"], "Waiting for a callback", "--at", "2026-09-01")[0] == 0

    output = tmp_path / "update_job.ep"
    code, payload = run_niles(tmp_path, "human-update", "--output", str(output))
    if not importlib.util.find_spec("edsl"):
        assert code == 1
        assert payload["errors"][0]["code"] == "edsl_not_installed"
        return

    assert code == 0
    data = payload["data"]
    assert data["entities"] == 2
    assert data["question_count"] == 11
    assert data["network"] is False
    assert data["publish_command"].startswith("ep humanize create --survey")
    assert output.is_file()
    manifest = json.loads(Path(data["manifest_path"]).read_text(encoding="utf-8"))
    assert {route["contact_id"] for route in manifest["routing"].values()} == {first["id"], second["id"]}
    assert set(manifest["disposition_rows"].values()) == {first["id"], second["id"]}

    from edsl import Survey
    from edsl.surveys import EndOfSurvey

    survey = Survey.load(str(output))
    assert survey.questions[0].question_type == "matrix"
    assert survey.questions[0].question_options == ["Current", "Follow up", "Waiting on them", "Waiting on us", "Stalled", "Won", "Lost / dead"]
    texts = [question.question_text for question in survey.questions]
    assert any("Burns Industries" in text and "Contract is with Mr. Burns" in text for text in texts)
    assert any("Waylon Smithers" in text and "Waiting for a callback" in text for text in texts)
    assert len(survey.rule_collection.to_dict()["rules"]) > len(survey.questions)
    rows = manifest["disposition_rows"]
    current_answers = {label: "Current" for label in rows}
    assert survey.next_question("entity_disposition", {"entity_disposition.answer": current_answers}) == EndOfSurvey
    second_label = next(label for label, contact_id in rows.items() if contact_id == second["id"])
    second_follow_up = {**current_answers, second_label: "Follow up"}
    assert survey.next_question("entity_disposition", {"entity_disposition.answer": second_follow_up}).question_name == f"actions_{second['id'].replace('-', '_')}"
    follow_up_answers = {**current_answers, next(label for label, contact_id in rows.items() if contact_id == first["id"]): "Follow up"}
    assert survey.next_question("entity_disposition", {"entity_disposition.answer": follow_up_answers}).question_name == f"actions_{first['id'].replace('-', '_')}"


def test_human_update_requires_entities(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    code, payload = run_niles(tmp_path, "human-update", "--output", "update_job.ep")
    if importlib.util.find_spec("edsl"):
        assert code == 1
        assert payload["errors"][0]["code"] == "no_entities"


def test_managed_exchange_hides_storage_paths_from_routine_commands(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    code, exported = run_niles(tmp_path, "intake", "export", "intake-basic")
    if not importlib.util.find_spec("edsl"):
        assert code == 1
        assert exported["errors"][0]["code"] == "edsl_not_installed"
        return

    assert code == 0
    export_data = exported["data"]
    assert export_data["managed"] is True
    assert Path(export_data["path"]).is_file()
    assert export_data["registration_path"] in export_data["publish_command"]

    Path(export_data["registration_path"]).write_text(
        json.dumps({"human_survey_uuid": "remote-managed"}), encoding="utf-8"
    )
    code, registered = run_niles(tmp_path, "intake", "register", "intake-basic")
    assert code == 0
    registration_data = registered["data"]
    assert registration_data["responses_path"] in registration_data["pull_command"]

    Path(registration_data["responses_path"]).write_text(
        json.dumps({"data": [{"id": "managed-1", "answer": {"name": "Lyle Lanley"}}]}),
        encoding="utf-8",
    )
    form_id = registration_data["form"]["id"]
    code, imported = run_niles(tmp_path, "intake", "import", form_id)
    assert code == 0
    assert imported["data"]["received"] == 1


def test_managed_recommendation_exchange_uses_name(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    contact = run_niles(tmp_path, "contact", "add", "Marge Simpson")[1]["data"]["contact"]
    if not importlib.util.find_spec("edsl"):
        return
    code, exported = run_niles(tmp_path, "recommend", "export", "client-next-step")
    assert code == 0
    data = exported["data"]
    assert data["managed"] is True
    assert data["results_path"] in data["run_command"]
    Path(data["results_path"]).write_text(
        json.dumps({"data": [{"scenario": {"contact_id": contact["id"]}, "answer": {"recommended_task": "Call Marge"}}]}),
        encoding="utf-8",
    )
    code, imported = run_niles(tmp_path, "recommend", "import", "--name", "client-next-step")
    assert code == 0
    assert imported["data"]["imported"] == 1


def test_survey_run_validation_and_additional_routes(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Maya Chen")[0] == 0

    code, payload = run_niles(tmp_path, "survey", "run", "debrief")
    assert code == 0
    assert payload["data"]["requires_answers"] is True

    missing_file = run_niles(tmp_path, "survey", "run", "debrief", "--answers", "missing.json")[1]
    assert missing_file["errors"][0]["code"] == "answers_not_found"
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"unknown": "value"}), encoding="utf-8")
    assert run_niles(tmp_path, "survey", "run", "debrief", "--answers", str(answers))[1]["errors"][0]["code"] == "unknown_answers"
    answers.write_text(json.dumps({"sentiment": "excellent"}), encoding="utf-8")
    assert run_niles(tmp_path, "survey", "run", "debrief", "--answers", str(answers))[1]["errors"][0]["code"] == "missing_answers"
    answers.write_text(json.dumps({"summary": "Call", "sentiment": "excellent"}), encoding="utf-8")
    assert run_niles(tmp_path, "survey", "run", "debrief", "--answers", str(answers))[1]["errors"][0]["code"] == "invalid_answers"
    answers.write_text(json.dumps({"summary": "Call"}), encoding="utf-8")
    assert run_niles(tmp_path, "survey", "run", "debrief", "--answers", str(answers))[1]["errors"][0]["code"] == "contact_required"

    routed = {
        "schema_version": "niles.survey.v1",
        "name": "routed",
        "version": 1,
        "questions": [
            {"name": "company", "text": "Company", "type": "text"},
            {"name": "tag", "text": "Tag", "type": "text"},
            {"name": "archive", "text": "Archive", "type": "text"},
        ],
        "routes": {
            "company": {"action": "set_field", "field": "company"},
            "tag": {"action": "add_tag"},
            "archive": {"action": "archive"},
        },
    }
    (tmp_path / ".niles" / "surveys" / "routed.json").write_text(json.dumps(routed), encoding="utf-8")
    answers.write_text(json.dumps({"company": "Acme", "tag": "buyer"}), encoding="utf-8")
    assert run_niles(tmp_path, "survey", "run", "routed", "--contact", "maya-chen", "--answers", str(answers))[0] == 0
    contact = run_niles(tmp_path, "contact", "show", "maya-chen")[1]["data"]["contact"]
    assert contact["company"] == "Acme"
    assert contact["tags"] == ["buyer"]
    answers.write_text(json.dumps({"archive": "yes"}), encoding="utf-8")
    assert run_niles(tmp_path, "survey", "run", "routed", "--contact", "maya-chen", "--answers", str(answers))[0] == 0


def test_invalid_survey_files_csv_and_duplicate_copy(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    surveys = tmp_path / ".niles" / "surveys"
    (surveys / "broken.json").write_text("{bad", encoding="utf-8")
    assert run_niles(tmp_path, "survey", "show", "broken")[1]["errors"][0]["code"] == "invalid_survey_json"
    assert run_niles(tmp_path, "survey", "show", "missing")[1]["errors"][0]["code"] == "unknown_survey"
    assert run_niles(tmp_path, "survey", "copy", "debrief", "copy")[0] == 0
    assert run_niles(tmp_path, "survey", "copy", "debrief", "copy")[1]["errors"][0]["code"] == "survey_exists"

    assert run_niles(tmp_path, "import", "csv", "missing.csv")[1]["errors"][0]["code"] == "import_not_found"
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("company\nAcme\n", encoding="utf-8")
    assert run_niles(tmp_path, "import", "csv", str(csv_path))[1]["errors"][0]["code"] == "invalid_csv"
    csv_path.write_text("name\n\n", encoding="utf-8")
    assert run_niles(tmp_path, "import", "csv", str(csv_path))[1]["errors"][0]["code"] == "invalid_csv"
    assert run_niles(tmp_path, "import", "csv", str(csv_path), "--mapping", "missing.toml")[1]["errors"][0]["code"] == "mapping_not_found"
    mapping = tmp_path / "bad.toml"
    mapping.write_text('[columns]\nname = "password"\n', encoding="utf-8")
    csv_path.write_text("name\nMaya\n", encoding="utf-8")
    assert run_niles(tmp_path, "import", "csv", str(csv_path), "--mapping", str(mapping))[1]["errors"][0]["code"] == "invalid_mapping"


def test_intake_merge_reject_close_and_review_guards(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Existing Maya", "--email", "maya@example.com")[0] == 0
    form_id = register_form(tmp_path, "intake", "intake-basic")
    responses = tmp_path / "responses.json"
    responses.write_text(json.dumps({"data": [
        {"id": "merge", "answer": {"name": "Maya", "email": "maya@example.com", "message": "Merge me"}},
        {"id": "reject", "answer": {"name": "Spam", "email": "spam@example.com"}},
    ]}), encoding="utf-8")
    assert run_niles(tmp_path, "intake", "import", form_id, str(responses))[0] == 0
    pending = run_niles(tmp_path, "intake", "review")[1]["data"]["pending"]
    assert run_niles(tmp_path, "intake", "review", pending[0]["id"])[1]["errors"][0]["code"] == "decision_required"
    merge = next(item for item in pending if item["remote_id"] == "merge")
    reject = next(item for item in pending if item["remote_id"] == "reject")
    assert run_niles(tmp_path, "intake", "review", merge["id"], "--merge", "existing-maya")[1]["data"]["status"] == "merged"
    assert run_niles(tmp_path, "intake", "review", reject["id"], "--reject")[1]["data"]["status"] == "rejected"
    assert run_niles(tmp_path, "intake", "review", reject["id"], "--reject")[1]["errors"][0]["code"] == "submission_reviewed"
    close = run_niles(tmp_path, "intake", "close", form_id)[1]
    assert close["data"]["remote_closed"] is False


def test_undo_and_recommendation_review_guards(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    contact_payload = run_niles(tmp_path, "contact", "add", "Maya Chen")[1]
    contact_event = contact_payload["data"]["event_id"]
    assert run_niles(tmp_path, "note", "add", "maya-chen", "Call")[0] == 0
    assert run_niles(tmp_path, "undo", contact_event)[1]["errors"][0]["code"] == "undo_has_dependents"
    assert run_niles(tmp_path, "undo", "missing")[1]["errors"][0]["code"] == "unknown_event"

    bad = tmp_path / "bad-results.json"
    bad.write_text("{bad", encoding="utf-8")
    assert run_niles(tmp_path, "recommend", "import", str(bad))[1]["errors"][0]["code"] == "invalid_recommendation_results"
    assert run_niles(tmp_path, "recommend", "accept", "missing")[1]["errors"][0]["code"] == "unknown_recommendation"

    results = tmp_path / "results.json"
    contact_id = contact_payload["data"]["contact"]["id"]
    results.write_text(json.dumps({"data": [{"scenario": {"contact_id": contact_id}, "answer": {"recommended_task": "Follow up"}}]}), encoding="utf-8")
    recommendation_id = run_niles(tmp_path, "recommend", "import", str(results))[1]["data"]["recommendation_ids"][0]
    assert run_niles(tmp_path, "recommend", "reject", recommendation_id)[0] == 0
    assert run_niles(tmp_path, "recommend", "reject", recommendation_id)[1]["errors"][0]["code"] == "recommendation_reviewed"
