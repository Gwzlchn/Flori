---
name: flori-delivery-train
description: Orchestrate Flori repository work with a fast read-only consult path and staged change, ship, or operate gates. Use when Codex or Claude Code reviews, changes, tests, commits, releases, deploys, operates content/runtime state, or coordinates agents/worktrees in the Flori repo and must preserve value boundaries, risk gates, evidence reuse, or release integrity without loading unrelated workflow rules.
---

# Flori Staged Delivery

Classify the requested action before loading process documents. Preserve rigor at the boundary being changed; do not run later delivery stages early.

## Select the execution mode

| Mode | Boundary | Required process |
|---|---|---|
| `consult` | Read-only answer, diagnosis, review, status, or discussion; no durable artifact or external mutation | Inspect only relevant evidence and report. Do not create a work item, select new delivery profiles, inventory unrelated Git state, or run delivery cleanup. A formal reviewer inherits the candidate's risk gate. |
| `change` | Edit tracked files or durable local project records; stop before commit/push/deploy | Create or update one work item, implement, run targeted validation, and leave a reviewable candidate. |
| `ship` | Commit, push, PR, CI, version, image, or deployment is requested | Run `change`, then load and execute only the requested release stages. |
| `operate` | Mutate runtime data, content delivery state, credentials, cleanup targets, or production resources | Record the operation, establish rollback/recovery evidence, and verify the external result. Add `ship` only when code also needs release. |

Mode may advance when the user expands the finish line. Do not infer `ship` or `operate` from a `consult` or `change` request.

## Load authority lazily

1. Treat `$REPO/CLAUDE.md` as governance authority. If its current contents are already supplied in context, do not read it again.
2. For `consult`, read only the files, runtime state, or history needed to answer. Read `docs/README.md` only when document routing is unclear.
3. For `change`, read the current work item. Read `.local/processing/迭代记录规范.txt` only when creating or structurally changing a work item. Read the relevant sections of `docs/11-dev-workflow.md` only for worktrees, multiple units, evidence/review, or integration.
4. For `ship`, read `docs/11-dev-workflow.md` §4.7 plus the relevant sections of `docs/12-cicd.md` immediately before commit/release work.
5. For `operate`, read the affected runbook. For source curation, delivery, cleanup/retry, or delivery-driven fixes, read `.local/delivery/README.txt` plus only the affected catalog, state, batch, and Bug records.
6. Follow explicit review-first, no-commit, no-push, no-deploy, and scope limits first. Stop on conflicts with governance.

## Profile only mutating work

For `change`, `ship`, and `operate`, record:

- Scale: `single` or `multi`.
- Risk: `normal`, `contract`, or `critical`.
- Release: `review-first`, `commit-only`, `ci`, or `full-deploy` when code delivery is involved. `commit-only` creates a local no-version value commit and stops before push; if it later advances to release, amend/squash the still-local project commit before the first push so the version stays in the value commit.

Actual changes to security boundaries, database migrations, backup/restore behavior, identity, credentials, authorization, or destructive production state default to `critical`. A read-only discussion or design review about those topics remains `consult`; include relevant invariants and recovery concerns in the answer without invoking implementation, test, or release gates.

A durable design for one of those boundaries is `change/review-first` with risk profile `normal` unless it changes a tracked external contract. Mark its risk gate `critical-target`, record a design-level invariant/threat/rejection/rollback/recovery matrix, and obtain one independent design review before implementation relies on it; do not run adversarial product tests or release gates for the design document itself.

`critical` requires an invariant/threat/rejection/rollback/recovery matrix, adversarial tests, and independent final review. `contract` requires the contract and consumers in the same value unit. `multi` adds a dependency DAG, serial hotspot owners, integration batches, and one train integrator.

## Start a mutating unit

1. Define one independently acceptable and reversible value boundary.
2. Inspect Git/deployment state only to the extent needed for the selected mode. Use `.agents/skills/flori-delivery-train/scripts/delivery-snapshot.sh start` for the compact Git baseline when Git changes are in scope.
3. Preserve unrelated changes. Use a lease worktree when work is parallel, main is dirty, or isolation is needed.
4. Keep the work item compact. Always record outcome, scope, baseline, mode/profiles, validation, and remaining work. Add agent leases, hotspot owners, release, deployment, and content-delivery fields only when triggered.
5. Assign one integrator. For `multi`, freeze the DAG and shared owners before parallel implementation.

