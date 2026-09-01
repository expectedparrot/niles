from __future__ import annotations

import json
import os
import subprocess
import sys
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


def test_ambiguous_contact_ref_blocks_mutation(tmp_path):
    assert run_niles(tmp_path, "init")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Ronnie Chatterjee")[0] == 0
    assert run_niles(tmp_path, "contact", "add", "Robbie Chatterjee")[0] == 0

    code, payload = run_niles(tmp_path, "note", "add", "chatterjee", "Follow up")
    assert code == 1
    assert payload["status"] == "error"
    assert payload["errors"][0]["code"] == "ambiguous_reference"
    assert len(payload["data"]["candidates"]) == 2
