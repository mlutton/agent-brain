# agent-brain

An Obsidian-compatible knowledge workspace for people who work with coding agents: ordinary Markdown documents, agent-facing skills, and explicit, tested contracts for persisting knowledge and finding it again in a fresh session.

## Current status

**Specification accepted; document validation, setup, single-file persist, interrupted-write recovery and single-document read implemented.** `python3 bin/brain validate --root <brain>` scans a brain's Markdown documents and reports a single JSON object per the specification's C5, C5a and C15 decisions (ST-01). `python3 bin/brain setup --starter <checkout> --target <folder> [--git] [--module projects]` creates a new brain from a clean starter checkout (ST-04). `python3 bin/brain persist --root <brain>` creates, updates and attaches one file at a time per C6 (ST-02) — see [Persist](#persist) below. `python3 bin/brain recover --root <brain>` classifies every intent an interrupted persist left open and finishes the missing bookkeeping exactly once (ST-03) — see [Recover](#recover) below. `python3 bin/brain read --root <brain>` reads one document, retained original or attachment by id or path, within a byte budget, under the publication rule (ST-05) — see [Read](#read) below, which also lists what it does not do. Everything else described in the specification below should still be assumed not to work today.

| Capability | State |
| --- | --- |
| Beta specification | Accepted |
| Repository topology decision | Accepted |
| Document validation | Implemented (`brain validate`; ST-01) |
| Setup | Implemented (`brain setup`; ST-04) — see [Setup](#setup) below |
| Single-file persist (create, update, attach) | Implemented (`brain persist`; ST-02) — see [Persist](#persist) below |
| Interrupted-write recovery (`brain recover`, plain mode) | Implemented (ST-03) — see [Recover](#recover) below |
| Single-document read (`brain read`) | Implemented in part (ST-05) — see [Read](#read) below |
| Index bookkeeping, `recover --restore-retained` and `--start-journal`, discovery, audit, ingestion, projects module data/integration, Claude skills | Not started |
| Codex entry path | Not started; optional for the beta, claimed only once demonstrated |

## Persist

`brain persist --root <brain>` with a JSON request on stdin (or `--input <file>`) creates, updates or attaches one file (C6):

- `create`: writes a new document in the canonical frontmatter form (C4); refuses `exists` if the target is already present, with no overwriting fallback.
- `update`: applies a targeted, byte-preserving replacement of only the named properties' spans (C4) — comments, hand ordering and untouched properties (including zero-indented block sequences) are left exactly as they were; refuses `unsupported_frontmatter` byte-identical when a touched span holds a comment or the block uses anchors, aliases, explicit tags, multiple YAML documents or flow style; refuses `version_mismatch` when `expected_version` no longer matches the current file. `update` can only replace an existing property's span — it cannot add a property the document does not already carry, and refuses `unsupported_frontmatter` byte-identical for one; appending a new property is out of this story's scope.
- `attach`: persists binary content (for example an image) alongside a document, with its hash recorded and matching; confined to the same durable data zones or installed module folders a document type may declare, so it cannot write into `.brain/` (including the custom type definitions that drive validation).
- Every write is journalled as an intent before any bytes move, applied by hard-link (`create`/`attach`, so a create can never overwrite) or backup-and-replace (`update`), and left with a change record and, in a git-backed brain, one commit whose final trailer paragraph names the operation, run, `op_key` and each changed path with its hash (C7) — a brain commit never sweeps in an unrelated uncommitted change. `kill`/`fail` fault injection at the C6 fault points leaves the target at either its prior bytes or the intended bytes, never truncated, and a bookkeeping failure returns `written_incomplete` naming exactly the pending step.
- Persist mints the document's id itself on every `create`; refuses `open_intent` on a path with unresolved interrupted work, `review_required` on a non-reviewed write of `origin: unknown`, `unsupported_version` on a `kb` value other than 1, and `journal_missing` while the brain's operation journal is absent.
- Every document persist writes re-parses to its intended values under both the vendored YAML 1.1 parser and a YAML 1.2 core-schema reading (the one documented difference is an unquoted date, read as a date under 1.1 and as a string under 1.2; both are accepted).

**Honest limits, as shipped today:** `persist` never retries after an unexpected target, and its own `target_unexpected` outcome cannot be reached through the command line today ([#37](https://github.com/mlutton/agent-brain/issues/37)); the state it stands for is observed from `recover`, below. Index bookkeeping (C8) is ST-06's; `written_incomplete`'s pending list never names `index` before then. `brain grant`, maintenance-grant validity and invalidation are out of scope; persist only records a create-time grant trailer for an agent-authored document. `discover`, `audit` and `ingest` do not exist yet, so nothing published by persist is findable except through `read` by id or path, or by reading the brain's files directly.

## Recover

`brain recover --root <brain>` takes no request body. It reads every open intent in the journal, compares each one's target with the hashes the intent recorded, and prints one JSON object (C6; the report shape is under "Recover report" in the specification):

- `not_applied` (the target still matches its prior state, and does not hold the intended bytes): the intent is closed and its temp file removed. `kill` at `after_intent` and `before_apply` lands here.
- `applied` (the target holds the intended bytes; this is tested first, so an update that changed no bytes counts as applied): the missing bookkeeping is finished exactly once. A change record is written only if none exists for the `op_key`, and a commit is made only if no commit in `HEAD`'s history already carries that `op_key`, so running recovery again, or after a `written_incomplete`, never adds a second of either. `kill` at `after_apply` and `fail` at `before_change_record` or `before_commit` land here. If a step still cannot be finished, the intent stays open and the report's outcome is `recovery_incomplete`; on that intent the signal is an `applied` entry with a non-empty `pending`.
- `target_unexpected` (the target matches neither): reported with the observed hash and the backup path (`null` for `create` and `attach`, which never take a backup). **Recovery does not resolve this state.** It cannot tell what happened, so it overwrites nothing, closes nothing, and keeps reporting the intent on every run; the path stays locked against `persist` (`open_intent`) until a person deals with the file and the intent.
- `target_unexpected` also reports an intent recovery cannot act on, with a `reason` and a null `observed_sha256`: `invalid_intent` (unreadable, incomplete, or naming a temp file that is not a persist temp file inside the brain), `not_a_regular_file` (the target is a link, a folder or anything else that is not a regular file inside the brain; recovery hashes only a regular file, so it never commits a hash for bytes it did not read from one) or `recovery_failed` (an unexpected error, such as an unreadable target, stopped that one intent). One intent's failure is reported in its own entry; every other intent is still recovered and reported. The intent stays open.
- `orphan_temps` lists temp files no intent owns. They are reported and left in place, and they never change the outcome.

The aggregate `outcome` is `target_unexpected` (exit 3) over `recovery_incomplete` (exit 3) over `recovered` (exit 0); no open intent is `recovered`. `recovery_incomplete` is an outcome only, never an intent's classification.

**Honest limits, as shipped today:** `run_pending` is defined but never reported, because it needs a run manifest and nothing creates one before ingestion. `recover --restore-retained` and `--start-journal` are refused `not_implemented`. A recovered commit carries no `Brain-Grant` trailer, so an interrupted agent-authored `create` needs `brain grant` to get one. Index bookkeeping does not exist yet, so recovery finishes only the change record and the commit. A temp file left beside an already-applied target is not removed, and is reported as an orphan once its intent closes. Git refuses a commit that changes no bytes, so an applied update that changed none has its change record written but its commit stays `pending`: the outcome is `recovery_incomplete` and the intent stays open. Recovery assumes one writer per brain (C6).

## Read

`brain read --root <brain>` with a JSON request on stdin (or `--input <file>`): `{id | path, max_bytes, include_unverified?}` (C10). It prints one JSON object and exits `0` for every outcome below; the only refusal is a request that carries a `heading` key (below), exit `2`.

- **Finding the file.** By `id` (the first path in code-point order whose frontmatter carries it, among the Markdown files under `documents/`, `wiki/` and installed module folders; a retained original or attachment is never found this way) or by `path` (a plain relative path into those same folders). Anything else — `..`, a hidden name, a link, `.brain/`, a file beside the brain — is `not_found`.
- **Publication (C8).** Read applies rule 1 (a file named by an open intent is `unpublished`: no content, whatever `include_unverified` says, decided before the file is parsed), rule 2 (a path with a publication record keeps its role whatever its bytes), rule 5 (a `type: document` file with no ingest or adopt record is `unverified` with `reason: no_ingest_record`), rule 6 (a file named by such a document through `original` or `attachments` is `unverified` with `hint: suspected_ingestion`) and rule 7 (committed is published; uncommitted with the journal present is `ok` with `labels: {"changed_out_of_band": true}`; uncommitted with the journal gone is `unverified` with `reason: journal_missing`). `unverified` withholds content unless `include_unverified` is `true`. If git cannot be consulted, or the root is not a git repository, the answer is `unverified` with `reason: git_unavailable`, never `ok`.
- **A publication record** is the latest ingest or adopt commit whose trailer is consistent (C7: every changed data-zone path declared, every declared path's committed content matching its declared hash, every declared path the commit leaves unchanged declaring `prior` equal to that hash) and that declares the path `role=original` or `role=attachment` alongside a `role=document` path that references it. Nothing this repository ships writes one yet, so today they exist only when written by hand; `recover --restore-retained` and `--start-journal` are still refused.
- **`ok` for a note or document:** `id`, `path`, `version` (SHA-256 of the file's bytes), `role` (`note`, or `document` when a record says so; never taken from the frontmatter `type`), `frontmatter` (dates as ISO text), `excerpt` (the body, not the file, at most `max_bytes` bytes and never a split character) with `truncated`, `origin` (a list of `{ref, retrieved}`, `authored` or `unknown`; `unknown` for a note that records none), `evidence` (`[{target, exists}]`: `true`/`false` for a brain id, and for an external URL the target exactly as written with `exists: null`, never fetched) and `labels`.
- **`ok` for a retained file:** `role` (`original` or `attachment`), `owner` (the path of the document that references it), `integrity` (`retained`, `retained_changed` or `retained_missing`), `recorded_sha256`, `publication_commit`, `version`, and `excerpt`/`truncated` over the whole file, which is never parsed (no `frontmatter`, whatever it holds). Bytes that are not UTF-8 give `excerpt: null` and `truncated: null`, decided on the whole file so `max_bytes` cannot change it.
- **Other outcomes:** `not_found`; `invalid` (C4's malformed class only: not UTF-8, unclosed block, invalid YAML, a non-mapping, duplicate keys, a non-integer `kb`; C5's field rules are not applied); `unsupported_version` (`kb` another integer).

**Honest limits, as shipped today.** Not delivered: `support` (its key is absent); `heading` (a request carrying the key at all, with any value, is refused `unsupported_heading`); every `labels` key but `changed_out_of_band`; the content fields of a `retained_missing` path (only the role, owner, integrity, recorded hash and publication commit are reported); C8 rules 3, 4 and 8 and open run manifests (nothing creates them yet); any `unverified` reason or hint beyond the four named above; request validation (a request without an integer `max_bytes` fails as an unhandled error, exit `1`); outcome precedence beyond rule 1 before parsing; whether `invalid` carries an errors list; and any test that read changes nothing on disk. The key names `integrity`, `recorded_sha256`, `publication_commit`, `owner` (as a path), `reason`, `hint` and `labels` (as an object) are this implementation's choice: the specification lists no key for them. `read` and `validate` disagree about retained paths today: `validate`'s `retained` count is always `0`, while `read` reports a path with a record as `role: original` or `attachment`.

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
- Tests that interrupt a write do it only through the specification's `BRAIN_TEST_FAULTS` / `BRAIN_FAULT` variables on a real `persist` run, show that the injection landed (the process died by SIGKILL, or reported `written_incomplete`, and the disk holds what that leaves) before claiming anything about recovery, and register their fixture's removal before building it so a failing or killed run leaves nothing behind.
- Every check asserts on outcomes a user could observe: the JSON on stdout, the exit code, and the brain's files and hashes before and after — never on internal module structure.
- Error assertions compare the full error multiset by exact equality (`tests/support.py`'s `errors_multiset`/`assert_errors`, backed by `collections.Counter`), not a subset check — a spurious extra error must fail a test, not pass it silently.
- Document-set assertions compare the full set of `documents` paths a report returns, not a subset.

## License

[MIT](LICENSE)