## Implement and validate

1. Build the smallest complete vertical value; keep contract, migration, consumers, tests, and required docs in the same unit.
2. Run new and directly related tests through `scripts/test.sh`. Use a unique `TEST_WARM_NAME` in a worktree. Do not run product tests for governance-only or ordinary read-only documentation work; use proportional static validation. A formal reviewer of a `contract` or `critical` candidate may rerun reviewer-scoped contract, risk-matrix, adversarial, or otherwise unverifiable tests without converting the review into `change`.
3. Bind reusable evidence to candidate identity, inputs, command, runtime config, dependency image, and result. A candidate that includes ignored durable files uses a composite digest covering those files. Reuse the result only while the first five dimensions remain unchanged.
4. Let implementers run targeted tests, reviewers challenge changed risks and unverifiable evidence, integrators run touched-path integration, and final CI run the full gate. Do not repeat the same full suite at every role.
5. Default `normal` to one implementation review. Default `contract` and `critical` to one implementation review plus one independent final review. Freeze the candidate before final review and reopen only for a new P0/P1 inside the changed boundary; defer P2/P3 and hypothetical mixed-version support instead of growing the active unit.
6. For `review-first`, leave the candidate inspectable and uncommitted until the user approves.

## Ship or operate only when selected

- Preserve one value commit per acceptance/rollback boundary. One complete released project has one version, carried by its final value commit. Checkpoints are recovery points and must be squashed before main; do not create commits for agents or review rounds.
- Before the first push, amend a promoted `commit-only` unit or the final `multi` value commit to include the one version bump; the train may instead squash into one complete project commit. Do not create a version-only commit by default. Allow `build(release)` only when the value commits are already on a shared remote and history must not be rewritten, and record that reason. Use Git tags or GitHub Releases as release markers.
- For `multi`, integrate by dependency batch, then amend/squash the version once, push once, and deploy once. Run builds early only when build inputs changed.
- For content delivery, follow `.local/delivery/README.txt`; keep catalog, state, batch, Bug, and processing authorities separate.
- For CI tuning, follow `docs/12-cicd.md` and use historical simulation plus bounded experiment cycles; do not churn main.
- For production/destructive operations, require a precise target manifest, recoverable backup when needed, fail-closed checks, and post-operation reconciliation.

## Default to cold upgrades on the personal NAS

The default backend release model is a maintenance-window cold upgrade, not zero-downtime mixed-version operation:

1. Pause new intake and subscriptions.
2. Let short jobs finish; record and cancel or defer long jobs.
3. Create and verify an exact DR generation.
4. Stop Scheduler, API, MCP, and every Worker that can write current state.
5. Start one product version, let one migration owner upgrade the database, and verify schema, ledger, readiness, and component versions.
6. Explicitly rerun or resubmit invalidated steps, resume intake, and perform external acceptance.

Do not extend old/new Scheduler or Worker coexistence, automatic active-DAG reshaping, old-writer-to-new-schema bridges, permanent support for every historical artifact schema, or generation machinery that exists only for rolling upgrades. Keep current compatibility code unless a bounded cleanup is separately authorized. Reopen such work only for an explicit zero-downtime or independently upgraded multi-node requirement. Exact DR, migration atomicity, idempotent commits, duplicate-AI-charge prevention, security, and evidence integrity remain mandatory.

## Close at the reached mode

1. Reconcile acceptance items and report only evidence relevant to stages actually reached.
2. For `change`, update the work item and leave the reviewable candidate; do not perform release-only checks.
3. For `ship` or a worktree-backed `change`, use `.agents/skills/flori-delivery-train/scripts/delivery-snapshot.sh close <task-branch>` to verify relevant Git state. Add `--extra <label=path>` for ignored durable candidate files, then reclaim only resources created by this unit when authorized.
4. For `operate`, reconcile the affected runtime/content authorities and recovery evidence.
5. Never claim success for skipped or unverified gates. State which later modes were not requested.
