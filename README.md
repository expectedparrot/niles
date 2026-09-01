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

## Test

```bash
python -m pytest
```

The v1 implementation is intentionally small. See `SPEC.md` for the planned EDSL survey, status-update, recommendation, teammate, and reporting flows.
