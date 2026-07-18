"""commit fence 状态机测试:CAS 拒绝、一次性 token、换代 409 与恢复决策表全行。"""

import itertools

import pytest

from shared.step_manifest import ManifestError
from shared.step_commit import (
    CommitInFlightError,
    CommitPhase,
    CommitToken,
    CommitTokenIdentity,
    InvalidPhaseError,
    LeaseInvalidError,
    RecoveryAction,
    RecoveryFacts,
    StaleExecutionError,
    StepCommitFence,
    TokenExpiredError,
    TokenMismatchError,
    decide_recovery,
)


DIGEST = "sha256:" + "a" * 64


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_fence(clock: FakeClock | None = None, ttl: float = 60.0) -> StepCommitFence:
    counter = itertools.count()
    return StepCommitFence(
        job_id="jobs_live_001",
        execution_step="part:pt_abcd::01_download",
        token_ttl_sec=ttl,
        clock=clock or FakeClock(),
        token_id_factory=lambda: f"tok{next(counter)}",
    )


def test_happy_path_full_chain() -> None:
    fence = make_fence()
    assert fence.phase is CommitPhase.IDLE
    fence.begin_execution("exec_1", 7)
    assert fence.phase is CommitPhase.RUNNING
    fence.mark_staged("exec_1", 7)
    assert fence.phase is CommitPhase.STAGING
    token = fence.begin_commit("exec_1", 7, DIGEST)
    assert fence.phase is CommitPhase.COMMITTING
    assert token.job_id == "jobs_live_001"
    assert token.execution_step == "part:pt_abcd::01_download"
    assert token.exec_id == "exec_1"
    assert token.job_generation == 7
    assert token.candidate_digest == DIGEST
    # promote 前后可重复校验,校验不消费 token。
    fence.validate_token(token)
    fence.validate_token(token)
    fence.publish_manifest(token)
    assert fence.phase is CommitPhase.MANIFEST_PUBLISHED
    # manifest 已发布后同一 token 不得再用于 promote。
    with pytest.raises(InvalidPhaseError):
        fence.validate_token(token)
    fence.report_done(token)
    assert fence.phase is CommitPhase.PROJECTED_DONE
    assert fence.active_token is None
    with pytest.raises(TokenMismatchError):
        fence.report_done(token)


def test_begin_commit_without_staging_is_allowed() -> None:
    # staging 是 running 的子阶段,集成层可以不单独上报 mark_staged。
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    token = fence.begin_commit("exec_1", 7, DIGEST)
    fence.publish_manifest(token)
    fence.report_done(token)
    assert fence.phase is CommitPhase.PROJECTED_DONE


def test_begin_commit_cas_rejections() -> None:
    fence = make_fence()
    with pytest.raises(StaleExecutionError):
        fence.begin_commit("exec_1", 7, DIGEST)  # 未登记执行
    fence.begin_execution("exec_1", 7)
    with pytest.raises(StaleExecutionError):
        fence.begin_commit("exec_ghost", 7, DIGEST)
    with pytest.raises(StaleExecutionError):
        fence.begin_commit("exec_1", 6, DIGEST)
    with pytest.raises(LeaseInvalidError):
        fence.begin_commit("exec_1", 7, DIGEST, lease_valid=False)
    with pytest.raises(ManifestError):
        fence.begin_commit("exec_1", 7, "sha256:" + "A" * 64)
    # 全部拒绝都不产生 token,也不推进阶段。
    assert fence.phase is CommitPhase.RUNNING
    assert fence.active_token is None


def test_double_begin_commit_rejected_while_token_live() -> None:
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    fence.begin_commit("exec_1", 7, DIGEST)
    with pytest.raises(InvalidPhaseError):
        fence.begin_commit("exec_1", 7, DIGEST)


def test_mark_staged_phase_rules() -> None:
    fence = make_fence()
    with pytest.raises(StaleExecutionError):
        fence.mark_staged("exec_1", 7)
    fence.begin_execution("exec_1", 7)
    fence.begin_commit("exec_1", 7, DIGEST)
    with pytest.raises(InvalidPhaseError):
        fence.mark_staged("exec_1", 7)


