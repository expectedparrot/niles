# niles

![Niles artwork](docs/assets/niles-artwork.png)

`niles` is a local-first CRM CLI for relationship work: contacts, notes, tasks, lightweight reports, and EDSL-powered intake flows. It writes an append-only local event log, projects it into SQLite, and returns machine-readable JSON envelopes so other tools can call it safely.

Docs: https://expectedparrot.github.io/niles/

License: MIT. The code and bundled Niles artwork are MIT licensed.

## Codex Agent Block

Copy and paste this whole block into Codex, Claude Code, or another coding agent:

```text
You are working with niles, a local-first CRM CLI for relationship work.

Install niles from GitHub:
1. Run: git clone https://github.com/expectedparrot/niles.git
2. Run: cd niles
3. For core local CRM commands, run: python -m pip install -e .
4. For EDSL/Expected Parrot workflows, run: python -m pip install -e ".[edsl]"
5. If the edsl extra is unavailable in the current environment, run: python -m pip install edsl
6. Verify installation with: niles version

Install and register EDSL / EP:
1. Create or log in to an Expected Parrot account.
2. Register the API key in the shell environment as EXPECTED_PARROT_API_KEY.
3. Never store, print, commit, or write the API key into CRM notes, events, docs, examples, or git history.
4. Verify EDSL with: python -c "import edsl; print(edsl.__version__)"
5. Verify the EP CLI with: ep --help
6. Niles core commands do not need network access. EDSL/EP is needed for humanize-powered status requests, intake flows, and .ep recommendation jobs.

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
1. Initialize a CRM project: niles init
2. Inspect state: niles status
3. Add a company/contact: niles contact add "Acme Data" --tag prospect --trait source=warm_intro --trait priority=1 --cadence-days 14
4. Add a person/contact: niles contact add "Maya Chen" --company "Acme Data" --role "VP Data" --email maya@acmedata.example --tag buyer
5. Show a contact: niles contact show <id-or-slug>
6. List contacts: niles contact list --tag prospect
7. Add a note: niles note add <contact-ref> "Robin introduced us. Maya wants a short technical proof before budget review." --kind call
8. Add a task: niles task add <contact-ref> "Send proof outline and two relevant customer examples" --due YYYY-MM-DD --assign john --tag next-step
9. List tasks: niles task list --status open --assignee john
10. Complete a task: niles task done <task-id> --note "What happened"

EDSL job workflow:
- Niles should export .ep jobs for model/human work.
- Run exported jobs with ep.
- Import audited results back into Niles.
- Do not invent CRM mutations from model output until a niles import/review/accept command records them.

For niles source development:
- Run tests from the niles source checkout with: python -m pytest
- Read SPEC.md before changing command semantics, event shapes, survey routing, EDSL handoff behavior, or JSON envelope structure.
```
