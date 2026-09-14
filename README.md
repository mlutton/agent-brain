# agent-brain

An Obsidian-compatible knowledge workspace for people who work with coding agents: ordinary Markdown documents, agent-facing skills, and explicit, tested contracts for persisting knowledge and finding it again in a fresh session.

## Current status

**Being specified, not implemented.** This repository contains no working code yet. The beta's behaviour is proposed in the specification below and is under review; nothing described there should be assumed to work today.

| Capability | State |
| --- | --- |
| Beta specification | Proposed, under review |
| Repository topology decision | Proposed, under review |
| Setup, validation, persistence, discovery, read, audit, ingestion, projects module, Claude skills | Not started |
| Codex entry path | Not started; optional for the beta, claimed only once demonstrated |

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
- Public content is freshly authored; examples are synthetic.

## License

[MIT](LICENSE)
