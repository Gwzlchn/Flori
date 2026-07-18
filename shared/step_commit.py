"""step 产物提交围栏纯状态机:commit token 签发/校验/CAS 拒绝与崩溃恢复决策表。

设计稿 §2.6/§2.7:围栏保证只有当前 generation 的当前执行能把 staging 输出 promote
到 canonical 并发布 manifest。本模块不含 Redis/网络集成;中心端原子性由集成层用
Lua/Gateway 实现同一状态机语义,时间与 token 随机性在此可注入以便测试。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

from .step_manifest import validate_digest, validate_job_id
from .step_scope import parse_execution_step


DEFAULT_COMMIT_TOKEN_TTL_SEC = 600.0


class CommitFenceError(Exception):
    """围栏拒绝的统一基类;集成层据子类映射 409/重试/丢弃。"""


class StaleExecutionError(CommitFenceError):
    """generation 或 exec_id 已不是当前执行;迟到 Worker 一律拒绝。"""


class InvalidPhaseError(CommitFenceError):
    """当前阶段不允许该转换。"""


class LeaseInvalidError(CommitFenceError):
    """执行租约已失效,不得签发 commit token。"""


class TokenMismatchError(CommitFenceError):
    """token 不是当前唯一有效 token(伪造/已轮换/已消费)。"""


class TokenExpiredError(CommitFenceError):
    """token 超出 TTL;崩溃窗口交给恢复表,不允许继续提交。"""


class CommitInFlightError(CommitFenceError):
    """有效 commit 在途;rerun/rebuild/delete 必须等待或返回 409(设计稿 §2.6-5)。"""


class CommitPhase(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STAGING = "staging"
    COMMITTING = "committing"
    MANIFEST_PUBLISHED = "manifest_published"
    PROJECTED_DONE = "projected_done"


@dataclass(frozen=True)
class CommitTokenIdentity:
    """过网的 token 身份四元组。issued_at/ttl_sec 是围栏本地时钟事实,过期判定
    只信围栏侧,身份字段不携带 TTL,Worker 无法自行续命。"""
    token_id: str
    exec_id: str
    job_generation: int
    candidate_digest: str

    _WIRE_KEYS = ("token_id", "exec_id", "job_generation", "candidate_digest")

    @classmethod
    def from_wire(cls, data: object) -> "CommitTokenIdentity":
        """解析网络回传的身份;结构/类型不符一律 TokenMismatchError,不猜。"""
        if not isinstance(data, dict) or set(data) != set(cls._WIRE_KEYS):
            raise TokenMismatchError("wire token must carry exactly the identity quadruple")
        token_id = data["token_id"]
        exec_id = data["exec_id"]
        generation = data["job_generation"]
        if type(token_id) is not str or not token_id:
            raise TokenMismatchError("wire token_id must be a non-empty str")
        if type(exec_id) is not str or not exec_id:
            raise TokenMismatchError("wire exec_id must be a non-empty str")
        if type(generation) is not int or generation < 0:
            raise TokenMismatchError("wire job_generation must be int >= 0")
        try:
            validate_digest(data["candidate_digest"], "wire candidate_digest")
        except ValueError as exc:
            raise TokenMismatchError(str(exc)) from exc
        return cls(
            token_id=token_id,
            exec_id=exec_id,
            job_generation=generation,
            candidate_digest=data["candidate_digest"],
        )


@dataclass(frozen=True)
class CommitToken:
    """一次性提交凭据;promote/manifest 发布/done 回执全程携带同一身份。"""
    token_id: str
    job_id: str
    execution_step: str
    exec_id: str
    job_generation: int
    candidate_digest: str
    issued_at: float
    ttl_sec: float

    def expired(self, now: float) -> bool:
        return now >= self.issued_at + self.ttl_sec

    def identity(self) -> CommitTokenIdentity:
        return CommitTokenIdentity(
            token_id=self.token_id,
            exec_id=self.exec_id,
            job_generation=self.job_generation,
            candidate_digest=self.candidate_digest,
        )

    def to_wire(self) -> dict:
        """序列化过网身份;job_id/execution_step 由传输层路由携带,时钟字段不出围栏。"""
        return {
            "token_id": self.token_id,
            "exec_id": self.exec_id,
            "job_generation": self.job_generation,
            "candidate_digest": self.candidate_digest,
        }


class StepCommitFence:
    """单个 scope step 的提交围栏。

    状态链 running -> staging -> committing -> manifest_published -> projected_done;
    任何阶段的 CAS 失败都抛异常而不是静默忽略,让集成层显式选择拒绝语义。
    clock 默认单调钟,token TTL 只与流逝时间比较,不依赖挂钟。
    """

    def __init__(
        self,
        *,
        job_id: str,
        execution_step: str,
        token_ttl_sec: float = DEFAULT_COMMIT_TOKEN_TTL_SEC,
        clock: Callable[[], float] = time.monotonic,
        token_id_factory: Callable[[], str] | None = None,
    ):
        validate_job_id(job_id)
        parse_execution_step(execution_step)
        if not (token_ttl_sec > 0):
            raise ValueError("token_ttl_sec must be positive")
        self._job_id = job_id
        self._execution_step = execution_step
        self._token_ttl_sec = float(token_ttl_sec)
        self._clock = clock
        self._token_id_factory = token_id_factory or (lambda: secrets.token_hex(16))
        self._phase = CommitPhase.IDLE
        self._exec_id: str | None = None
        self._generation: int | None = None
        self._token: CommitToken | None = None
        self._promote_started = False

    @property
    def phase(self) -> CommitPhase:
        return self._phase

    @property
    def promote_started(self) -> bool:
        """当前 commit 尝试是否已开始向 canonical promote;喂 RecoveryFacts.promote_started。"""
        return self._promote_started

    @property
    def exec_id(self) -> str | None:
        return self._exec_id

    @property
    def generation(self) -> int | None:
        return self._generation

    @property
    def active_token(self) -> CommitToken | None:
        return self._token

    def _token_live(self) -> bool:
        return self._token is not None and not self._token.expired(self._clock())

    def _commit_in_flight(self) -> bool:
        # §2.6-5:有效 commit token 存续期间提交在途。MANIFEST_PUBLISHED 也算——
        # manifest 已发布但 done 回执未收敛,rerun 若此刻放行会与迟到回执竞态;
        # token TTL 保证等待有界,过期后走恢复表而不是继续提交。
        return (
            self._phase in (CommitPhase.COMMITTING, CommitPhase.MANIFEST_PUBLISHED)
            and self._token_live()
        )

    def _check_current(self, exec_id: str, generation: int) -> None:
        if type(exec_id) is not str or not exec_id:
            raise StaleExecutionError("exec_id must be a non-empty str")
        if type(generation) is not int:
            raise StaleExecutionError("generation must be int")
        if generation != self._generation or exec_id != self._exec_id:
            raise StaleExecutionError(
                f"not current execution: got gen={generation} exec={exec_id!r}, "
                f"current gen={self._generation} exec={self._exec_id!r}"
            )

    def begin_execution(self, exec_id: str, generation: int) -> None:
        """登记新的当前执行(调度派发)。同 generation 允许重试换 exec;旧 generation 拒绝。"""
        if type(exec_id) is not str or not exec_id:
            raise StaleExecutionError("exec_id must be a non-empty str")
        if type(generation) is not int or generation < 0:
            raise StaleExecutionError("generation must be int >= 0")
        if self._generation is not None and generation < self._generation:
            raise StaleExecutionError(
                f"generation {generation} is older than current {self._generation}"
            )
        # 在途有效 commit 不能被新执行顶掉,否则半提交输出/迟到回执无人负责(§2.6-5)。
        if self._commit_in_flight():
            raise CommitInFlightError("a live commit token exists; wait or reject")
        self._exec_id = exec_id
        self._generation = generation
        self._phase = CommitPhase.RUNNING
        self._token = None
        self._promote_started = False

    def mark_staged(self, exec_id: str, generation: int) -> None:
        """candidate 输出已进入 execution staging namespace。"""
        self._check_current(exec_id, generation)
        if self._phase is not CommitPhase.RUNNING:
            raise InvalidPhaseError(f"mark_staged requires running, got {self._phase.value}")
        self._phase = CommitPhase.STAGING

    def begin_commit(
        self,
        exec_id: str,
        generation: int,
        candidate_digest: str,
        *,
        lease_valid: bool = True,
        current_job_generation: int | None = None,
    ) -> CommitToken:
        """校验 generation/exec/运行态/租约后签发一次性 commit token。

        running 与 staging 都算执行存活(staging 是 running 的子阶段,集成层可不
        单独上报);committing 下仅当旧 token 已过期才允许同一执行重新签发。

        current_job_generation 是集成层在校验时刻实时读取的 job generation
        (Redis Lua 内 GET,同一原子块),不是 claim 时的快照:claim 后 rerun
        换代的窗口里,快照比对会放行旧执行。纯状态机内 self._generation 即 live
        值,该参数用于强制集成层把实时值传进来并 CAS。
        """
        self._check_current(exec_id, generation)
        if current_job_generation is not None and current_job_generation != generation:
            raise StaleExecutionError(
                f"live job generation {current_job_generation} != claimed {generation}"
            )
        if not lease_valid:
            raise LeaseInvalidError("execution lease is not valid")
        validate_digest(candidate_digest, "candidate_digest")
        if self._phase in (CommitPhase.RUNNING, CommitPhase.STAGING):
            pass
        elif self._phase is CommitPhase.COMMITTING and not self._token_live():
            pass  # 崩溃后的同执行重试:旧 token 过期即失去效力,可换新 token 重来。
        else:
            raise InvalidPhaseError(
                f"begin_commit not allowed in phase {self._phase.value}"
            )
        token = CommitToken(
            token_id=self._token_id_factory(),
            job_id=self._job_id,
            execution_step=self._execution_step,
            exec_id=exec_id,
            job_generation=generation,
            candidate_digest=candidate_digest,
            issued_at=self._clock(),
            ttl_sec=self._token_ttl_sec,
        )
        self._token = token
        self._phase = CommitPhase.COMMITTING
        self._promote_started = False
        return token

    def _require_active_token(
        self, token: object, allowed_phases: tuple[CommitPhase, ...],
    ) -> CommitToken:
        # 身份四元组比对而非整 dataclass 相等:wire 往返丢掉 issued_at/ttl_sec 等
        # 本地时钟字段,过期只按围栏侧保存的 token 判。
        if isinstance(token, CommitToken):
            identity = token.identity()
        elif isinstance(token, CommitTokenIdentity):
            identity = token
        elif isinstance(token, dict):
            identity = CommitTokenIdentity.from_wire(token)
        else:
            raise TokenMismatchError("not a commit token")
        active = self._token
        if active is None or identity != active.identity():
            # 身份对不上时:generation/exec 已换 = 迟到执行;否则 = 伪造或已被轮换。
            if identity.job_generation != self._generation or identity.exec_id != self._exec_id:
                raise StaleExecutionError("token belongs to a superseded execution")
            raise TokenMismatchError("token is not the active commit token")
        if active.expired(self._clock()):
            raise TokenExpiredError("commit token expired")
        if self._phase not in allowed_phases:
            raise InvalidPhaseError(
                f"phase {self._phase.value} does not accept this token operation"
            )
        return active

    def validate_token(self, token: object) -> None:
        """promote 前后逐次校验(设计稿 §2.6:每次 promote 前后都必须过围栏)。"""
        self._require_active_token(token, (CommitPhase.COMMITTING,))

    def mark_promote_started(self, token: object) -> None:
        """首次 canonical promote 前登记(committing 内子事实)。

        该位喂 §2.7 行 1/2 的区分事实(RecoveryFacts.promote_started);持久化
        证据(commit 记录落盘)由集成单元实现,状态机先提供转换与围栏校验。
        """
        self._require_active_token(token, (CommitPhase.COMMITTING,))
        self._promote_started = True

    def publish_manifest(self, token: object) -> None:
        """全部输出 read-back 通过后,原子发布 manifest 的围栏侧确认。"""
        self._require_active_token(token, (CommitPhase.COMMITTING,))
        self._phase = CommitPhase.MANIFEST_PUBLISHED

    def report_done(self, token: object) -> None:
        """Worker 用同一 token 上报 done;通过后 token 即回收,不接受第二次回执。"""
        self._require_active_token(token, (CommitPhase.MANIFEST_PUBLISHED,))
        self._phase = CommitPhase.PROJECTED_DONE
        self._token = None

    def resolve_recovered(self) -> None:
        """恢复主体免 token 收敛投影(§2.7 行 3:manifest 已发布、DB 未 done)。

        reconciler 依据的是已发布 manifest 这一持久事实,不持有 worker token;
        收敛后回收 token,迟到 worker 的 report_done 将被 TokenMismatch 拒绝。
        """
        if self._phase is not CommitPhase.MANIFEST_PUBLISHED:
            raise InvalidPhaseError(
                f"resolve_recovered requires manifest_published, got {self._phase.value}"
            )
        self._phase = CommitPhase.PROJECTED_DONE
        self._token = None

    def supersede(self, new_generation: int) -> None:
        """rerun/rebuild/delete 换代。在途有效 commit 抛 CommitInFlightError(对应 409)。"""
        if type(new_generation) is not int:
            raise StaleExecutionError("generation must be int")
        if self._generation is not None and new_generation <= self._generation:
            raise StaleExecutionError(
                f"supersede requires generation > {self._generation}, got {new_generation}"
            )
        if self._commit_in_flight():
            raise CommitInFlightError("a live commit token exists; wait or reject")
        self._generation = new_generation
        self._exec_id = None
        self._phase = CommitPhase.IDLE
        self._token = None
        self._promote_started = False


class RecoveryAction(Enum):
    """§2.7 六行崩溃恢复表的输出动作;NO_ACTION 表示状态自洽无需修复。"""
    CLEAR_STAGING_RESET_WAITING = "clear_staging_reset_waiting"
    CLEAN_HALF_PROMOTED_AND_RETRY = "clean_half_promoted_and_retry"
    REPAIR_PROJECTION_AND_REPLAY_EFFECTS = "repair_projection_and_replay_effects"
    REPLAY_COMPLETION_EFFECTS = "replay_completion_effects"
    INVALIDATE_STEP_AND_DOWNSTREAM = "invalidate_step_and_downstream"
    REJECT_STALE_EXECUTION = "reject_stale_execution"
    NO_ACTION = "no_action"


@dataclass(frozen=True)
class RecoveryFacts:
    """恢复决策的可见事实。全部来自可直接观察的持久状态,不含推测。

    - execution_current: 回执/残留所属 exec/generation/token 是否仍是当前执行。
      无任何执行残留可归因时该位平凡为 True(没有"迟到者"可拒),决策落在其余行;
      False 仅当确有旧执行的回执/staging/token 在场。
    - manifest_present/manifest_valid: final manifest 是否存在且 schema+输出校验通过;
    - promote_started: canonical 可能存在半提交输出(commit 记录显示 promote 已开始);
    - db_terminal: DB/Redis 投影为 done/skipped;
    - completion_effects_applied: on_complete 副作用已落地。
    """
    execution_current: bool
    manifest_present: bool
    manifest_valid: bool
    promote_started: bool
    db_terminal: bool
    completion_effects_applied: bool

    def __post_init__(self) -> None:
        if self.manifest_valid and not self.manifest_present:
            raise ValueError("manifest_valid requires manifest_present")
        if self.completion_effects_applied and not self.db_terminal:
            raise ValueError("completion effects run only after DB terminal state")


def decide_recovery(facts: RecoveryFacts) -> RecoveryAction:
    """纯函数实现 §2.7 恢复决策表;行序即优先级。

    "DB done 但 manifest 缺失"不在六行表内,按 §2.1 对账表映射为降 waiting
    并失效 DAG 下游(与 manifest 损坏同一动作)。
    """
    if not facts.execution_current:
        return RecoveryAction.REJECT_STALE_EXECUTION
    if facts.manifest_present:
        if not facts.manifest_valid:
            return RecoveryAction.INVALIDATE_STEP_AND_DOWNSTREAM
        if not facts.db_terminal:
            return RecoveryAction.REPAIR_PROJECTION_AND_REPLAY_EFFECTS
        if not facts.completion_effects_applied:
            return RecoveryAction.REPLAY_COMPLETION_EFFECTS
        return RecoveryAction.NO_ACTION
    if facts.db_terminal:
        return RecoveryAction.INVALIDATE_STEP_AND_DOWNSTREAM
    if facts.promote_started:
        return RecoveryAction.CLEAN_HALF_PROMOTED_AND_RETRY
    return RecoveryAction.CLEAR_STAGING_RESET_WAITING
