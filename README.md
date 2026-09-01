# niles

![Niles artwork](docs/assets/niles-artwork.png)

`niles` is a local-first CRM CLI for relationship work: contacts, notes, tasks, lightweight reports, and EDSL-powered intake flows. It writes an append-only local event log, projects it into SQLite, and returns machine-readable JSON envelopes so other tools can call it safely.

Docs: https://expectedparrot.github.io/niles/

License: MIT. The code and bundled Niles artwork are MIT licensed.

## Install

```bash
git clone https://github.com/expectedparrot/niles.git
cd niles
python -m pip install -e .
```

## Install With EDSL / EP

Niles core commands do not need network access. EDSL/Expected Parrot support is needed for humanize-powered status requests, intake flows, and `.ep` recommendation jobs.

```bash
git clone https://github.com/expectedparrot/niles.git
cd niles
python -m pip install -e ".[edsl]"
```

Create or log in to an Expected Parrot account, then register the API key in your shell environment:

```bash
export EXPECTED_PARROT_API_KEY="your-ep-api-key"
```

Confirm that EDSL imports and the `ep` command is available:

```bash
python -c "import edsl; print(edsl.__version__)"
ep --help
```

## Start A CRM

```bash
mkdir acme-crm
cd acme-crm
niles init
```

## Add Contacts One By One

```bash
niles contact add "Acme Data" \
  --tag prospect \
  --trait source=warm_intro \
  --trait priority=1 \
  --cadence-days 14

niles contact add "Maya Chen" \
  --company "Acme Data" \
  --role "VP Data" \
  --email maya@acmedata.example \
  --tag buyer
```

## Track Notes And Tasks

```bash
niles note add acme-data "Robin introduced us. Maya wants a short technical proof before budget review." --kind call

niles task add acme-data "Send proof outline and two relevant customer examples" \
  --due 2026-09-05 \
  --assign john \
  --tag next-step

niles task list --assignee john --status open
```

## Agent Instructions

Use this block when giving Codex, Claude Code, or another coding agent access to a Niles CRM project:

```text
You are working with niles, a local-first CRM CLI.

Project boundary:
- Run niles commands from the CRM project directory, not from the niles source checkout unless you are developing niles itself.
- Durable CRM state lives under .niles/.
- Do not edit .niles/events/ manually. Use niles commands so the event log and SQLite projection stay consistent.
- Treat .niles/index/niles.sqlite as rebuildable derived state.

Command contract:
- Prefer niles CLI commands over direct file edits.
- Every command prints one JSON envelope to stdout.
- Check envelope.status before assuming success.
- If envelope.status is "error", read envelope.error.code, envelope.error.message, and envelope.next_steps.
- Use stable ids or unambiguous slugs returned by previous envelopes when mutating contacts, notes, or tasks.

Core CRM commands:
- Initialize: niles init
- Inspect: niles status
- Add contact: niles contact add "Name" --email person@example.com --tag prospect --trait priority=1
- Show contact: niles contact show <id-or-slug>
- List contacts: niles contact list [--tag prospect]
- Add note: niles note add <contact-ref> "Note text" --kind call
- Add task: niles task add <contact-ref> "Task text" --due YYYY-MM-DD --assign john --tag next-step
- List tasks: niles task list --status open [--assignee john]
- Complete task: niles task done <task-id> --note "What happened"

EDSL / EP setup:
- Install Niles with EDSL support from GitHub:
  git clone https://github.com/expectedparrot/niles.git
  cd niles
  python -m pip install -e ".[edsl]"
- If needed, install EDSL directly:
  python -m pip install edsl
- Expected Parrot auth is delegated to EDSL/EP.
- Do not store or print API keys in CRM notes, events, docs, or git.
- The shell should provide EXPECTED_PARROT_API_KEY for commands that need the EP server:
  export EXPECTED_PARROT_API_KEY="..."
- Verify setup:
  python -c "import edsl; print(edsl.__version__)"
  ep --help

EDSL job workflow:
- Niles should export .ep jobs for model/human work.
- Run exported jobs with ep.
- Import audited results back into Niles.
- Do not invent CRM mutations from model output until a niles import/review/accept command records them.
```

## Test

```bash
python -m pytest
```

The v1 implementation is intentionally small. See `SPEC.md` for the planned EDSL survey, status-update, recommendation, teammate, and reporting flows.
