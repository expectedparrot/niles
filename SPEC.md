# niles — local-first CRM CLI with EDSL-powered surveys and intake

> Package and CLI executable: **niles**.

**Status:** Draft v0.1 · **Target:** v1 scope only unless marked otherwise.

---

## 1. Purpose

niles is a local-first CLI and Python library for managing a network of
contacts: who you know, your history with them, what you promised, and who
you are neglecting. It uses EDSL as its survey substrate in three ways:

1. **Internal check-in surveys** — structured prompts the *user* answers in
   the terminal (post-interaction debriefs, periodic reviews) whose answers
   route directly into the data model.
2. **Customer-facing intake forms** — niles exports Survey objects; `ep`
   publishes them via `humanize()` and retrieves responses; niles registers
   the returned metadata and imports reviewed results as contacts.
3. **(v2) LLM operations** — enrichment, labeling, and summarization packaged
   as `.ep` EDSL job artifacts, following the bewley rule: niles *exports*
   jobs and *imports* audited results, but never executes model calls itself.

The core loop it must make effortless: log an interaction → answer a short
debrief → next steps become tasks → staleness surfaces neglected
contacts → a review survey processes them.

## 2. Design principles (ecosystem conventions)

Follow bewley's conventions unless a CRM-specific reason forces a deviation:

- **Local-first and network-contained.** All durable state lives under
  `.niles/`. Niles never authenticates with EP or makes network calls. It
  exports artifacts and imports or registers files; the `ep` CLI exclusively
  owns humanize publishing, response retrieval, and recommendation execution.
- **Event log as source of truth.** `.niles/events/` is an append-only log of
  every mutation. `.niles/index/niles.sqlite` is a rebuildable projection
  (`niles rebuild-index`). `niles fsck` verifies integrity. `niles history` and
  `niles undo` inspect and compensate events; nothing silently repairs,
  replaces, or deletes registered events. For a CRM this doubles as the
  interaction audit trail, and makes "when did this field change and why"
  answerable — including "this field was set by intake submission X."
- **GitHub can be the backend.** A CRM project may be a normal git repository.
  Commit durable `.niles/` files (`manifest.json`, `events/`, `surveys/`,
  material metadata, selected reports) and ignore `.niles/index/`. Cloning or
  pulling the repository plus `niles rebuild-index` recreates the local binary
  projection. `niles sync` owns the safe durable path list, commits only those
  paths, and optionally pushes; it never stages unrelated working-tree files.
  Sync also maintains a deterministic human-readable projection inside marked
  sections of the root `README.md`, preserving content outside those markers.
- **Agent-first output contract.** Every command emits exactly one versioned
  JSON envelope to stdout: `schema_version`, `status` (`ok`/`error`),
  `command`, `argv`, `data`, `warnings`, `errors`, `next_steps`. Failures
  exit nonzero with structured errors. Each `next_steps` entry declares
  `mutates`, `network`, and `requires_approval` flags. Human-readable output
  is opt-in via `--human`/`-H`. `niles capabilities` and
  `niles agent schema envelope` expose the contract; versioned JSON Schemas
  ship with the package.
