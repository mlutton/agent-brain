# ADR 0001: The starter repository and each brain are separate

Status: Accepted (merged with the beta specification in #2)
Date: 2026-09-14

## Context

agent-brain has two audiences with opposite needs.

- The **starter** — default layout, base skills, supporting code, document types and synthetic examples — should be public, reviewable and improvable by anyone.
- A **brain** — someone's actual notes, research, decisions and project context — should be private, with its own history, and must never leak into a public repository.

Users edit brains in ordinary note editors such as Obsidian, and agents work in them through skills. Both need a folder that looks like a normal Markdown vault, with machinery out of the way.

Options considered:

1. **The brain is a clone of the starter, with user data git-ignored.** Simple to update. But user data then has no version history, and a single ignore-rule mistake publishes private notes to the public repository.
2. **The starter contains a nested private data repository** (submodule or inner repository). Keeps history, but nested repositories are hard for people and agents to operate correctly: wrong-repository commits, confusing status, and fragile tooling.
3. **Fork topologies** (public fork for improvements, private copy for data). Workable for experienced git users; too much ceremony for the common case, and still mixes the two concerns in related histories.
4. **Separate instances created by setup.** A setup step copies what a brain needs from a specific starter commit into a new folder that is its own repository.

## Decision

Option 4.

- The **starter repository** holds only freshly authored system source: layout definition, skills, supporting code, base and module document types, tests, synthetic examples, this specification and ADRs, and contributor guidance. The one exception to freshly authored content is a pinned, vendored third-party library under `vendor/`, kept with its own licence, which the specification names explicitly. It never contains a real brain or anyone's knowledge.
- A **brain** is created by the setup skill from a specific starter commit into a separate folder. Setup copies skills into `.claude/skills` and `.agents/skills`, and code, type definitions and the layout into `.brain/` and the data folders. It writes a setup record: the starter URL, the commit, and every copied file with its hash.
- Setup recommends, and the maintainer's own use requires, a **private git repository for the brain** that tracks user data and the copied system files, and ignores only generated state (the rebuildable cache and the local operation journal).
- A brain is **never nested** inside the starter or any other repository, and the starter is never nested inside a brain. Setup refuses a target inside another repository's working tree.
- Code always receives the brain root explicitly; nothing locates a brain by walking up from the current directory.
- **Improvements flow back** by ordinary pull requests to the starter. Skills to compare a brain's copied system files with the setup record and propose changes upstream, or to update a brain from a newer starter commit, are planned after the beta.
- **Work state lives in the tracker.** GitHub issues and pull requests on this repository are the authority for open, in-progress and blocked work. Repository documents link to them and do not duplicate status.
- **Delivery tooling stays outside.** The starter does not include or depend on the tooling used to plan, dispatch or verify its own development.

## Consequences

- Private data has full history in its own repository and cannot be published by a misconfigured ignore rule in the starter.
- Each brain records exactly which starter version it came from, so later update and contribute-back steps can distinguish unchanged system files from local changes.
- Copied code means a brain keeps working if the starter moves on; updates are deliberate rather than automatic. The cost is that improvements do not reach existing brains until an update step exists.
- A brain without git still works, but with weaker out-of-band detection and recovery guarantees; beta acceptance uses a git-backed brain.
- Developing agent-brain with agent-brain follows the same rule. The repository holds the reusable source and a synthetic development example that sets up a temporary brain. A maintainer's real development-knowledge brain is a separate private instance that links to this repository's contracts and issues.
