---
name: flori-delivery-train
description: Plan and execute Flori repository delivery through one unified lifecycle for either a single review-first change or multiple dependent units. Use when an agent (Codex or Claude Code) changes, reviews, tests, commits, releases, deploys, or coordinates agents/worktrees in the Flori repo and must select scale, risk, and release profiles, reuse evidence, enforce review gates, or close a release train without micro-commit churn.
---

# Flori Delivery Train

Use one delivery protocol for every change. Treat a single unit as a one-node train; add DAG scheduling and batch integration only for multiple units.

## Load authority

1. Read `$REPO/CLAUDE.md`, `docs/README.md`, `docs/11-dev-workflow.md`, and `docs/12-cicd.md` before acting.
2. Read `.local/processing/迭代记录规范.txt` and the current work item or create one before edits.
3. Follow explicit user limits first. In particular, preserve review-first, no-commit, no-push, or no-deploy instructions.
4. Treat repo governance as policy and this skill as execution guidance. Stop and report any conflict instead of silently overriding the repo.

## Select profiles

Record all three profiles in the work item:

| Dimension | Values | Gate |
|---|---|---|
| Scale | `single`, `multi` | Add DAG, hotspot serialization, batches, and a train integrator only for `multi` |
| Risk | `normal`, `contract`, `critical` | Synchronize contracts for `contract`; freeze invariants/threats and require adversarial tests plus independent review for `critical` |
| Release | `review-first`, `ci`, `full-deploy` | Stop respectively at user review, required CI success, or local/NAS and ECS external verification |

Default security boundaries, database migrations, backup/restore, identity, credentials, and authorization to `critical`.

## Start safely

1. Inspect `git status --short --branch`, recent log, `git worktree list --porcelain`, relevant branches, and current deployment/CI state when in scope.
2. Preserve unrelated user changes. Use a lease worktree when work is parallel, main is dirty, or isolation is needed.
3. Define each unit by complete acceptance and rollback boundaries. Do not split by agent, file count, feedback round, or checkpoint.
4. Record integrator, base, branch/worktree, file scope, hotspot owner, tests, first-artifact deadline, reclaim condition, profiles, dependencies, and evidence location.
5. For `critical`, write the invariant, attack, rejection, rollback, and recovery matrix before implementation.

## Run a single unit

1. Implement the smallest complete vertical value, not a partial layer.
2. Run new and directly related tests through `scripts/test.sh`. Set a unique `TEST_WARM_NAME` in a worktree.
3. Bind evidence to a candidate identity, inputs, command, runtime image/config, and result. Prefer a checkpoint/tree SHA; use a deterministic diff digest for an uncommitted `review-first` candidate.
4. Review according to risk, then have the unit integrator squash checkpoints and run touched-path integration, the affected build, and API or Playwright verification.
5. For `review-first`, leave the result inspectable and uncommitted until the user approves. Merge a worktree diff back only when requested and safe for unrelated main changes.
6. For a product-changing `ci` or `full-deploy` unit, use the value commit as the release commit and bump once. Do not bump pure docs, governance, research, test, or CI-tuning commits.

## Run multiple units

1. Build a dependency DAG with unit IDs, rollback boundaries, owners, serial hotspots, parallel nodes, and integration batches.
2. Assign one train integrator. Unit integrators may assemble local value commits, but only the train integrator may perform the final bump, push, deploy, and global cleanup.
3. Start detailed audit and implementation only after dependencies are stable. While blocked, allow one lightweight preflight of at most 15 minutes; do not periodically rescan the same files.
4. Keep one no-version value commit per completed unit on the release branch. Do not push or deploy each node separately.
5. Run cross-unit tests, builds, and manual checks once per integration batch. Rebuild early only when Dockerfile, dependencies, build context, or runtime inputs changed.
6. After all batches pass, create one `build(release)` commit that bumps the version, then push once and perform CI/deployment once. If an early push is mandatory, close the current train as an independent release and move remaining units to a new train.

## Enforce agent liveness

1. Require a first verifiable artifact within 10 minutes unless the lease declares a justified longer command.
2. Accept a heartbeat only when it contains a diff/checkpoint, an observable test/build process, completed evidence, or a reproducible blocker. Do not accept “planning” alone.
3. Ping once at timeout. If no evidence appears within another 5 minutes, interrupt, archive useful state, reclaim the lease, and reassign.
4. Never let two agents edit the same hotspot or operate the same Git worktree, image tag, container, version, push, or deployment resource.

## Reuse evidence and bound review

1. Reuse evidence only when candidate identity, inputs, command, runtime config, and dependency image still match.
2. Let implementers run red and targeted tests. Let reviewers rerun the risk matrix, newly affected scope, and unverifiable evidence. Let integrators run batch integration. Let final CI run the full gate.
3. Default `normal` to one implementation review. Default `contract` and `critical` to one implementation review plus one independent final review.
4. Reopen a passed gate for a genuinely new P0/P1 class. Close same-class fixes inside the current round; move non-blocking P2/P3 expansion to a later unit.
5. Treat checkpoints as recovery points, not review units or main commits.

## Tune CI without main churn

1. Separate queue delay, workflow control time, and runner execution time. Use at least three comparable runs for a stable baseline when available.
2. Simulate sharding and dependency changes from historical timing before pushing.
3. Require each candidate to state the bottleneck, predicted saving, and protected invariants. Skip a real run when expected saving is below 10 seconds and no correctness issue is fixed.
4. Use an experiment branch and at most three candidate cycles by default. Fix forward with a new SHA; never rerun a known bad SHA as proof.
5. Consolidate the proven result into one main value commit. A user-specified hard SLA may require more experiments, but not more main micro-commits or weaker gates.

## Close and report

1. Update unit work items and the train evidence ledger with implementation, waits, review, integration, release timing, final SHAs, CI URLs, image digests, versions, health, and external checks as applicable.
2. Reconcile every acceptance item. Mark conditional work as implemented or explicitly not triggered with evidence.
3. Reclaim merged worktrees, checkpoint branches, temporary branches, test containers, and experiment resources under repo rules.
4. Verify `git status`, worktrees, merged/unmerged task branches, origin alignment when pushed, and `.local` ignore status.
5. Report the completion matrix, commits, validation, CI/deployment evidence, remaining conditional items, and external limitations. Never claim success for skipped or unverified gates.

## Operational pitfalls

- Use the repo test entrypoint only; a worktree needs a unique warm test container.
- Recreate the frontend container when a rebuilt bundle appears stale.
- Store Playwright and other local evidence under the current `.local/processing` directory, never in tracked paths.
- Do not expose secret values. Record only approved credential locations and verification outcomes.
