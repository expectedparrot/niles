# niles

![Niles artwork](docs/assets/niles-artwork.png)

`niles` is a local-first CRM CLI for relationship work: contacts, notes, tasks, lightweight reports, and EDSL-powered intake flows. It writes an append-only local event log, projects it into SQLite, and returns machine-readable JSON envelopes so other tools can call it safely.

Docs: https://expectedparrot.github.io/niles/

License: MIT. The code and bundled Niles artwork are MIT licensed.

## Codex Agent Block

Copy and paste this whole block into Codex, Claude Code, or another coding agent:

```text
You are working with niles, a local-first CRM CLI for relationship work.

Install:
- For core CRM use, run: python -m pip install "niles @ git+https://github.com/expectedparrot/niles.git"
- For EDSL/Expected Parrot workflows, run: python -m pip install "niles[edsl] @ git+https://github.com/expectedparrot/niles.git"
- If the edsl extra is unavailable in the current environment, run: python -m pip install edsl
- Verify with: niles version
- First agent command after install or when returning to a project: niles agent next

EP / EDSL registration:
- Create or log in to an Expected Parrot account.
- Register the API key in the shell environment as EXPECTED_PARROT_API_KEY.
- Never store, print, commit, or write the API key into CRM notes, events, docs, examples, or git history.
- Verify EDSL with: python -c "import edsl; print(edsl.__version__)"
- Verify the EP CLI with: ep --help
- Niles core commands do not need network access. EDSL/EP is needed for humanize-powered status requests, intake flows, and .ep recommendation jobs.

How Niles works:
- Niles is an agent-friendly CRM command layer, not a hosted CRM.
- The agent records relationship state as contacts, notes, and tasks.
- The durable source of truth is an append-only event log under .niles/events/.
- SQLite under .niles/index/ is a rebuildable projection for listing, lookup, and reporting.
- Every command emits one JSON envelope to stdout with status, data, errors, warnings, and next_steps.
- Portable setup uses niles export/import zip archives; archives include durable .niles state and rebuild the SQLite index after import.
- EDSL/EP work is explicit: niles exports .ep jobs or humanize requests, ep runs them, and niles imports reviewed results.

When to use Niles:
- Use it when the user wants a durable local CRM that an agent can operate through shell commands.
- Use it to add contacts one by one, capture interaction notes, assign next-step tasks, recover stale relationships, and prepare reports.
- Use EDSL/EP handoffs when a humanize survey, intake form, or recommendation job should update the CRM through reviewable results.

Decision rule:
- If the task is relationship tracking, outreach follow-up, lightweight pipeline review, or team task coordination, use niles.
- If the user only wants prose advice and no durable CRM state, answer directly instead.
- If model or human survey work is needed, export/run/import through EDSL/EP rather than inventing local CRM mutations from unreviewed model output.

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
- If you lose track, run: niles agent next

Core CRM commands:
1. Initialize a CRM project: niles init
2. Inspect state: niles status
3. Ask what to do next: niles agent next
4. Add a company/contact: niles contact add "Acme Data" --tag prospect --trait source=warm_intro --trait priority=1 --cadence-days 14
5. Add a person/contact: niles contact add "Maya Chen" --company "Acme Data" --role "VP Data" --email maya@acmedata.example --tag buyer
6. Show a contact: niles contact show <id-or-slug>
7. List contacts: niles contact list --tag prospect
8. Add a note: niles note add <contact-ref> "Robin introduced us. Maya wants a short technical proof before budget review." --kind call
9. Add a task: niles task add <contact-ref> "Send proof outline and two relevant customer examples" --due YYYY-MM-DD --assign john --tag next-step
10. List tasks: niles task list --status open --assignee john
11. Complete a task: niles task done <task-id> --note "What happened"
12. Export a CRM for another context: niles export niles-crm.zip
13. Import a CRM in a fresh directory: niles import /path/to/niles-crm.zip
14. Replace an existing local CRM only when the user explicitly asks: niles import /path/to/niles-crm.zip --replace

EDSL job workflow:
- Niles should export .ep jobs for model/human work.
- Run exported jobs with ep.
- Import audited results back into Niles.
- Do not invent CRM mutations from model output until a niles import/review/accept command records them.

Common pitfalls:
- Do not treat the source checkout as the CRM project unless the user explicitly wants that.
- Do not edit .niles/events/ directly.
- Do not assume a fuzzy contact reference is safe when multiple contacts may match.
- Do not store secrets in CRM state.
- Do not use niles import --replace unless the user explicitly wants to overwrite the destination .niles state.
- Do not treat planned EDSL commands as available unless niles agent next or niles --help shows them in the installed version.

For niles source development:
- Run tests from the niles source checkout with: python -m pytest
- Read SPEC.md before changing command semantics, event shapes, survey routing, EDSL handoff behavior, or JSON envelope structure.
```