- **State-aware guidance.** `niles guide` explains the lifecycle;
  `niles next` recommends the next action given current state (e.g. "3 intake
  submissions pending review", "5 contacts stale past cadence").
- **EDSL objects at the boundaries.** Surveys are EDSL `Survey` objects.
  Intake results arrive in EDSL's Results format. Contacts export to
  `ScenarioList` (and, v2, to `AgentList`). Everything supports
  `to_dict`/`from_dict`.
- **Delegated auth and execution.** The `ep` CLI owns Expected Parrot
  authentication and every network operation. Niles never reads, stores, or
  checks EP keys. It only exports EDSL artifacts and imports or registers
  local files produced by `ep`.
- **Network is explicitly review-gated.** Humanize-powered intake and status
  requests can fetch data from people, but pulled records are quarantined
  until review. v2 recommendation jobs export `.ep` artifacts for `ep run`;
  imported model results enter a review queue before any CRM mutation.
- **Packaging.** `pyproject.toml` + `uv`, `src/niles/` layout, `tests/`,
  `Makefile`, `AGENTS.md`/`CLAUDE.md`, `SPEC.md` (this file), `design/` for
  design notes. Python 3.11+. MIT license.

## 3. Concepts and data model

All entities are event-sourced; the fields below describe the projected
(indexed) shape. All ids are stable ULIDs. All timestamps are ISO-8601 UTC.

### 3.1 Contact

- `id`
- `name` (required, the only required field)
- `emails: list[str]`, `phones: list[str]` — lists, because people have
  several; first entry is primary. Emails are the dedup key for intake.
- `company`, `role` — plain optional strings.
- `traits: dict[str, str|num|bool]` — open-ended extras ("timezone",
  "met_at", "spouse"). Mirrors EDSL `Agent(traits=...)`; this is what
  `to_scenario_list()` / (v2) `to_agent()` project from.
- `tags: list[str]` — freeform, lowercase, no hierarchy in v1.
- `cadence_days: int | null` — desired contact frequency; drives staleness.
- `archived: bool` — soft delete. `niles contact delete --hard` exists for
  genuine erasure (privacy requests) and logs a redaction event.
- Derived, not stored: `last_touched` (max timestamp of notes/interactions),
  `staleness` (`now - last_touched` vs `cadence_days` or a global default).

### 3.2 Note (interaction record)

- `id`, `contact_id`, `created_at`
- `kind`: `note | call | meeting | email | intake | debrief` — `intake` and
  `debrief` mark machine-written notes so provenance is always visible.
- `text`
- `source`: `user | routing | import` plus a pointer (e.g. submission id,
  survey response id) when not user-typed.

### 3.3 Task

- `id`, `contact_id | null` (null = general todo)
- `assignee_id | null` — points at a teammate (§3.4) when the owner is known.
- `due_date | null`
- `text`
- `status`: `open | done | blocked | cancelled`
- `tags: list[str]`
- `source` (as above — a task created by a debrief answer points at that
  response).

Reminders are a view over open tasks with due dates, not a separate entity.

### 3.4 Teammate

- `id`
- `name`
- `aliases: list[str]` — e.g. `john`, `JJH`, `robin`, `RBY`.
- `email | null`
- `role | null`

Teammates exist so notes, tasks, status requests, and reports can track who is
doing what without burying ownership in free text.

### 3.5 Org context

- `name`
- `context`
- `traits: dict[str, str|num|bool]`

Org context is local project state used by reports, templates, and v2
recommendation job export. It should not be treated as authentication or
remote account configuration.

### 3.6 Survey definitions

A niles survey definition = an EDSL `Survey` + a **routing map** (§5).
Stored as versioned objects under `.niles/surveys/` with ids and names.
Two kinds:

- **Templates** shipped with the package: `debrief` (post-interaction),
  `review` (stale-contact triage), `intake-basic` (name/email/company/
  message). Users copy and edit (`niles survey copy debrief my-debrief`).
- **User-defined**: authored in Python against the library API or imported
  from a serialized dict.

v1 surveys are static (no LLM-generated questions). Skip logic uses EDSL's
native skip/stop rules (e.g. answering "archive" in a review skips the
follow-ups).

### 3.7 Intake submission

- `id`, `form_id`, `received_at`, `raw_answers: dict`
- `status`: `pending | accepted | merged | rejected`
- `matched_contact_id | null` — set by dedup when an email matches an
  existing contact.
- Submissions are quarantined: nothing writes to contacts until review (§6.3).

## 4. CLI surface (v1)

Verb-noun, mirroring bewley's flat style. All commands emit the JSON
envelope; representative examples only — full flags via `--help`.

```
niles init                                  # create project (.niles/)
niles status                                # counts, stale contacts, pending intake/updates
niles sync [--message m] [--no-push] [--dry-run]
niles fsck                                  # verify manifest, event log, replay, projections
niles rebuild-index                         # rebuild disposable sqlite projection
niles guide | niles next | niles capabilities

niles contact add "Jane Doe" --email jane@acme.com --company Acme --tag prospect
niles contact show <ref>                    # ref = id | email | fuzzy name
niles contact show <ref> --with-notes [--with-tasks]
niles contact list [--tag t] [--stale] [--json is the default; --human for tables]
niles contact edit <ref> --set role="CTO" --trait timezone=ET
niles contact update <ref> [--name n] [--company c] [--role r] [--trait k=v]
niles contact status <ref> <status> [--at timestamp]
niles contact tag <ref> [--add tag] [--remove tag]
niles contact merge <ref-keep> <ref-dup>
niles contact archive <ref> | niles contact delete <ref> --hard

niles note add <ref> "Called re renewal" [--kind call] [--debrief]
niles note list [<ref>] [--limit n]
niles task add [<ref>] "Send proposal" --due 2026-09-05 [--assign john]
niles task list [--due this-week] [--assignee robin] [--status open]
niles task done <id> [--note "Sent proposal"]
niles task update <id> [--text t] [--due d] [--assign a] [--status open|done|blocked|cancelled]
niles task reassign <id> <assignee>
niles task cancel <id> [--note text]
niles task suggest [--assignee john]

niles org set --name "Expected Parrot" --context <text>
niles org context set <text> [--name "Expected Parrot"] [--trait k=v]
niles org context show
niles teammate add "John Horton" --alias john --alias JJH
niles teammate list | niles teammate show <ref>
niles material add "Deck" [--path p | --url u] [--tag sales]
niles material list [--tag sales]
niles enrich ingest <ref> <text> [--source-url u] [--confidence x]

niles search <terms>                        # FTS over names, notes, traits
niles import csv <path> [--mapping m.toml]  # column→field mapping, dry-run default
niles export csv|json [--tag t]
niles export <archive.zip>                  # portable .niles state archive
niles import <archive.zip> [--replace]      # restore archive and rebuild index

niles survey list | show | copy | edit
niles survey run <name> [--contact <ref>]   # interactive terminal Q&A → routed answers
niles review [--stale]                      # sugar: survey run review over stale contacts

niles intake export <survey-name> [--output <survey.ep>]
niles intake register <survey-name> [<ep-registration.json>]
niles intake import <form-id> [<responses.ep>] # import into pending queue
niles intake review                         # triage: accept / edit / merge / reject
niles intake status | close <form-id>

niles status-request export <survey-name> [--output <survey.ep>]
niles status-request register <survey-name> [<registration.json>] --contact <ref> --recipient <ref>
niles status-request import <form-id> [<responses.ep>]
niles status-request review                 # accept / edit / reject learned updates
niles human-update --scope pipeline|people|organizations|all [--tag t] [--stage s]
                   [--entity-id id] [--include-archived] --output <update-job.ep>
niles sheet export [--scope pipeline|people|organizations|all] --output <crm.xlsx>
niles sheet import <crm.xlsx>
niles sheet review [<change-set-id> --accept|--reject]

niles report pipeline | activity | neglect | tasks
niles report status --html status.html

niles history [--contact <ref>] | niles undo <event-id>
niles fsck | niles rebuild-index
niles version
```

**Contact references:** exact id wins; exact email wins; otherwise fuzzy name
match — a unique fuzzy match proceeds, multiple matches return an `error`
envelope listing candidates (never guess on a mutating command).

**Terminal survey UX:** `niles survey run` renders EDSL questions as
sequential prompts (numbered options for multiple choice, free text
otherwise). With `--no-input` it emits the question list instead, so agents
can drive it via `--answers answers.json`.

## 5. The routing layer

The piece that makes surveys *do* something. A routing map binds each
`question_name` to a destination:

```toml
# routing for the "debrief" template
[route.sentiment]   action = "set_trait"     trait = "last_sentiment"
[route.summary]     action = "append_note"   kind = "debrief"
[route.next_step]   action = "create_task"   text_from = "answer"
[route.next_by]     action = "task_due"      binds = "next_step"
[route.owner]       action = "task_assignee" binds = "next_step"
[route.outcome]     action = "noop"          # recorded in the event log only
```

Rules:

- **Closed action vocabulary.** v1 actions: `set_field`, `set_trait`,
  `append_note`, `create_task`, `task_due`, `task_assignee`, `add_tag`,
  `archive`, `noop`. A survey cannot express an action outside this list — this is the
  containment guarantee that makes intake routing safe, and (v2) makes
  LLM-generated surveys safe: generation may compose only these actions.
- **Deterministic and previewable.** `niles survey run --dry-run` and
  `niles intake review` both show the exact mutations a response would
  produce before committing. Every applied route logs an event pointing at
  the response.
- **Validation at definition time.** `niles survey edit`/import fails fast if
  a route references a missing question or an unknown action.
- **Intake restrictions.** Intake routing maps may not use `archive` or
  `set_field` on protected fields; submissions can only create/annotate,
  never destroy. Enforced at publish time.

## 6. Intake via humanize

### 6.1 Export and register

`niles intake export <survey> [--output <survey.ep>]`:

1. Validates intake routing restrictions.
2. Writes an EDSL Survey artifact without network access. By default Niles
   manages the exchange path; `--output` is an advanced override.
3. Prints the exact `ep humanize create` command to run, including where its
   registration output belongs.

The user or agent runs `ep humanize create` and records its JSON output. Then
`niles intake register <survey> [<registration.json>]` records the remote UUID,
respondent URL, and admin URL in an event. Niles never reads EP credentials.

The published form must include a purpose/consent line; the `intake-basic`
template ships with one and export warns if a custom survey lacks a
question or description tagged `consent`.

### 6.2 Retrieve and import

The user or agent runs the exact `pull_command` returned by registration.
`niles intake import <form-id> [<responses.ep>]` reads the managed or explicit EDSL Results
artifact and writes records to the pending queue. JSON Results are also
accepted. Import is idempotent: repeated imports never duplicate submissions.

### 6.3 Review queue

`niles intake review` walks pending submissions (itself a survey run):

- Shows raw answers + the previewed mutations from the routing map.
- Dedup: if a submitted email matches an existing contact, default proposal
  is **merge** (append note + fill blank fields), never overwrite non-blank
  fields without an explicit `edit`.
- Choices: `accept` (apply routes, create/merge contact), `edit` (adjust
  values, then apply), `reject` (keep the submission event, apply nothing).
- Nothing reaches the contact store without passing through this gate.

### 6.4 Trust and privacy posture

- Submission text is **untrusted data**. It is stored verbatim, marked with
  `source: intake`, and never interpolated into any LLM prompt in v1 (there
  are no LLM calls in v1). v2 LLM features must treat `intake`-sourced text
  as data-only context and keep any job that reads it read-only or
  human-approved — this is the prompt-injection boundary.
- Intake data transits and rests on the Expected Parrot server when the user
  invokes `ep humanize`; Niles only stores imported local copies.
- `niles contact delete --hard` plus `niles intake purge <submission-id>`
  provide genuine local erasure (logged as redaction events with content
  removed). Server-side deletion is out of niles's control; document the
  EP retention story rather than promising more than we can deliver.

## 7. Status requests via humanize

Status requests are targeted humanize surveys for learning what changed with a
known contact/account from a known respondent (often a teammate). They reuse the
intake safety model but route into existing CRM records instead of creating new
contacts by default.

### 7.1 Export and register

`niles status-request export <survey> [--output <survey.ep>]` writes the local
artifact. After `ep humanize create`, the command:

`niles status-request register <survey> [<registration.json>] --contact <ref>
--recipient <ref>` records the remote metadata, target contact, and recipient.

### 7.2 Import and review

After `ep humanize responses` retrieves an artifact, `niles status-request
import <form-id> [<responses.ep>]` writes it to a pending status-update queue.
Review shows raw answers and the exact routed mutations.
Accepted updates may append notes, set traits, and create assigned tasks.
Rejected updates remain in history but apply no CRM changes.

## 8. Recommendation jobs (`.ep` handoff)

v2 recommendation features never execute model calls inside niles. The workflow
is:

```
niles recommend export next-steps --tag prospect
# run the returned run_command
niles recommend import --name next-steps
niles recommend accept <recommendation-id> --assign john --due 2026-09-03
```

Rules:

- Exported `.ep` artifacts contain EDSL survey/job definitions plus scenario
  projections from local CRM state, not secrets.
- Imported recommendation results enter a pending review queue.
- Accepting a recommendation is the only step that can create notes, tasks, or
  trait updates.
- Every accepted recommendation logs provenance linking the source `.ep`
  artifact and imported results file.

## 9. Reports

Reports are read-only projections over the event log and SQLite index. They do
not create new CRM state unless explicitly exported to a user path.

Representative reports:

```
niles report pipeline --group priority
niles report activity --assignee robin --since 2026-06-01
niles report neglect --cadence
niles report tasks --status open --due this-week
```

Reports support JSON envelopes by default and human/Markdown output with
`--human` or `--format markdown`.

## 10. Python library API (v1 sketch)

The CLI is a thin layer; the library is the real interface for notebooks.

```python
from niles import Project, Contact, ContactList

p = Project.open(".")                      # or Project.init(".")
c = Contact(name="Jane Doe", emails=["jane@acme.com"],
            traits={"timezone": "ET"}, tags=["prospect"])
p.contacts.add(c)
p.notes.add(c.id, "Intro call", kind="call")
p.tasks.add(c.id, "Send follow-up", due_date="2026-09-05", assignee="john")

stale = p.contacts.filter(stale=True)      # ContactList
sl = stale.to_scenario_list()              # EDSL ScenarioList

from niles.surveys import load_template
debrief = load_template("debrief")         # (Survey, RoutingMap)
p.run_survey(debrief, contact=c)           # interactive when in a TTY
```

`Contact`/`ContactList` implement `to_dict`/`from_dict`, rich `__repr__`
tables, and `ContactList.filter/select/from_csv`. Mutations go through
`Project` so every change is an event.

## 11. Non-goals for v1 (explicit)

- **Deals/pipeline.** Contacts + tags + traits can approximate it
  (`tag:lead`, `trait stage=demo`); a first-class Deal entity waits for v2
  and a decision on whether this is a sales tool at all.
- **LLM anything**: generated surveys, enrichment, summarization,
  contacts-as-agents. The routing vocabulary and job-packaging boundary are
  designed so these bolt on without rework.
- Email/calendar integration, vCard, multi-user, web dashboard, watch-mode
  intake, companies as first-class entities.

## 12. v2 direction (informative, not binding)

- `niles enrich` / `niles label` / `niles recommend`: export `.ep` artifacts
  over `to_scenario_list()` projections (bewley's open-coding pattern:
  export → external `ep run` → import → review → apply selected results via
  the same routing vocabulary).
- Context-aware generated follow-up questions in debrief/review, constrained
  to the closed routing vocabulary.
- `Contact.to_agent()` for message pretesting, with EDSL's own caveat about
  simulated responses surfaced in output.
- Deal entity, per the pipeline decision.

## 13. Open questions

1. **Envelope default for a human tool.** bewley defaults to JSON with
   `--human` opt-in. A CRM gets far more direct human use than a coding
   pipeline. Options: (a) follow bewley exactly; (b) auto-detect TTY and
   default to human rendering there, JSON when piped, with `--json` to
   force. Spec currently assumes (a) for ecosystem consistency, but (b) is
   likely the better product. Decide before implementation.
2. **Global vs per-project store.** bewley projects are per-directory, which
   suits corpora. A personal CRM is more naturally one global store
   (`~/.local/share/niles`, `NILES_HOME` override). Spec assumes per-directory
   `niles init` for convention; revisit if the "one network, many
   directories" friction bites.
3. **Pipeline vs network** (deferred from design discussion): the traits
   system means v1 doesn't force the choice, but the answer shapes v2.
4. **Humanize surface details** to verify during implementation: closing a
   form, result pagination/high-water-mark semantics, whether form
   description text can carry the consent line.

## 14. Milestones

- **M1 — core store.** init/status/fsck/history/undo, events + index,
  contact/note/task/search/import/export, staleness. Envelope contract,
  guide/next, tests.
- **M2 — team + reporting.** org context, teammates, assigned tasks,
  pipeline/activity/neglect/task reports.
- **M3 — surveys + routing.** Survey storage, templates, terminal runner,
  routing vocabulary + dry-run, `niles review` loop.
- **M4 — intake and status requests.** export/register/import/review/purge,
  dedup + merge, consent warning, status-request review gate, strict EP CLI
  network boundary.
- **M5 — polish.** Library API surface, `--human` rendering, docs in
  bewley's README shape (when-to-use / stretch cases / decision rule /
  worked examples / pitfalls), example dataset (`niles example fetch`).
