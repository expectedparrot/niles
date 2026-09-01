# niles

![Niles artwork](docs/assets/niles-artwork.png)

`niles` is a local-first CRM CLI for relationship work: contacts, notes, tasks, lightweight reports, and EDSL-powered intake flows. It writes an append-only local event log, projects it into SQLite, and returns machine-readable JSON envelopes so other tools can call it safely.

Docs: https://expectedparrot.github.io/niles/

License: MIT. The code and bundled Niles artwork are MIT licensed.

## How It Works

Niles is a filesystem CRM. A Niles project is just a directory with a `.niles/` folder inside it. You can put that directory in git, push it to GitHub, clone it somewhere else, and rebuild the local working index from the files.

The durable state is append-only:

```text
.niles/
  manifest.json          project identity and storage contract
  .gitignore             ignores local derived indexes
  events/                append-only JSON event log
  surveys/               EDSL survey and job definitions
  reports/               generated reports you may choose to commit
  index/                 disposable local SQLite projection
```

The event log is the source of truth. Commands like `niles contact add`, `niles note add`, and `niles task done` write new JSON event files under `.niles/events/`. Niles then replays those events into `.niles/index/niles.sqlite` so lookup, lists, and reports are fast.

SQLite is not the backend. It is a cache. If you delete `.niles/index/`, clone the repository on another machine, or pull new events from GitHub, run:

```bash
niles rebuild-index
```

To check that the filesystem state is healthy:

```bash
niles fsck
```

`fsck` validates the project manifest, event JSON, event sequence, duplicate event ids, supported event types, and replay integrity. If it passes, the CRM can be reconstructed from the committed files.

## GitHub As Backend

Because the durable state is plain files, GitHub can act as the sync and provenance layer:

```bash
git add .niles/manifest.json .niles/.gitignore .niles/events .niles/surveys .niles/reports
git commit -m "Update CRM"
git push
```

Do not commit `.niles/index/`. Niles writes `.niles/.gitignore` with `index/` during `niles init`, and this source repo also ignores `.niles/index/`.

This gives you normal git affordances for CRM history: diffs, commits, branches, pull requests, blame, and rollback strategies. Niles itself still owns CRM mutations; git stores and syncs the results.

## Command Model

Every command prints one JSON envelope to stdout. Agents should check `status`, read `data`, and follow `next_steps`. Errors are structured and nonzero.

```bash
niles agent next
niles status
niles contact add "Acme Data" --tag prospect --trait priority=1
niles note add acme-data "Intro call. Waiting on security review." --kind call
niles task add acme-data "Send security language" --due 2026-09-05 --assign john
niles report status --html status.html
```

Agents should not query `.niles/index/niles.sqlite` directly. If the CLI does not expose the needed view, that is a missing Niles feature, not a reason to bypass the command layer.

## Moving Contexts

Git is the preferred backend when you want provenance. Zip archives are useful when you want to hand a CRM state to another agent/session without setting up a repo:

```bash
niles export niles-crm.zip
mkdir next-context
cd next-context
niles import /path/to/niles-crm.zip
niles fsck
```

The archive includes durable `.niles` state and excludes the SQLite projection. Import rebuilds the projection locally.

## EDSL / EP Handoff

Niles core CRM work is local and offline. EDSL and Expected Parrot enter only for explicit survey, humanize, intake, status-request, and recommendation workflows.

The design rule is: Niles prepares or ingests, EP runs. For example, recommendation work should export an `.ep` job, run through `ep`, then import reviewed results back into Niles. Enrichment follows the same containment rule: the research agent does the web searching, then records reviewed findings with `niles enrich ingest`.

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
- Git can be the backend: commit .niles/manifest.json, .niles/events/, .niles/surveys/, selected .niles/reports/, and .niles/.gitignore.
- SQLite under .niles/index/ is a local rebuildable projection for listing, lookup, and reporting.
- Every command emits one JSON envelope to stdout with status, data, errors, warnings, and next_steps.
- Portable setup uses niles export/import zip archives; archives include durable .niles state and rebuild the SQLite index after import.
- Agents must not read .niles/index/niles.sqlite directly; use Niles commands for notes, tasks, reports, and exports.
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
- Commit durable .niles files when the user wants persistence, sync, review, or provenance through GitHub.
- Do not commit .niles/index/.
- Treat .niles/index/niles.sqlite as rebuildable derived state, not an agent API.
- Do not query .niles/index/niles.sqlite directly. If a needed view is missing, ask for a Niles command to be added rather than reaching into the database.

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
4. Verify event-log health: niles fsck
5. Rebuild the disposable SQLite projection after clone/pull/cache deletion: niles rebuild-index
6. Add a company/contact: niles contact add "Acme Data" --tag prospect --trait source=warm_intro --trait priority=1 --cadence-days 14
7. Add a person/contact: niles contact add "Maya Chen" --company "Acme Data" --role "VP Data" --email maya@acmedata.example --tag buyer
8. Show a contact: niles contact show <id-or-slug>
9. Show notes inline: niles contact show <id-or-slug> --with-notes
10. List contacts: niles contact list --tag prospect
11. Update tags: niles contact tag <contact-ref> --add dead --remove prospect
12. Archive a contact: niles contact archive <contact-ref> --reason "No active path"
13. Merge duplicates: niles contact merge <keep-ref> <duplicate-ref> --note "Duplicate"
14. Add a note: niles note add <contact-ref> "Robin introduced us. Maya wants a short technical proof before budget review." --kind call
15. List notes: niles note list <contact-ref> --limit 10
16. Add a task: niles task add <contact-ref> "Send proof outline and two relevant customer examples" --due YYYY-MM-DD --assign john --tag next-step
17. List tasks: niles task list --status open --assignee john
18. Reassign a task: niles task reassign <task-id> robin
19. Cancel a task: niles task cancel <task-id> --note "Waiting on them"
20. Suggest missing tasks: niles task suggest --assignee john
21. Save company context: niles org context set "What our company does" --name "Expected Parrot"
22. Add shareable material: niles material add "GTM deck" --url https://example.com/deck --tag sales
23. Ingest researched enrichment after the agent does the research: niles enrich ingest <contact-ref> "Researched claim or profile note" --source-url https://example.com/source --confidence 0.8
24. Generate an HTML status report: niles report status --html status.html
25. Export a CRM for another context: niles export niles-crm.zip
26. Import a CRM in a fresh directory: niles import /path/to/niles-crm.zip
27. Replace an existing local CRM only when the user explicitly asks: niles import /path/to/niles-crm.zip --replace

EDSL job workflow:
- Niles should export .ep jobs for model/human work.
- Run exported jobs with ep.
- Import audited results back into Niles.
- Do not invent CRM mutations from model output until a niles import/review/accept command records them.
- For enrichment, the agent does the searching outside Niles, then records reviewed findings with niles enrich ingest.

Common pitfalls:
- Do not treat the source checkout as the CRM project unless the user explicitly wants that.
- Do not edit .niles/events/ directly.
- Do not inspect or query .niles/index/niles.sqlite directly.
- Do not commit .niles/index/; it is binary derived state.
- Do not assume a fuzzy contact reference is safe when multiple contacts may match.
- Do not store secrets in CRM state.
- Do not use niles import --replace unless the user explicitly wants to overwrite the destination .niles state.
- Do not treat planned EDSL commands as available unless niles agent next or niles --help shows them in the installed version.

For niles source development:
- Run tests from the niles source checkout with: python -m pytest
- Read SPEC.md before changing command semantics, event shapes, survey routing, EDSL handoff behavior, or JSON envelope structure.
```