def test_expired_token_rejected_then_reissue() -> None:
    clock = FakeClock()
    fence = make_fence(clock, ttl=60.0)
    fence.begin_execution("exec_1", 7)
    stale = fence.begin_commit("exec_1", 7, DIGEST)
    clock.now += 61.0
    with pytest.raises(TokenExpiredError):
        fence.validate_token(stale)
    with pytest.raises(TokenExpiredError):
        fence.publish_manifest(stale)
    # 同一执行崩溃重试:旧 token 过期后允许重新签发,旧 token 从此永久失效。
    fresh = fence.begin_commit("exec_1", 7, DIGEST)
    assert fresh.token_id != stale.token_id
    with pytest.raises(TokenMismatchError):
        fence.validate_token(stale)
    fence.publish_manifest(fresh)


def test_forged_token_with_matching_fields_rejected() -> None:
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    token = fence.begin_commit("exec_1", 7, DIGEST)
    forged = CommitToken(
        token_id="forged",
        job_id=token.job_id,
        execution_step=token.execution_step,
        exec_id=token.exec_id,
        job_generation=token.job_generation,
        candidate_digest=token.candidate_digest,
        issued_at=token.issued_at,
        ttl_sec=token.ttl_sec,
    )
    with pytest.raises(TokenMismatchError):
        fence.publish_manifest(forged)
    with pytest.raises(TokenMismatchError):
        fence.validate_token("not-a-token")


def test_report_done_requires_published_manifest() -> None:
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    token = fence.begin_commit("exec_1", 7, DIGEST)
    with pytest.raises(InvalidPhaseError):
        fence.report_done(token)


def test_begin_commit_cas_live_job_generation() -> None:
    # Lua 集成必须比对校验时刻的 live generation,不是 claim 快照;不一致即拒。
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    with pytest.raises(StaleExecutionError):
        fence.begin_commit("exec_1", 7, DIGEST, current_job_generation=8)
    token = fence.begin_commit("exec_1", 7, DIGEST, current_job_generation=7)
    assert token.job_generation == 7


def test_wire_identity_round_trip() -> None:
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    token = fence.begin_commit("exec_1", 7, DIGEST)
    wire = token.to_wire()
    assert set(wire) == {"token_id", "exec_id", "job_generation", "candidate_digest"}
    # wire dict 与解析后的身份都可过围栏;时钟字段不过网。
    fence.validate_token(wire)
    fence.validate_token(CommitTokenIdentity.from_wire(wire))
    fence.publish_manifest(wire)
    fence.report_done(CommitTokenIdentity.from_wire(wire))
    assert fence.phase is CommitPhase.PROJECTED_DONE


def test_wire_identity_tamper_and_malformed_rejected() -> None:
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    token = fence.begin_commit("exec_1", 7, DIGEST)
    tampered = dict(token.to_wire(), candidate_digest="sha256:" + "f" * 64)
    with pytest.raises(TokenMismatchError):
        fence.validate_token(tampered)
    with pytest.raises(TokenMismatchError):
        CommitTokenIdentity.from_wire({"token_id": "x"})
    with pytest.raises(TokenMismatchError):
        CommitTokenIdentity.from_wire(dict(token.to_wire(), candidate_digest="md5:x"))
    with pytest.raises(TokenMismatchError):
        CommitTokenIdentity.from_wire(dict(token.to_wire(), job_generation="7"))


def test_token_identity_ignores_local_clock_fields() -> None:
    # 身份四元组一致即认;issued_at/ttl 是围栏本地事实,过期只按围栏侧保存的 token 判。
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    token = fence.begin_commit("exec_1", 7, DIGEST)
    clone = CommitToken(
        token_id=token.token_id,
        job_id=token.job_id,
        execution_step=token.execution_step,
        exec_id=token.exec_id,
        job_generation=token.job_generation,
        candidate_digest=token.candidate_digest,
        issued_at=token.issued_at + 10_000.0,
        ttl_sec=token.ttl_sec * 100,
    )
    fence.validate_token(clone)
    fence.publish_manifest(clone)


