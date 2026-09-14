# agent-brain beta specification

Version: f5237ea5540b63a27feec27b23da968bbe75f7d31fb8b21b0c61b29b7304daab
Publication: published — draft pull request for review; proposed, not accepted
Status: **Proposed.** The beta is being specified, not implemented. Nothing described here exists yet.

This document is the shared contract for the agent-brain beta. Delivery stories reference it by the version above (a SHA-256 over this document from the first `##` heading to the end) and by part — for example "Behaviour 12–15" or "Decision C5" — rather than restating it. Repository topology is recorded separately in [ADR 0001](../adr/0001-repository-topology.md).

Words used with a specific meaning — brain, starter, zone, module, document type, origin, out-of-band change, discovery, context manifest — are defined in [Terms](#terms) at the end.

## Problem Statement

People who work with coding agents produce useful knowledge in every session: research, decisions, project context. That knowledge is hard to keep and harder to find again.

- Saved notes are either invisible to the next session or loaded wholesale, so a fresh session cannot pick out what is relevant.
- When something is found, nothing says where it came from, whether it is still current, whether it was finished, or whether a newer version exists.
- An interrupted write can leave a truncated file that looks like a real document.
- Edits made by hand in a note editor are invisible to automation, which may then overwrite them or trust stale summaries.
- A file existing on disk is not the same as knowledge that can be discovered and used as context.

Users also want to keep their knowledge in ordinary Markdown that stays useful in Obsidian or any editor without an agent, and to keep it private.

## Solution

A public starter repository, `agent-brain`, from which a user creates their own separate, private **brain**: a folder of ordinary Markdown documents with a small frontmatter contract, plus agent skills and supporting code copied into hidden folders.

- **Skills are the entry point.** A Claude session opened at the brain root uses skills to find knowledge, read selected evidence, save results and ingest new material. Claude is the required entry path for the beta; Codex support is optional and claimed only once demonstrated.
- **Code does the mechanics.** Skills call a command-line interface that takes an explicit brain root and returns JSON. The code validates documents, persists them safely, maintains an index, discovers candidates, reads bounded excerpts, audits out-of-band changes and composes ingestion runs. The model proposes; the code validates and applies.
- **Everything stays inspectable.** Documents remain readable Markdown. Each operation leaves a change record and, in a git-backed brain, a commit whose trailer declares exactly what changed. Generated state (index, intents, backups) is rebuildable and never committed.
- **Honest outcomes.** Discovery distinguishes a real match, an honest no-match and a partial result where coverage is incomplete. Persistence distinguishes refused, not applied, written, written-with-pending-bookkeeping and unexpected target content. Nothing incomplete is presented as complete.
- **Human edits are respected.** Out-of-band edits are detected by code, corrected only additively, and suspend automatic agent maintenance of that document until a person explicitly restores it.

## Behaviour

Actors: **user** (a person owning a brain), **agent** (an agent session at the brain root using skills), **operator** (whoever runs code directly, including tests), **contributor** (someone developing agent-brain itself), **evaluator** (someone measuring the beta).

### Setup and layout

1. As a user, I want to create a new brain from a specific starter commit, so that my brain's system files are known and reproducible.
2. As a user, I want setup to write a setup record naming the starter URL, commit, and every copied file with its hash, so that I can later tell which system files are unchanged.
3. As a user, I want setup to offer to initialise a private git repository that tracks my data and copied system files and ignores only generated state, so that my history is kept and nothing generated is committed.
4. As a user, I want setup to run a smoke check (supported Python and git versions, hard-link support in data and state folders, a valid empty brain), so that unsupported environments fail at setup rather than during a write.
5. As a user, I want the brain to contain `inbox/`, `raw/`, `documents/`, `wiki/` and installed module folders, with skills in `.claude/skills` and `.agents/skills` and code, type definitions and state under `.brain/`, so that knowledge and machinery stay separate and the editor shows only knowledge.
6. As a user, I want setup to refuse a target folder that is not empty or is inside another git repository's working tree, so that a brain is never nested in, or mixed with, another repository.

### Documents and validation

7. As a user, I want every managed document to carry the `kb` marker and the base fields, so that any document can be validated and discovered consistently.
8. As an operator, I want validation to fail loudly, naming every invalid field of every invalid document, so that a problem is never silently skipped.
9. As a user, I want uncertainty recorded field by field (`created: unknown`, `reviewed: never`, `kind: unknown`, `type: unclassified`, empty summary, with freshness unknown when `fresh_until` is absent) independently of origin, so that a document with a known origin can still say what is unknown.
10. As a user, I want `needs_review` to list exactly the fields that hold uncertainty values, so that open questions about a document are visible in one place.
11. As a user, I want `origin` to be one or more references with retrieval dates, or `authored`, or `unknown`, where only audit correction or an explicit human statement may set `unknown`, so that where content came from is always recorded honestly.
12. As a user, I want type definitions to be data files read from the brain root, so that custom document types validate without code changes.
13. As an operator, I want a malformed file (unparseable frontmatter, outside the supported frontmatter subset) reported as `invalid` and never modified, so that damaged or unusual notes are surfaced, not rewritten.

### Persistence

14. As an agent, I want to create a document that fails if the path already exists, with no overwriting fallback, so that a create can never destroy existing content.
15. As an agent, I want to update a document only when its current version matches the version I read, so that edits made since I read it are never overwritten.
16. As an agent, I want to persist an attachment (image or other binary) alongside its document with its hash recorded, so that attachments are stored intact.
17. As an agent, I want every persist to return exactly one outcome — `refused`, `failed_before_apply`, `written`, `written_incomplete` or `target_unexpected` — with the observed hashes, so that I know precisely what happened to the target.
18. As a user, I want each successful persist to leave a change record and, in a git brain, one commit whose trailer declares the operation, run and each path with its hash, so that the history explains itself.
19. As a user, I want an interrupted persist to leave the target either at its prior state or at the intended content, never truncated, so that a crash cannot corrupt a document.
20. As an operator, I want a recovery command that classifies every open intent as `not_applied`, `applied` or `target_unexpected`, finishes bookkeeping for applied writes exactly once, and reports orphan temp files, so that interrupted work is resolved without duplicate records or commits.
21. As an operator, I want persist to refuse any write to a path with an open intent, so that only recovery touches interrupted work.

### Index, discovery and read

22. As an agent, I want the index updated by every brain operation and rebuildable from files at any time, so that discovery is fast and never the only copy of anything.
23. As an agent, I want discovery to return candidates ranked on metadata and summary, each with id, path, type, module, kind, status, freshness, authorship, origin, evidence count, conflicts, version and labels, so that I can choose what to read without opening files.
24. As an agent, I want discovery to return `no_match` only when coverage is complete, and `partial` whenever coverage is incomplete, with the reasons counted, so that "nothing found" is never a guess.
25. As an agent, I want a missing index to produce `partial` with `index: missing`, never `no_match`, so that a lost index cannot masquerade as an empty brain.
26. As an agent, I want undescribed notes returned separately as weaker body-text matches, so that unsummarised human notes are still findable without being mistaken for described documents.
27. As an agent, I want each candidate labelled `fresh`, `stale` or `unknown` against a given `as_of` date, with status, `superseded_by` and `newer_version_available` shown, so that outdated or superseded material is never presented as current.
28. As an agent, I want discovery to run a quick out-of-band check first and label affected candidates `changed_out_of_band`, without backfilling, so that I know when metadata may not match the file.
29. As an agent, I want to read a document by id or path with a byte budget and receive a bounded excerpt, its version, origin, and each evidence reference with whether it resolves, so that I load only what I need and can trace it.
30. As an agent, I want files belonging to an unfinished operation or ingestion run to be invisible as candidates and unreadable as published content, consistently in discover, read and rebuild, so that a half-finished run is never used as knowledge.
31. As an agent, I want a truncated result to say how many candidates were omitted, so that I can narrow or page instead of assuming completeness.

### Provenance and support

32. As an agent, I want every source document to carry a source identity (normalised URL, DOI, brain id or declared identifier), so that versions of the same source are recognised as such.
33. As an agent, I want each distinct content version of a source kept as a separately citable document, so that a citation always refers to the content that was actually read.
34. As an agent, I want discover and read to return a `support` summary that follows `evidence` and `derived_from` to a depth limit and reports resolution (`resolved`, `partial`, `unresolved`, `unknown`, `cyclic`), broken references, cycles, unknown nodes and unretained external sources, so that I can see whether a claim's chain reaches a source.
35. As an agent, I want synthesis that cites only synthesis to report unresolved support, so that generated text is never treated as independent evidence.
36. As an agent, I want independence reported as `not_assessed` unless a judgment is recorded, with distinct identities, declared derivations and a same-content-across-identities flag shown, so that differing hashes or URLs never imply independent corroboration.
37. As an agent, I want claim-level support reported separately as `not_checked` unless a check is recorded, so that provenance is not mistaken for verification of a specific claim.

### Modules and custom types

38. As a user, I want the projects module installed by setup, bringing its folder, its document types and its discovery display fields, so that project context is a first-class part of my brain.
39. As a user, I want to add a new custom document type by adding a definition file to my brain, and have documents of that type validate and appear in discovery with their display fields exactly like base documents, so that I can extend my brain without changing code.
40. As an agent, I want discovery to filter by module and type, so that I can scope a question to one project.

### Audit and correction

41. As a user, I want an audit command that lists data-zone changes made outside brain operations since the last audit position — commits without a valid trailer, and uncommitted changes — so that out-of-band edits are found by code, not by guesswork.
42. As a user, I want audit to classify trailer commits as `record_backed`, `consistent_unrecorded`, `inconsistent_trailer` or `unparseable_trailer`, and commits without a trailer as `out_of_band`, so that I know how much each change can be trusted.
43. As a user, I want audit to state that a trailer proves internal consistency, not authorship, so that a forged but consistent trailer is not reported as verified.
44. As a user, I want audit after a fresh clone or loss of generated state to report consistent trailer commits as unverifiable, not verified, while still flagging inconsistent, unparseable and out-of-band commits, so that lost local records reduce confidence rather than invent it.
45. As a user, I want correction to add the `kb` marker and deterministic fields to new unmanaged notes, re-index edited managed documents, follow renames and mark removals, only ever appending and never altering existing bytes, so that my notes join the brain without being rewritten.
46. As a user, I want correction to refuse a file whose existing field holds an invalid value (`correction_blocked`) and never touch a malformed file, so that correction cannot damage what it does not understand.
47. As a user, I want audit and correction to make no model call, so that they are deterministic and repeatable.

### Ingestion

48. As an agent, I want to ingest one item from `raw/` by proposing its document (type, destination, metadata, source identity, unresolved fields) for code to validate and apply, so that model judgment and mechanical writing stay separate.
49. As a user, I want an ingestion run to write the kept original and attachments first, the processed document last, and publish everything in one commit, so that the run is all-or-nothing for discovery.
50. As a user, I want the `raw/` item deleted only after the run commit, only if its content still matches what was proposed, and in its own commit, so that a changed or unprocessed raw item is never lost.
51. As an agent, I want re-ingesting identical content from the same source to return `duplicate` without writing anything — a new retrieval date alone never creates a new version — so that repeated retrieval does not clutter the brain.
52. As an agent, I want re-ingesting different content from a known source to create a new, separately citable version document with a supersession assessment proposed for review but not applied, so that changed sources are preserved and reviewed.
53. As an operator, I want a rerun with the same run id to skip files already at their intended content, commit once, and repeat the raw-deletion check, so that retries never create a second document.

### Review and maintenance permission

54. As a user, I want changes to human-authored or mixed documents, proposals with unresolved fields, and overwrites to stop for my review, so that I stay in control of what matters.
55. As a user, I want automatic agent updates allowed only under a valid maintenance grant on an agent-attributed current version with a matching expected version and nothing unresolved, so that agents maintain only their own untouched work.
56. As a user, I want any out-of-band content change to end a document's maintenance grant, derived from history rather than rewritten into the note, so that my edit immediately stops automatic maintenance.
57. As a user, I want approving one reviewed update to permit that update only, and a new grant to require my explicit action recorded as its own commit, so that one approval is never ongoing permission.
58. As a user, I want replacing an original or attachment, and deleting, renaming or moving a document, always treated as overwrites, so that destructive changes always reach me.

### Entry path, dogfooding and evaluation

59. As a user, I want Claude skills at the brain root to find knowledge, read selected evidence, record a reuse decision, save results and ingest `raw/` items through the operations, so that a fresh Claude session can use my brain without the earlier conversation.
60. As a user, I want the skills to run audit's report mode before they persist, and to tell me when out-of-band changes need audit, so that agent writes start from an audited view.
61. As a contributor, I want a synthetic development-project example that sets up a brain from the current commit, records a project decision with its evidence, and retrieves it from a fresh process and a fresh Claude session, so that the starter demonstrates its use for developing software as early as its core exists.
62. As an evaluator, I want a harness that builds a synthetic brain at a pinned commit, copies it per run, strips skills, code and index for a baseline arm, isolates each run's configuration and captures each run's transcript and usage, so that brain-assisted and baseline sessions can be compared honestly.
63. As an evaluator, I want an acceptance run and evaluation report covering every scenario in Verification Decisions on Claude, reporting per phase and cumulatively with no target and unknowns kept unknown, so that the beta is accepted on evidence.

## Implementation Decisions

Each decision is numbered (C1…C17, with C5a) so stories can reference it. Field names, outcome names and command names below are part of the contract.

### C1. Runtime and packaging

- Supporting code is Python 3.11+ using only the standard library, plus the `git` command (2.30+) for git-backed brains. No package installation, daemon, network service, MCP server or Obsidian dependency.
- One command-line entry point, `brain`, with subcommands `setup`, `validate`, `persist`, `recover`, `rebuild`, `discover`, `read`, `audit`, `ingest`, `grant`. It is invoked as `python3 bin/brain` from the starter repository root (development and tests) and as `python3 .brain/bin/brain` inside a brain; nothing is installed.
- Every subcommand except `setup` requires `--root <brain>`; the root is never inferred from the working directory. Structured input is a JSON document on stdin or `--input <file>`. Output is exactly one JSON object on stdout carrying `outcome`.
- Exit codes: `0` a defined non-refusal outcome (including `no_match`, `partial`, `duplicate`); `2` `refused` or invalid request — nothing changed; `3` an outcome needing attention (`failed_before_apply`, `written_incomplete`, `target_unexpected`, `not_published`, and `invalid` from `validate`); `1` an unhandled error. The JSON `outcome` is authoritative; exit codes are a convenience.

### C2. Brain layout and folder categories

```
<brain>/
  inbox/  raw/                 temporary user data
  documents/  wiki/            durable user data
  projects/                    module folder (projects module)
  .claude/skills/  .agents/skills/   copied skills (system)
  .brain/                      copied code, base and module types, setup record (system)
  .brain/types/custom/         user-added type definitions (user data)
  .brain/state/                generated: index, intents, change records, backups, temps (ignored)
```

| Category | Starter repository | Brain |
| --- | --- | --- |
| System shipped | tracked source | copied; listed with hashes in the setup record; tracked |
| User data | absent (synthetic examples only, never a real brain) | tracked in the brain's private git |
| Generated state | never present | always git-ignored |

`wiki/` is a persist target for synthesis documents. No beta operation compiles or regenerates `wiki/` content.

### C3. Setup

`brain setup --starter <checkout> --target <folder> [--git] [--module projects]`:

1. Refuse unless the starter checkout is clean and its `HEAD` commit resolves; refuse a non-empty target or a target inside another repository's working tree.
2. Create the layout; copy skills to both skill folders, code, base types and selected modules under `.brain/`.
3. Write `.brain/setup.json`: `starter_url`, `commit`, `created`, `modules`, and `copied: [{path, sha256}]`.
4. Write `.gitignore` containing the generated-state folder. With `--git`, initialise a repository and make the first commit with trailer `Brain-Op: setup`.
5. Smoke check: Python and git versions; create and refuse a hard link inside a data zone and inside state; validate the empty brain. Any failure → outcome `setup_failed` naming the check; with a failed hard-link check the brain must not be used for `create`.

The setup skill wraps this command and explains the private-repository recommendation. Contribute-back and update skills are post-beta; the setup record makes them possible later.

### C4. Frontmatter subset and document versions

- A **managed document** is a UTF-8 Markdown file whose first line is `---`, followed by a frontmatter block closed by a `---` line, containing `kb: 1`.
- A Markdown file with no frontmatter block, or with a block that parses within the subset but has no `kb` key, is **unmanaged**. It is not an error. Audit brings it under management (C12).
- A file whose first line is `---` but whose block does not parse within the subset is **malformed** and reported `invalid`, whether or not a `kb` line is readable.
- **Supported frontmatter subset:**
  - Encoding and lines: UTF-8 without a byte-order mark. Lines end in LF or CRLF. Blank lines inside the block are allowed. Comment lines, duplicate keys and tab indentation make the file malformed.
  - Each top-level line is `key: value` or `key:`. A key is lowercase letters, digits and `_`.
  - Scalars: a **plain scalar** is everything after `: ` to the end of the line, with trailing spaces stripped; `#` and `: ` inside it are literal. A **quoted scalar** is enclosed in double quotes, with `\"` and `\\` as its only escapes. `key:` with nothing after it, and `key: ""`, are both the empty string.
  - Lists: `key: []` is the only inline form and means an empty list. A **block list** is `key:` followed by items indented two spaces as `  - <scalar>`.
  - `origin` alone may hold a block list of mappings. Each item is `  - ref: <scalar>` followed by `    retrieved: <scalar>` on the next line, in that order.
  - Anything else — nested mappings, inline lists with items, mappings under other keys — is malformed.
- Code never re-serialises frontmatter; it appends lines.
- Dates are `YYYY-MM-DD`. `kb` must be the plain scalar `1`.
- A document's **version** is the SHA-256 of its full file bytes.
- A document **id** is minted once as a lowercase 26-character ULID, never reused, never derived from the path, and survives renames.

### C5. Field contract (`kb: 1`)

Required: `kb`, `id`, `type`, `title`, `summary`, `status`, `created`, `reviewed`, `origin`, `evidence`, `kind`, `authored_by`, `retention`.

Optional: `fresh_until`, `first_seen`, `needs_review`, `supersedes`, `superseded_by`, `previous_version`, `derived_from`, `conflicts_with`, `independence_assessment`, `claim_checks`, `tags`, plus type-specific fields.

| Field | Values |
| --- | --- |
| `status` | `draft`, `incomplete`, `complete`, `superseded` |
| `kind` | `source`, `synthesis`, `query-output`, `decision`, `project-context`, `unknown` |
| `authored_by` | `human`, `agent`, `mixed` |
| `retention` | `ephemeral`, `durable` |
| `created` | ISO date or `unknown` |
| `reviewed` | ISO date or `never` |
| `summary` | at most 300 characters; empty means undescribed |
| `origin` | list of `{ref, retrieved}` (`retrieved` an ISO date or `unknown`), or `authored`, or `unknown` |
| `evidence`, `derived_from`, `conflicts_with`, `supersedes` | lists of brain ids or external URLs |

Rules:
- **Uncertainty values** are valid for any origin: `created: unknown`, `reviewed: never`, `kind: unknown`, `type: unclassified`, empty `summary`, `origin: unknown`, any `retrieved: unknown` (named as field `origin`), and any value a type definition declares under `uncertainty_values`.
- `needs_review` must list exactly the fields holding uncertainty values, in any order. It is absent or `[]` when there are none.
- An absent `fresh_until` means freshness is unknown, but it is not an uncertainty value for `needs_review`.
- A `kind: unknown` document never counts as a source.
- `evidence`, `derived_from`, `conflicts_with` and `supersedes` entries are each an id (C4 shape) or an `http`/`https` URL.
- **Who may write `origin: unknown`.** Validation accepts the value; the writing operations enforce who may write it. Audit correction may write it. `persist` refuses it unless the request is a reviewed write (C14).

Base types: `note` (general authored note; any durable data zone or module folder), `document` (processed item; `documents/` only), `wiki-page` (synthesis; `wiki/` only), `unclassified` (any durable data zone or module folder). The `document` type additionally requires `source_identity`, `original` (relative path of the kept original) and `original_sha256`.

Type definitions:
- **File shape.** Each definition is a JSON file named `<type>.json` with keys `type`, `zones` (list of zone folder names, or `["*"]` for any durable data zone or module folder), `required` (additional field names), `allowed_values` (object mapping field to list of allowed strings), `uncertainty_values` (object mapping field to one value, optional), `display_fields` and `search_fields` (lists).
- **Locations.** Base definitions ship with the code, in the starter's `types/base/` and in a brain's `.brain/types/base/`, and are always read from beside the running code. Module definitions are in `.brain/modules/<module>/types/`. Custom definitions are in `<root>/.brain/types/custom/`.
- **Enforcement.** Zones are enforced. A document whose `type` has no definition is invalid (`unknown_type`). A custom definition that reuses a base or module type name, has a malformed file, or has a file name that does not match its `type` makes validation's outcome `invalid`, with a `definitions` error.
- Identity keys and relation types are post-beta.

### C5a. Validate report

`brain validate --root <brain>` scans every `.md` file under `documents/`, `wiki/` and installed module folders. It skips `inbox/`, `raw/`, hidden folders and non-Markdown files. Output:

```json
{
  "outcome": "valid | invalid",
  "counts": {"valid": 0, "invalid": 0, "unmanaged": 0},
  "definitions": [{"path": "…", "code": "…", "message": "…"}],
  "documents": [
    {"path": "documents/x/x.md", "status": "valid | invalid | unmanaged",
     "id": "… or null", "type": "… or null", "version": "sha256",
     "errors": [{"field": "summary | null", "code": "missing | not_allowed | too_long | bad_format | needs_review_mismatch | unknown_type | wrong_zone | malformed", "message": "…"}]}
  ]
}
```

- `documents` lists every scanned file, sorted by path (POSIX, relative to the root).
- An error's `field` is null only for `malformed`.
- A document reports every error it has, not just the first.
- `outcome` is `invalid` when any document is invalid or any definition error exists. An empty brain is `valid`.
- Exit code: `0` for `valid`, `3` for `invalid`, `2` for a refused request (for example a missing or nonexistent `--root`).
- Validation never writes to the brain.

### C6. Single-file persist

Request: `{operation: create|update|attach, path, frontmatter?, body?, content_base64?, for_document?, expected_version?, run_id?}`. Sequence:

1. Validate the request and the resulting document against C5 and its type.
2. Record an intent under `.brain/state/intents/`: `run_id`, `op_key`, `path`, `expected_prior` (`absent` or a hash), `intended_sha256`, temp path, backup path.
3. Write a temp file in the target's directory, `fsync`, and verify its hash.
4. Re-check the target against `expected_prior`.
5. Apply. `create` and `attach`: hard-link the temp file to the target; the link fails if the target exists, and there is no fallback. `update`: copy the current target to a backup in state, then atomically replace.
6. Hash the target after applying.
7. Bookkeeping: write the change record, update the index, commit (git brains).

`op_key` = SHA-256 of `run_id`, `path` and `intended_sha256`. At most one change record and one commit exist per `op_key`.

| Outcome | Meaning |
| --- | --- |
| `refused` | Target untouched; observed hash reported; reason named (`exists`, `version_mismatch`, `invalid`, `open_intent`, `review_required`). |
| `failed_before_apply` | This operation did not apply; any temp file reported; includes `unsupported_filesystem`. |
| `written` | Target hash equals intended hash and all bookkeeping finished. |
| `written_incomplete` | Target hash equals intended hash; pending steps named (`change_record`, `index`, `commit`). |
| `target_unexpected` | Target hash differs from intended hash; observed hash and backup path reported; no retry; audit treats the file as changed out of band. |

Stated limitation: the beta assumes one writer per brain. A write by another process between step 4 and step 5 of an `update` can be overwritten; the backup preserves displaced content only if that write landed before the backup.

`recover`: for each open intent — target matches `expected_prior` → `not_applied` (close intent, remove temp); target matches `intended_sha256` → `applied` (finish missing bookkeeping exactly once, checking for an existing change record and an existing commit carrying the same `op_key`); otherwise → `target_unexpected`. A temp file with no intent → `orphan_temp`.

Fault injection for tests: when `BRAIN_TEST_FAULTS=1`, the environment variable `BRAIN_FAULT=<step>:<kill|fail>` stops the process (SIGKILL) or raises a failure at a named step (`after_intent`, `after_temp`, `before_apply`, `after_apply`, `before_change_record`, `before_index`, `before_commit`, `after_run_commit`, `before_raw_delete`). Without `BRAIN_TEST_FAULTS=1` the variable is ignored.

### C7. Commit trailers

Every brain commit message ends with a single final paragraph of trailers (git ignores trailer lines placed in any earlier paragraph):

```
Brain-Op: create | update | attach | ingest | delete-raw | audit | grant | setup
Brain-Run: <run_id>
Brain-Key: <op_key>                      (one per applied write)
Brain-Path: <path> sha256=<hash> prior=<absent|hash|deleted>   (one per path)
Brain-Grant: <id> maintenance=agent by=<agent|human>            (create and grant only)
Brain-Review: approved by=human                                  (reviewed writes only)
```

A trailer is **consistent** when the commit changes exactly the declared data-zone paths and each declared hash matches the committed content. Brain commits stage and commit only their declared paths, never other working-tree changes.

### C8. Index and publication visibility

- The index lives in generated state and records, per managed document: id, path, version, size, modification time, type, module, display and search fields, summary, labels inputs, evidence and derivation lists. `rebuild` recreates it from files and reports invalid, unpublished and unverified files.
- **Publication rule, shared by discover, read and rebuild.** A data-zone file is:
  - `unpublished` when an open intent or open ingestion run names it — excluded from candidates; `read` returns outcome `unpublished` without content; counted as `interrupted` coverage.
  - `unverified` when generated state is missing (fresh clone or loss) and, in a git brain, the file's current bytes are not committed — excluded from candidates and listed separately; `read` returns outcome `unverified` and gives content only when `include_unverified` is requested; audit correction publishes it by committing it.
  - `changed_out_of_band` when state is present, no intent names it, and it differs from its index entry (discover compares size and modification time; read and rebuild compare bytes) — returned as a candidate with that label, with metadata taken from the current file where it parses.
  - `published` otherwise.
- In a brain without git, loss of generated state makes publication unverifiable for every file: `rebuild` marks all files `unverified` until the user runs `rebuild --accept-current-files`, which records that acknowledgement in state. Non-git brains are outside beta acceptance.

### C9. Discover

Request: `{query, module?, type?, zone?, limit (default 10), as_of, max_bytes}`.

1. Quick out-of-band check: compare each data-zone file's modification time and size with the index, and list files missing from the index or from disk. This check is a fast hint that runs on every call; it does not run git. A rename-on-save by an editor changes modification time and is caught here. Authoritative classification of what changed, and by whom, belongs to audit (C12).
2. Candidates from the index: lowercase word tokens matched against title (weight 3), summary (2), and tags and display/search fields (1). Documents with empty summaries and non-managed Markdown files are matched on body text only and returned in `body_matches`, never in `candidates`.
3. Labels per candidate: `freshness` (`fresh` when `as_of` ≤ `fresh_until`, `stale` when after, `unknown` when absent), `status`, `superseded_by`, `newer_version_available`, `summary_may_be_outdated`, `changed_out_of_band`, `needs_review`, `conflicts_with`, and the `support` summary (C11).
4. `summary_may_be_outdated` is derived from history: the body changed through an out-of-band or unattributed change after the last brain write that set the summary. It is never written into the note.

Output: `{outcome: matches|no_match|partial, candidates, body_matches, coverage: {indexed, undescribed, invalid, interrupted, unverified, changed_out_of_band}, index: present|missing|stale, truncated, omitted_count}`.

- `partial` when any coverage count other than `indexed` and `undescribed` is non-zero, or the index is missing or stale.
- `no_match` only when there are no candidates and no body matches and coverage is complete.
- Truncation to `limit` or `max_bytes` sets `truncated` and `omitted_count` and does not by itself make the result partial.

### C10. Read

Request: `{id | path, max_bytes, heading?, include_unverified?}`. Output: `{outcome: ok|not_found|invalid|unpublished|unverified, id, path, version, frontmatter, excerpt, truncated, origin, evidence: [{target, exists}], support, labels}`. External URLs are returned, never fetched.

### C11. Source identity, versions and support

- **Source identity**: a normalised URL (lowercase scheme and host, no fragment, no default port, trailing slash removed), a DOI, a brain id, or an identifier declared at import. Different URLs are one identity only when declared equivalent. Content hashes never establish or separate identity.
- **Content version**: the pair (source identity, `original_sha256`). Retrieval dates are observations, not part of the version.
- **Support summary**: follows `evidence` and `derived_from` up to depth 5 and reports `resolution` (`resolved` when every chain reaches a `kind: source` document or retained original; `partial`; `unresolved`; `unknown` when a node is `kind: unknown`; `cyclic`), plus `broken`, `cycles`, `unknown_nodes`, `unretained_external`, `distinct_identities`, `declared_derivations`, `same_content_across_identities`, `independence` (`not_assessed` unless an `independence_assessment` is recorded) and `claim_check` (`not_checked` unless `claim_checks` are recorded).

### C12. Out-of-band audit and correction (git brains)

- **Audit position**: the latest consistent `Brain-Op: audit` commit; without one, the brain's first commit.
- **Report** (`brain audit`): each commit since the position touching data zones gets an evidence level (`record_backed`, `consistent_unrecorded`, `inconsistent_trailer`, `unparseable_trailer`, `out_of_band`). With state present, a `consistent_unrecorded` commit is also flagged `unrecorded_trailer_commit`. Without state, consistent trailer commits are `unverifiable`. Uncommitted changes with no intent are `unattributed_uncommitted`. Each affected file is classified `managed_changed`, `unmanaged_new`, `managed_missing` (rename followed when git detects it, otherwise `removed`), `invalid` or `correction_blocked`.
- **Correction** (`brain audit --apply`), code only:
  - `managed_changed`: re-hash and re-index; no file write.
  - `unmanaged_new`: append a frontmatter block (or missing keys to an existing block) with `kb: 1`, new `id`, `title` (first heading, else file name), `type: unclassified`, `kind: unknown`, `status: draft`, `origin: unknown`, `authored_by: human`, `summary` empty, `created` (an existing valid value, else `unknown`), optional `first_seen` (date of the file's first commit), `reviewed: never`, `evidence: []`, `retention: durable`, and `needs_review`.
  - Verify afterwards that every original line is byte-identical; otherwise restore and report `correction_blocked`.
  - An existing key holding an invalid base-field value → `correction_blocked`, file unchanged. Malformed files → `invalid`, unchanged.
  - `managed_missing`: update the index (follow rename or mark removed); never write files.
  - Commit corrected and unattributed files with `Brain-Op: audit`, which becomes the new position.
- Audit never rewrites `authored_by` or any existing value, and makes no model call.

### C13. Ingestion runs

- **Proposal** (from the ingestion skill): `{run_id, raw_path, raw_sha256 (as read), source_identity, retrieved, destination, document: {frontmatter, body}, attachments: [raw paths], unresolved_fields, supersession_assessment?}`.
- **Apply** (`brain ingest --input <proposal>`):
  1. Re-hash the raw item; a mismatch → `refused` with `raw_changed` before any write.
  2. Look up published `document`s with the same source identity. Same `original_sha256` → `duplicate`: write nothing, return the existing id, its recorded retrieval dates and the new retrieval date, then run the raw-deletion step. Different hash → continue as a new version: the new document gets its own id and `previous_version`, and the proposal must include a `supersession_assessment` (`replaces`, `partially_updates`, `conflicts_with` or `unrelated`, with reasons).
  3. Review check (C14). Unresolved fields or an overwrite → `review_required`, nothing written.
  4. Record one run intent listing every file: kept original (`documents/<slug>/original.<ext>`), attachments, processed document (`documents/<slug>/<slug>.md`).
  5. Write the original and attachments, then the processed document, each through C6 steps 1–6 with its own intent. Change records are `pending`; index updates are staged; no per-file commit. The kept original's hash must equal `raw_sha256`.
  6. Once every file is at its intended hash, make one commit (`Brain-Op: ingest`, one `Brain-Path` per file), mark change records committed, apply staged index updates. Any `target_unexpected` stops the run before commit; nothing is published.
  7. Raw deletion: record a deletion intent with `raw_sha256`; re-read the raw item; if its hash differs, do not delete (`raw_changed`); otherwise delete and commit with `Brain-Op: delete-raw` and `prior=<hash>`.
- **Run outcomes**: `ingested`, `ingested_raw_retained`, `not_published`, `target_unexpected`, `duplicate`, `new_version` (supersession pending review), `review_required`, `refused`.
- **Retry** with the same `run_id`: skip files already at their intended hash, commit once, repeat the raw-deletion check. When generated state was lost, a create whose target already exists, is `unverified` and holds exactly the intended bytes is treated as applied; any other existing target is refused.

### C14. Review, authorship and maintenance grants

- `authored_by` records who contributed content. Audit never changes it. A reviewed update applied to a document with out-of-band content changes since its last agent write sets `authored_by: mixed`.
- A **maintenance grant** is recorded in history, never in frontmatter: an agent `create` of an agent-authored document carries `Brain-Grant: <id> maintenance=agent by=agent`; `brain grant --id <id>` makes a separate `Brain-Op: grant` commit with `by=human` after the skill obtains the user's explicit instruction.
- A grant is valid only while every content change to the document since the grant is a `record_backed` or `consistent_unrecorded` brain write and the working copy is unchanged. After state loss, unverifiable commits make the grant invalid until a new grant.
- **Precedence** — the first matching rule applies:
  1. An open intent on the path: only `recover` may write.
  2. Audit correction: may append missing fields without review, commits separately.
  3. Automatic agent update, only when all hold: valid grant; current version agent-attributed; expected version matches; no unresolved values; the change does not delete, rename or move a document or replace an original or attachment.
  4. Anything else changing existing content requires review. A reviewed write carries `Brain-Review: approved by=human`; approving it does not create a grant.
- **Overwrite**: any change to or removal of existing content not covered by rule 3. Replacing an original or attachment, and deleting, renaming or moving a document, always count. Creating a new path never counts.
- `supersedes` and `superseded_by` are written only by a reviewed update approving a supersession assessment. The prior version stays citable; discovery flags citations of it with `newer_version_available`.

### C15. Projects module

- Folder `projects/<project-slug>/`. Types: `project` (required `project_status`: `active`, `paused`, `closed`; display fields `project_status`, `summary`) and `decision` (required `decision_status`: `proposed`, `accepted`, `superseded`; `decided` date or `unknown`, with `unknown` declared under `uncertainty_values`; display fields `decision_status`, `decided`).
- Module documents use the same base fields, persistence, discovery and audit as base documents; nothing in the code names these types.

### C16. Claude entry path

- Skills copied to both skill folders: find knowledge (discover, select, read within a budget, record a reuse decision — reuse, refresh, extend or new — with its reasons), save knowledge (build a document, run audit report, persist, report the outcome verbatim), ingest from `raw/` (propose, apply, handle review stops), audit (report and, on instruction, apply) and set up a brain.
- Skills never write files directly; every change goes through `brain`. Skills report `partial`, `unverified` and `changed_out_of_band` to the user rather than smoothing them over.
- A **fresh session** is a new agent process at the brain root with no resumed conversation and no prompt naming target paths.

### C17. Development example and evaluation harness

- **Development example** (shipped as a safe, synthetic example in the starter; never a brain committed into the repository): a script sets up a temporary brain from the current commit with the projects module, persists a `project` and an accepted `decision` whose evidence references a public repository contract by URL and commit, commits, then (a) runs `discover` and `read` from a new process and (b) optionally starts a fresh Claude session at the brain root that is told only how to run the CLI's help and asked what was decided and why. It records the operation outputs and, for (b), the session transcript.
- **Evaluation harness**: builds one synthetic brain through `setup` at a pinned commit; creates a fresh copy per run outside the repository; for the baseline arm removes skills, code and index; verifies data-zone hashes are identical across arms; runs each session headless with an isolated configuration and a transcript capture; invalidates a baseline run whose session start lists brain skills. It writes a run manifest (arm, task, repetition, commit, capture path, requested settings) and never depends on external delivery tooling.

## Verification Decisions

### Seams

- **Seam 1 — the operation interface.** Almost every behaviour is verified by running `brain` against a real brain folder in a temporary directory, then observing the JSON output, exit code, file bytes, generated state and git history. Tests never import internal modules to assert on internal structures. Faults are injected only through the C6 fault variables at this same seam.
- **Seam 2 — an agent session at a brain root.** Skill-level behaviour (59–61, 63, and skill-driven setup) is verified by a headless Claude session at a set-up brain, observing its transcript's tool calls and final answer against pre-written labels, plus the brain's state afterwards.

No other seam is introduced. A good test states a behaviour, runs the command, and checks outcomes a user could observe.

### Rules

- Every check is shown to fail under a named injection before it counts: write the failing test first where possible; otherwise mutate the code or fixture and observe the failure.
- Tests use the Python standard library test runner, real git and real filesystem operations in temporary directories. No mocking of the filesystem or git.
- The repository's single check command runs the full suite; it is added by the first code story and extended by each later story.
- There is no prior test code in this repository; the first story establishes the conventions.

### Scenario catalogue

Scenarios run against a brain created by `setup` from a pinned commit and populated with a synthetic collection: two synthetic projects (one of them the development example), about a dozen documents with relevance labels written before runs, one `incomplete`, one past `fresh_until`, one superseded pair, one conflicting pair, one synthesis citing only synthesis, one undescribed plain note, one malformed file and one document with an image attachment, with `as_of` fixed.

| Id | Scenario | Key injections |
| --- | --- | --- |
| S-0 | Setup: layout, both skill folders, setup record hashes match, git optional, generated state ignored, hard-link smoke check | tamper a copied file; simulate a filesystem refusing hard links |
| S-A | Persist and inspect: `written` with id and version; plain Markdown; change record and trailer commit; duplicate create `refused`; stale `expected_version` after a hand edit `refused` with the edit intact; attachment hash matches | hand edit between read and update |
| S-B | Fresh-session selective retrieval: the right project's documents found; other project and body-only mentions excluded from candidates; reads within a byte budget; answer cites resolvable ids | none (labels are the oracle) |
| S-C | No relevant result: `no_match` with complete coverage; `partial` with a malformed file or out-of-band change in scope; `partial` with `index: missing` | delete the index; add a malformed file |
| S-D | Stale evidence: `stale` label past `fresh_until`; superseded document shows successor; the session proposes a targeted refresh | move `as_of` |
| S-D2 | Conflicting evidence: a declared conflict echoed and both sides shown; a synthesis whose chain reaches no source reports unresolved support | remove the `conflicts_with` link; unresolved support still reported |
| S-D3 | Incomplete output: `incomplete` labelled; the session reuses the complete part and proposes refreshing the gap | none |
| S-E | Interrupted persist: each C6 outcome observed; create refused by the hard link; unexpected target content after apply | SIGKILL at `after_temp` and `after_apply`; corrupt target after apply |
| S-E2 | Bookkeeping failure recovered exactly once: change record, commit, and index failures each recovered with one record and one commit | `fail` at `before_change_record`, `before_commit`, `before_index` |
| S-E3 | Kill at three points, each classified correctly by `recover` | `kill` at `after_intent`, `before_apply`, `after_apply` |
| S-F | Traceability: id and version cited; origin and evidence returned; broken internal reference `exists: false`; external URLs verbatim | break an internal id |
| S-F2 | Provenance graph: identity, version, declared derivation, same-content flag, broken reference, cycle, unknown node; changing a mirror's content never yields "independent" | alter mirror content |
| S-G | Out-of-band change and correction: body edit committed without trailer; new unmanaged note; rename; an editor-style write-temp-then-rename save; discover `partial` with labels; audit classifies and corrects; later discover finds all at current paths; no model call | forged consistent trailer without a record; inconsistent trailer; unrecorded trailer commit with state present |
| S-G2 | Fresh clone or lost state: consistent trailer commits `unverifiable`, never verified; unverified files hidden from candidates until audit publishes them | delete generated state |
| S-G3 | Metadata correction and preservation: appended fields only; original lines byte-identical; `correction_blocked` on invalid existing value; malformed untouched; ingested document with known origin and `created: unknown` validates | invalid existing value |
| S-H | Ingestion and projects module: raw article ingested with original kept and origin recorded; raw removed after commit; a new custom type added by file validates and is discoverable with display fields; unresolved field stops for review | missing required type field |
| S-H2 | Ingestion recovery: kill before publish (nothing discoverable, rebuild agrees, read `unpublished`); kill after run commit before raw deletion; raw modified after proposal → `raw_changed`, not deleted; duplicate with a new retrieval date writes nothing; new version with supersession proposed, not applied; state lost mid-run then retry adopts identical files | `kill` at `before_commit`, `after_run_commit`; modify raw; delete state |
| S-I | Review and maintenance: automatic update under a valid grant; stop after an out-of-band edit; one approved update does not restore automatic updates; explicit grant restores them; audit additions without review; open intent blocks writes; attachment replacement stops | out-of-band edit; open intent |
| S-DEV | Development example: decision and evidence retrieved by a fresh process with evidence resolving; fresh Claude session answer cites the decision id and version | remove the decision's evidence target |

### Evaluation

Seven labelled tasks (three with relevant evidence, one needing two documents; one no-match; one stale or superseded; one conflicting pair; one after an out-of-band edit), three repetitions, two arms: baseline (same brain files, no skills, code or index; ordinary file tools) and brain (skills and operations). Brain arm workload: setup once, ingest ten items, two out-of-band edits plus an audit, then the retrieval runs; baseline arm runs the same retrieval tasks over the same final files. Report retrieval quality, context loaded, preparation overhead, interface overhead, clarification and rework, and usage coverage, per phase and cumulatively. No numeric target; unknown values are reported as unknown, never as zero; total workflow effort is compared, not only retrieval turns.

## Boundaries

Out of the beta:

- Contribute-back and update skills; inbox routing into modules; generated index notes; automatic backfill; non-git audit fallback (non-git brains work but carry the C8 limitation and are outside acceptance).
- Compiling or regenerating `wiki/` content; content drafting and writing workflows.
- Tracking modules (companies, people, application records, commitments), identity keys and relation types.
- Obsidian plugin, web UI, MCP adapter, embeddings or graph database, image understanding or search.
- Concurrent multi-writer guarantees; synchronisation conflict files.
- Migration or backfill of any existing note collection.
- Codex entry path as an acceptance requirement (it may be added and claimed once demonstrated).
- Fetching external URLs during read or discovery; refreshing sources automatically.
- Lint that fixes anything: lint reports only.

## Further Notes

### Using agent-brain to build agent-brain

- **Now.** Accepted contracts (this specification), architecture decisions (`docs/adr/`) and contributor guidance live in this repository. GitHub issues and pull requests are the authority for live work state; documents link to them rather than tracking status.
- **During construction.** The development example (behaviour 61, C17, scenario S-DEV) is the earliest dogfooding point. It needs only setup, persistence, read, index and discovery, and the projects module, so it can land before audit, ingestion and review work. It demonstrates capturing a real public decision of this project with its evidence and retrieving it from a fresh session. It is not skill-driven; the skill-driven fresh-session scenario (S-B) still follows. Because it lands before audit, out-of-band changes in that example brain are only flagged by discovery's quick check; they are not classified or corrected.
- **After beta acceptance.** A maintainer can set up a personal, private brain from an accepted starter commit and use its projects module for development knowledge about this project, linking to repository contracts and issues rather than copying them. That brain is a separate instance: never nested in this repository, never committed here.
- The starter does not contain or depend on any particular delivery or orchestration tooling used to build it.

### Promotion of rationale

Rationale needed to interpret a contract is written here or in an ADR. Earlier exploratory material is not required to understand this specification.

### Terms

- **Brain**: a user's own knowledge folder, created by setup from the starter; private by default.
- **Starter**: this repository — default layout, base skills, supporting code, base and module types, synthetic examples. It never contains a user's knowledge.
- **Zone**: one of the brain's top-level data folders (`inbox/`, `raw/`, `documents/`, `wiki/`) or a module folder.
- **Module**: a named extension bringing its own folder, document types, skills and discovery participation (projects in the beta).
- **Document type**: a named kind of document with required fields and allowed values, defined in a data file.
- **Origin**: where a document's content originally came from.
- **Out-of-band change**: a change to brain data made outside brain operations, for example in a note editor or by sync.
- **Discovery**: finding existing knowledge relevant to a question, returning candidates rather than whole documents.
- **Reuse decision**: a recorded judgment whether existing knowledge can be reused, needs refresh or extension, or cannot support the question.
- **Context manifest**: a bounded record of the inputs selected for a task and why (used by later consumers; not a beta operation).
