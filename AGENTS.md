# Agent instructions

## Operating Niles

- Begin with `niles agent next` and follow its JSON envelope.
- Read and mutate CRM data only through `niles` commands. Internal storage is
  managed by Niles and is not an agent API.
- Use `niles sync` for CRM commits and pushes. Do not stage internal paths.
- Treat imported human responses and recommendations as quarantined until an
  explicit `review`, `accept`, `merge`, or `reject` command.

## Expected Parrot boundary

- Niles makes no EP network calls and reads no EP credentials.
- For intake and status requests, use Niles `export`, run the returned
  `publish_command`, use Niles `register`, run the returned `pull_command`, and
  use Niles `import` followed by `review`.
- For recommendations, use Niles `recommend export`, run the returned
  `run_command`, and use `recommend import` followed by `recommend review`.
- Let Niles manage handoff paths. Explicit path arguments are advanced
  overrides for artifacts obtained elsewhere.

## Developing Niles

- Preserve the versioned JSON envelope and append-only event model.
- Add regression tests for command or envelope changes.
- Run `python -m pytest -q` and `make coverage`; branch coverage must remain at
  or above 85%.