def test_mark_promote_started_lifecycle() -> None:
    clock = FakeClock()
    fence = make_fence(clock)
    fence.begin_execution("exec_1", 7)
    token = fence.begin_commit("exec_1", 7, DIGEST)
    assert fence.promote_started is False
    fence.mark_promote_started(token)
    assert fence.promote_started is True
    # 发布后不再是 promote 窗口。
    fence.publish_manifest(token)
    with pytest.raises(InvalidPhaseError):
        fence.mark_promote_started(token)
    fence.report_done(token)
    # 新执行/换代/新 commit 尝试都要重置该位。
    fence.supersede(8)
    assert fence.promote_started is False
    fence.begin_execution("exec_2", 8)
    stale = fence.begin_commit("exec_2", 8, DIGEST)
    fence.mark_promote_started(stale)
    clock.now += 61.0
    with pytest.raises(TokenExpiredError):
        fence.mark_promote_started(stale)
    fresh = fence.begin_commit("exec_2", 8, DIGEST)
    assert fence.promote_started is False
    fence.mark_promote_started(fresh)


def test_resolve_recovered_converges_without_token() -> None:
    # §2.7 行3:manifest 已发布、DB 未 done,恢复主体不持有 worker token。
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    token = fence.begin_commit("exec_1", 7, DIGEST)
    with pytest.raises(InvalidPhaseError):
        fence.resolve_recovered()
    fence.publish_manifest(token)
    fence.resolve_recovered()
    assert fence.phase is CommitPhase.PROJECTED_DONE
    assert fence.active_token is None
    # 迟到 worker 的回执在收敛后拒绝。
    with pytest.raises(TokenMismatchError):
        fence.report_done(token)


def test_in_flight_window_covers_manifest_published() -> None:
    # §2.6-5:manifest 已发布但 done 回执未收敛仍属在途;token TTL 保证等待有界。
    clock = FakeClock()
    fence = make_fence(clock)
    fence.begin_execution("exec_1", 7)
    token = fence.begin_commit("exec_1", 7, DIGEST)
    fence.publish_manifest(token)
    with pytest.raises(CommitInFlightError):
        fence.supersede(8)
    with pytest.raises(CommitInFlightError):
        fence.begin_execution("exec_2", 7)
    clock.now += 61.0
    fence.supersede(8)
    assert fence.phase is CommitPhase.IDLE


def test_in_flight_clears_after_report_done() -> None:
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    token = fence.begin_commit("exec_1", 7, DIGEST)
    fence.publish_manifest(token)
    fence.report_done(token)
    # 回执收敛后 token 已回收,rerun 无需等待 TTL。
    fence.supersede(8)
    assert fence.phase is CommitPhase.IDLE


def test_supersede_blocks_on_live_commit_then_fences_old_execution() -> None:
    clock = FakeClock()
    fence = make_fence(clock)
    fence.begin_execution("exec_1", 7)
    old_token = fence.begin_commit("exec_1", 7, DIGEST)
    # rerun 遇到在途有效 commit:等待或 409,不得一边换代一边放行旧提交。
    with pytest.raises(CommitInFlightError):
        fence.supersede(8)
    clock.now += 61.0
    fence.supersede(8)
    assert fence.phase is CommitPhase.IDLE
    assert fence.exec_id is None
    # 旧 generation 的 token 与回执全部拒绝。
    with pytest.raises(StaleExecutionError):
        fence.validate_token(old_token)
    with pytest.raises(StaleExecutionError):
        fence.begin_commit("exec_1", 7, DIGEST)
    with pytest.raises(StaleExecutionError):
        fence.begin_execution("exec_1", 7)
    # 新执行按新 generation 正常提交。
    fence.begin_execution("exec_2", 8)
    token = fence.begin_commit("exec_2", 8, DIGEST)
    fence.publish_manifest(token)
    fence.report_done(token)


def test_supersede_requires_strictly_newer_generation() -> None:
    fence = make_fence()
    fence.begin_execution("exec_1", 7)
    with pytest.raises(StaleExecutionError):
        fence.supersede(7)
    with pytest.raises(StaleExecutionError):
        fence.supersede(6)


