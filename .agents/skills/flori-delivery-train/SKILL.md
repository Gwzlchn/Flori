---
name: flori-delivery-train
description: Deliver Flori work quickly from an approved bounded task packet while enforcing vNext scope, type, complexity, risk, Git, CI, worktree, and production-operation boundaries. Use for reviews, code or documentation changes, tests, commits, pushes, CI, releases, operations, and sub-agent coordination in this repository.
---

# Flori vNext Delivery

Optimize for a small verified vertical result. Do not build process or architecture that the task does not need.

## 1. Choose one mode

| Mode | Boundary |
|---|---|
| `consult` | Read-only answer, diagnosis or review. No durable write or external mutation. |
| `deliver` | An approved task packet authorizes implementation through its stated commit, push and CI endpoint. |
| `operate` | Production, deployment, credentials, external accounts, content delivery, data deletion or downtime. Requires separate authorization. |

Do not split normal work into change/commit/push approval rounds. In `deliver`, stop only on the conditions in section 5.

## 2. Load only the needed authority

1. Treat `CLAUDE.md` as workflow authority.
2. For vNext scope, start at `docs/vnext/README.md` and read only the task's routed documents.
3. Read old ADRs or Python code from `main` or Git history only when current-production evidence or rollback is required. They do not create vNext compatibility requirements.
4. Read `.local/processing/迭代记录规范.txt` only when a WP, migration, operation or long investigation needs a durable record.
5. Read content-delivery ledgers only for actual source curation, retry or deletion operations.

## 3. Require a bounded task packet

Before editing, the packet must state:

- one observable user or developer value;
- dependencies and risk level L0-L3;
- allowed paths and explicit non-goals;
- contract revision and fixture when applicable;
- allowed counts for new architecture primitives;
- compile/test/manual acceptance commands;
- rollback boundary and delivery endpoint.

Architecture primitives are tables, persistent states, endpoints, Pipeline fields, Artifact kinds, crates, dependencies, services, images, Providers, feature flags, compatibility readers and fallbacks. Their default allowed increase is zero.

## 4. Implement the smallest complete slice

1. Make the narrowest change that satisfies the acceptance behavior.
2. Keep contract, implementation, consumer, tests and required docs in one WP.
3. Do not add forwarding service/repository/manager layers, single-implementation factories, plugin registries, shadow DTOs or future compatibility.
4. Prefer two short repetitions over a premature framework. Abstract only after three real callers, unless the frozen contract already requires two implementations.
5. Keep Rust domain types authoritative. Generate OpenAPI and TypeScript; do not repair type errors with dynamic Value, aliases, casts or duplicate models.
6. Run a deletion pass before commit.

For every product WP, apply the single work-package accounting rule in [vNext development](../../../docs/vnext/development.md#工作包复杂度账本); file splitting alone is not a complexity reduction.

Stop and re-slice a normal small task when it exceeds 300 net handwritten production lines, 10 handwritten files, two business crates, two frontend pages, or any undeclared architecture primitive. Also stop after two failed implementation paths, 20 minutes without a compiling skeleton, or 45 minutes without a local green result.

## 5. Stop only for a real boundary change

Ask the user again only when:

1. the frozen product behavior or keep/delete decision must change;
2. an undeclared architecture primitive is required;
3. the complexity alarm cannot be resolved by splitting;
4. the approved fixture, tool or environment cannot perform the acceptance method;
5. work would spend unapproved AI money or mutate accounts, production, credentials, public networking or data;
6. CI exposes an issue owned by another WP.

Do not stop merely to request diff review, commit permission, push permission or permission to fix in-scope CI.

## 6. Validate by risk

| Level | Default evidence |
|---|---|
| L0 | Static checks, links and decision simulations |
| L1 | fmt/check/typecheck plus direct unit tests |
| L2 | L1 plus a real SQLite, HTTP or sidecar integration |
| L3 | L2 plus adversarial inputs, crash points, idempotency and independent final review |

Mocks may replace external sites, Qoder/Codex and media tools. They must not replace DAG execution, SQLite transactions, Artifact commit, evidence resolution or frontend contracts.

Bind reusable evidence to candidate identity, input, command, runtime configuration, dependency image and result. Re-run only when one of the first five changes or the reviewer cannot verify it.

## 7. Coordinate sub-agents only when useful

Default to one Agent. Use parallel agents only for at least three independent nodes or when the user explicitly asks.

Before delegation, assign each child:

- one WP value and dependency state;
- a disjoint path scope and non-goals;
- contract revision, fixture and architecture budget;
- test command and first evidence deadline;
- shared hotspot owner and cleanup condition.

Cargo.lock, SQLite schema, Pipeline schema, Artifact manifest, Runner OpenAPI, frontend router/generated types, CI and Compose each have one owner. Children do not change final version, integration branch, production or product scope. The main Agent integrates, verifies, commits, pushes and reclaims worktrees.

Use `$FLORI_WORKING_DIR/wt/<slug>` for parallel or dirty-tree work. Each worktree has its own target/tmp. Never run task-scoped `cargo clean` on shared caches.

## 8. Deliver without process inflation

For an approved `deliver` packet:

```text
implement -> local minimum green -> deletion pass -> risk review
          -> intentional commit -> push -> in-scope CI fix-forward -> report
```

- One independently acceptable and reversible value equals one commit.
- Checkpoints and review rounds are not final commits; squash fixups before integration.
- Small tasks do not need long worklogs. Keep one compact record for a WP, migration, operation or long investigation.
- Do not bump the product version for rust-vnext intermediate or governance commits. Bump once at an actual release candidate.
- CI is final evidence, not an interactive design loop. Get a local green result before push.
- If the branch has no applicable CI yet, state that fact; do not expand the task by modifying CI owned by a later WP.

## 9. Protect operations

`operate` requires an exact target, rejection conditions, rollback or recovery, and post-operation reconciliation.

- Ordinary upgrades do not create Flori backups.
- A schema-changing upgrade stops all writers and retains the old SQLite and image. Migration failure exits and rolls back; do not add compatibility reads.
- NAS owns Artifact backup and disaster recovery.
- `delete_source` removes the whole Source and verifies no logical or physical orphan remains.
- Production main merge, deployment, data deletion, credential changes and WP16 cold cutover always need explicit user authorization.

## 10. Close at the authorized endpoint

Report the delivered value, candidate or commit, exact validation, complexity delta and remaining dependency. Check task-owned branches/worktrees before finishing and reclaim only resources created by the task.

Never claim CI, deployment or external verification that did not run.
