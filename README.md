# agent-brain

An Obsidian-compatible knowledge workspace for people who work with coding agents: ordinary Markdown documents, agent-facing skills, and explicit, tested contracts for persisting knowledge and finding it again in a fresh session.

## Current status

**Specification accepted; document validation, setup and single-file persist implemented.** `python3 bin/brain validate --root <brain>` scans a brain's Markdown documents and reports a single JSON object per the specification's C5, C5a and C15 decisions (ST-01). `python3 bin/brain setup --starter <checkout> --target <folder> [--git] [--module projects]` creates a new brain from a clean starter checkout (ST-04). `python3 bin/brain persist --root <brain>` creates, updates and attaches one file at a time per C6 (ST-02) — see [Persist](#persist) below. Everything else described in the specification below should still be assumed not to work today.

| Capability | State |
| --- | --- |
| Beta specification | Accepted |
| Repository topology decision | Accepted |
| Document validation | Implemented (`brain validate`; ST-01) |
| Setup | Implemented (`brain setup`; ST-04) — see [Setup](#setup) below |
| Single-file persist (create, update, attach) | Implemented (`brain persist`; ST-02) — see [Persist](#persist) below |
| Index bookkeeping, `recover`, discovery, read, audit, ingestion, projects module data/integration, Claude skills | Not started |
| Codex entry path | Not started; optional for the beta, claimed only once demonstrated |

## Persist

`brain persist --root <brain>` with a JSON request on stdin (or `--input <file>`) creates, updates or attaches one file (C6):

- `create`: writes a new document in the canonical frontmatter form (C4); refuses `exists` if the target is already present, with no overwriting fallback.
- `update`: applies a targeted, byte-preserving replacement of only the named properties' spans (C4) — comments, hand ordering and untouched properties (including zero-indented block sequences) are left exactly as they were; refuses `unsupported_frontmatter` byte-identical when a touched span holds a comment or the block uses anchors, aliases, explicit tags, multiple YAML documents or flow style; refuses `version_mismatch` when `expected_version` no longer matches the current file.
- `attach`: persists binary content (for example an image) alongside a document, with its hash recorded and matching.
- Every write is journalled as an intent before any bytes move, applied by hard-link (`create`/`attach`, so a create can never overwrite) or backup-and-replace (`update`), and left with a change record and, in a git-backed brain, one commit whose final trailer paragraph names the operation, run, `op_key` and each changed path with its hash (C7) — a brain commit never sweeps in an unrelated uncommitted change. `kill`/`fail` fault injection at the C6 fault points leaves the target at either its prior bytes or the intended bytes, never truncated, and a bookkeeping failure returns `written_incomplete` naming exactly the pending step.
- Persist mints the document's id itself on every `create`; refuses `open_intent` on a path with unresolved interrupted work, `review_required` on a non-reviewed write of `origin: unknown`, `unsupported_version` on a `kb` value other than 1, and `journal_missing` while the brain's operation journal is absent.
- Every document persist writes re-parses to its intended values under both the vendored YAML 1.1 parser and a YAML 1.2 core-schema reading (the one documented difference is an unquoted date, read as a date under 1.1 and as a string under 1.2; both are accepted).

**Honest limits, as shipped today:** the `target_unexpected` outcome and `recover` (interrupted-write recovery, and the `--restore-retained` path) are ST-03's, not this story's — `persist` never retries after an unexpected target, and reaching that state today leaves the brain requiring ST-03 to resolve it. Index bookkeeping (C8) is ST-06's; `written_incomplete`'s pending list never names `index` before then. `brain grant`, maintenance-grant validity and invalidation are out of scope; persist only records a create-time grant trailer for an agent-authored document. `discover`, `read`, `audit` and `ingest` do not exist yet, so nothing published by persist is yet findable except by reading the brain's files directly.

## Setup

`brain setup --starter <checkout> --target <folder> [--git] [--module projects]` creates a new brain (C3):

- Refuses a non-empty target, a target nested inside another repository's working tree, or a dirty/unresolvable starter checkout — nothing changes on refusal.
- Copies the starter's skill payload, unaltered, into both `.claude/skills/` and `.agents/skills/`; copies code (including the vendored library) and base types under `.brain/`; installs a requested `--module` by copying whatever manifest and type definitions the named starter checkout supplies for it (a starter can name any module, not only `projects`) — a requested module the starter doesn't have, or whose manifest names an invalid destination folder, refuses before anything is written.
- Writes `.brain/setup.json` naming the brain id, the resolved `starter_url` (the starter's first configured `origin` remote URL, or a `file:` URI when there is none — source-location metadata only, not an authenticity or reachability claim), the starter commit, the installed modules, and the hash of every copied file.
- With `--git`, initialises a private local repository and makes one checkpoint commit whose trailer carries `Brain-Op: setup` and a freshly generated `Brain-Run: <run_id>` — this commit exists even if the smoke check below then fails. Without `--git`, no repository is created; setup itself never pushes or adds a remote.
- Only then runs a smoke check: supported Python (3.11+) and git (2.30+) versions, real hard-link creation and collision refusal in a data zone and in `.brain/state/`, and validation of the freshly copied, still-empty brain through its own copied runtime. Any failure returns `setup_failed` naming the check (`python_version`, `git_version`, `hardlink_data`, `hardlink_state`, or `empty_brain_validate`) and leaves the brain unusable for `create` per C3 — except a starter carrying a broken vendored dependency, which is reported as the copied runtime's own `error`/`vendored_dependency` outcome (exit 1), never folded into `setup_failed`.

**Honest limits, as shipped today:** this starter repository ships **no production skills and no module content at all** — `skills/` and `modules/` are absent here, so a real setup run against this checkout produces empty `.claude/skills/`, `.agents/skills/` and `.brain/types/custom/` folders, and any `--module` request refuses with `module_not_found`. The operational skills (including a setup skill that wraps this command) and the real `projects` module content are separate, later stories; an empty production skill set here is not a bug in this command. `brain setup`'s own tests supply their own synthetic, committed skill and module fixtures to exercise the installation mechanics.

## What the beta is for

- Set up your own private **brain** — a separate folder, ideally its own private git repository — from a specific version of this starter.
- Save documents safely: no truncated files after a crash, no silent overwrites, a change record and an explanatory commit for every write.
- Find and read relevant knowledge from a **fresh agent session**, with source, status, freshness and support shown, including honest "nothing found" and "partial" results.
- Keep editing notes by hand in Obsidian; code detects those edits and corrects metadata only additively.

## Next outcomes

In order, each independently verifiable:

1. Document validation and the formatting rules.
2. Safe persistence with recovery, setup of a new brain, and bounded read.
3. Index, rebuild, discovery and provenance support.
4. Projects module and the development example (the starter used to capture and retrieve its own development decisions), out-of-band audit and correction, ingestion from `raw/`, and review and maintenance rules.
5. Claude skills at the brain root, the evaluation harness, and a beta acceptance run.

Planned work and its status are tracked in this repository's [issues](https://github.com/mlutton/agent-brain/issues); preparation is tracked in [#1](https://github.com/mlutton/agent-brain/issues/1).

## Documents

- [Beta specification](docs/specification/beta.md) — the shared contract: behaviour, operation and data contracts, verification seams and scenarios, boundaries.
- [ADR 0001: repository topology](docs/adr/0001-repository-topology.md) — why the starter and each brain are separate.

## Contributing

- GitHub issues and pull requests are the authority for work state; documents link to them rather than tracking status.
- Changes to behaviour update the specification (and its version) in the same pull request.
- Public content is freshly authored; examples are synthetic. Third-party code is added only as a pinned, vendored library with its licence, named in the specification.
- Run the full check suite with `python3 -B -m unittest discover -s tests -t .` from the repository root. `-B` stops the interpreter writing bytecode caches so a test run leaves the tree exactly as it found it.
- Tests exercise `bin/brain` only as a subprocess (`[sys.executable, "-B", "-S", "-E", "bin/brain", …]`), against fixture brains written as literal text into temporary directories; no test imports a product module or mocks the filesystem. A behaviour is proven by showing its test fail first — write the test, watch it fail (or mutate the code/fixture to observe the failure), then make it pass.
- Every check asserts on outcomes a user could observe: the JSON on stdout, the exit code, and the brain's files and hashes before and after — never on internal module structure.
- Error assertions compare the full error multiset by exact equality (`tests/support.py`'s `errors_multiset`/`assert_errors`, backed by `collections.Counter`), not a subset check — a spurious extra error must fail a test, not pass it silently.
- Document-set assertions compare the full set of `documents` paths a report returns, not a subset.

## License

[MIT](LICENSE)