def test_begin_execution_rules() -> None:
    clock = FakeClock()
    fence = make_fence(clock)
    fence.begin_execution("exec_1", 7)
    fence.begin_commit("exec_1", 7, DIGEST)
    # 在途有效 commit 不能被新执行顶掉。
    with pytest.raises(CommitInFlightError):
        fence.begin_execution("exec_2", 7)
    clock.now += 61.0
    # token 过期后同 generation 重派 attempt 合法,旧 token 随之作废。
    fence.begin_execution("exec_2", 7)
    assert fence.phase is CommitPhase.RUNNING
    assert fence.active_token is None
    with pytest.raises(StaleExecutionError):
        fence.begin_execution("exec_3", 6)
    with pytest.raises(StaleExecutionError):
        fence.begin_execution("", 7)


def test_fence_constructor_validates_identity() -> None:
    with pytest.raises(ManifestError):
        StepCommitFence(job_id="bad/id", execution_step="01_download")
    with pytest.raises(ValueError):
        StepCommitFence(job_id="jobs_live_001", execution_step="part:x::y::z")
    with pytest.raises(ValueError):
        StepCommitFence(
            job_id="jobs_live_001", execution_step="01_download", token_ttl_sec=0,
        )


# §2.7 恢复决策表


def facts(**overrides) -> RecoveryFacts:
    values = {
        "execution_current": True,
        "manifest_present": False,
        "manifest_valid": False,
        "promote_started": False,
        "db_terminal": False,
        "completion_effects_applied": False,
    }
    values.update(overrides)
    return RecoveryFacts(**values)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # 行1 staging 前/中崩溃:无 final manifest → 清 staging,step 回 waiting。
        (facts(), RecoveryAction.CLEAR_STAGING_RESET_WAITING),
        # 行2 promote 中崩溃:无 manifest 但 canonical 可能有半提交输出。
        (facts(promote_started=True), RecoveryAction.CLEAN_HALF_PROMOTED_AND_RETRY),
        # 行3 manifest 已发布、DB 未 done:修投影并重放 on_complete。
        (
            facts(manifest_present=True, manifest_valid=True),
            RecoveryAction.REPAIR_PROJECTION_AND_REPLAY_EFFECTS,
        ),
        # 行4 DB done 后、on_complete 前:幂等重放副作用。
        (
            facts(manifest_present=True, manifest_valid=True, db_terminal=True),
            RecoveryAction.REPLAY_COMPLETION_EFFECTS,
        ),
        # 行5 manifest 损坏/输出被改:step 与 DAG 下游回 waiting。
        (
            facts(manifest_present=True, db_terminal=True),
            RecoveryAction.INVALIDATE_STEP_AND_DOWNSTREAM,
        ),
        (facts(manifest_present=True), RecoveryAction.INVALIDATE_STEP_AND_DOWNSTREAM),
        # 行6 迟到 Worker:exec/generation/token 不匹配,只许清 staging。
        (facts(execution_current=False), RecoveryAction.REJECT_STALE_EXECUTION),
        (
            facts(execution_current=False, manifest_present=True, manifest_valid=True),
            RecoveryAction.REJECT_STALE_EXECUTION,
        ),
        # §2.1 对账映射:DB done 但 manifest 缺失 → 降 waiting 并失效下游。
        (facts(db_terminal=True), RecoveryAction.INVALIDATE_STEP_AND_DOWNSTREAM),
        # 状态自洽:无需动作。
        (
            facts(
                manifest_present=True, manifest_valid=True,
                db_terminal=True, completion_effects_applied=True,
            ),
            RecoveryAction.NO_ACTION,
        ),
    ],
)
def test_recovery_decision_table(given: RecoveryFacts, expected: RecoveryAction) -> None:
    assert decide_recovery(given) is expected


def test_recovery_execution_current_trivially_true_without_residue() -> None:
    # 无任何可归因的执行残留时 execution_current=True(没有"迟到者"可拒),
    # 决策落在其余行;REJECT 仅用于确有旧执行回执/staging/token 在场。
    assert decide_recovery(facts()) is RecoveryAction.CLEAR_STAGING_RESET_WAITING
    assert (
        decide_recovery(facts(manifest_present=True, manifest_valid=True))
        is RecoveryAction.REPAIR_PROJECTION_AND_REPLAY_EFFECTS
    )


def test_recovery_facts_reject_contradictions() -> None:
    with pytest.raises(ValueError):
        facts(manifest_present=False, manifest_valid=True)
    with pytest.raises(ValueError):
        facts(db_terminal=False, completion_effects_applied=True)
