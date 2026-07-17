"""prompts 领域的显式数据库边界。"""

from __future__ import annotations

from .seams import db as _db

from ..db import (
    PROMPT_VERSION_MAX,
    PromptVersionExhaustedError,
    _now_iso,
    _valid_prompt_version,
)


class PromptsRepository:
    """由 Database façade 调用，不持有独立连接或锁。"""

    def list_prompt_override_versions(
        self, scope: str, domain: str | None, pipeline: str, step: str,
        document_kind: str | None = None,
    ) -> list[dict]:
        """该 (scope,domain,pipeline,step) 的全部历史版本元信息(不含 content),version 升序。"""
        scope, dom = self._norm_override_key(scope, domain)
        with self._lock:
            rows = self._conn.execute(
                "SELECT version, note, created_at FROM prompt_override_versions "
                "WHERE scope=? AND domain=? AND pipeline=? AND document_kind=? "
                "AND step=? ORDER BY version",
                (scope, dom, pipeline, document_kind or "", step),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_prompt_override_version(
        self, scope: str, domain: str | None, pipeline: str, step: str, version: int,
        document_kind: str | None = None,
    ) -> dict | None:
        """读某历史版本(含 content),未命中返回 None。"""
        if not _valid_prompt_version(version):
            return None
        scope, dom = self._norm_override_key(scope, domain)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM prompt_override_versions WHERE scope=? AND domain=? "
                "AND pipeline=? AND document_kind=? AND step=? AND version=?",
                (scope, dom, pipeline, document_kind or "", step, version),
            ).fetchone()
        return dict(row) if row else None

    def get_prompt_override(
        self, scope: str, domain: str | None, pipeline: str, step: str,
        document_kind: str | None = None,
    ) -> dict | None:
        """读单条 prompt 覆盖,未命中返回 None。"""
        scope, dom = self._norm_override_key(scope, domain)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM prompt_overrides WHERE scope=? AND domain=? AND pipeline=? "
                "AND document_kind=? AND step=?",
                (scope, dom, pipeline, document_kind or "", step),
            ).fetchone()
        return dict(row) if row else None

    def list_prompt_overrides(self) -> list[dict]:
        """全量 prompt 覆盖(供设置页标记哪些步已有覆盖)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM prompt_overrides "
                "ORDER BY pipeline, document_kind, step, scope, domain"
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_prompt_overrides(
        self, pipeline: str, domain: str | None, document_kind: str | None = None,
    ) -> dict[str, dict]:
        """派发注入用:给定 job 的 pipeline + domain,返回 {step: {content, version}} 解析结果。
        domain 覆盖优先于 global;同一步两者都有则取 domain(连同其版本号)。job 创建时(api 有 DB)
        调用,结果写 job.json.prompt_overrides 随 job 下发(含激活版本号快照),worker step_base 读取
        (pure worker 无 DB)。空 content 视为无覆盖被过滤。
        注:worker _injected_prompt_override 兼容 dict 与存量纯字符串两种 job.json 形态。"""
        dom = (domain or "").strip()
        resolved: dict[str, dict] = {}
        with self._lock:
            rows = self._conn.execute(
                "SELECT scope, domain, document_kind, step, content, version "
                "FROM prompt_overrides WHERE pipeline=? AND document_kind IN ('', ?) "
                "AND (scope='global' OR (scope='domain' AND domain=?))",
                (pipeline, document_kind or "", dom),
            ).fetchall()
        ranked = sorted(
            rows,
            key=lambda row: (
                2 if row["document_kind"] else 0,
                1 if row["scope"] == "domain" else 0,
            ),
        )
        for row in ranked:
            resolved[row["step"]] = {
                "content": row["content"], "version": row["version"],
                "document_kind": row["document_kind"] or None,
                "scope": row["scope"],
            }
        return {k: v for k, v in resolved.items() if (v.get("content") or "").strip()}

    @staticmethod
    def _norm_override_key(scope: str, domain: str | None) -> tuple[str, str]:
        """归一 (scope, domain):scope 非 'domain' 一律按 'global' 处理且 domain='';
        'domain' scope 须有非空 domain。返回 (scope, domain) 供主键统一(避免 NULL 破唯一)。"""
        if scope == "domain" and (domain or "").strip():
            return "domain", domain.strip()
        return "global", ""

    def set_prompt_override_in_tx(
        self,
        connection,
        scope: str,
        domain: str | None,
        pipeline: str,
        step: str,
        content: str,
        mode: str = "overwrite",
        note: str | None = None,
        document_kind: str | None = None,
    ) -> int:
        """存某步的 prompt 覆盖,带版本管理(类 Grafana save)。返回激活版本号。
        - 该 (scope,domain,pipeline,step) 此前无任何覆盖 → 首版 v1(mode 忽略)。
        - mode='overwrite'(默认)→ 更新当前激活版本历史行的 content(+note,留空则保留原 note),
          主表 content/version 不变(version 仍指激活版本)。
        - mode='new' → 新版本 version=max(历史)+1,历史表插一条,主表指向新版本(成为激活)。
        content 不做空判断(空判断/删除由上层 delete_prompt_override 负责)。"""
        scope, dom = self._norm_override_key(scope, domain)
        key = (scope, dom, pipeline, document_kind or "", step)
        now = _db._now_iso()
        cur = connection.execute(
            "SELECT version FROM prompt_overrides WHERE scope=? AND domain=? "
            "AND pipeline=? AND document_kind=? AND step=?",
            key,
        ).fetchone()
        maxv = connection.execute(
            "SELECT COALESCE(MAX(version),0) FROM prompt_override_versions "
            "WHERE scope=? AND domain=? AND pipeline=? AND document_kind=? AND step=?",
            key,
        ).fetchone()[0]
        if cur is None and maxv == 0:
            version = 1                          # 首版
        elif mode == "new":
            if maxv >= PROMPT_VERSION_MAX:
                raise PromptVersionExhaustedError(
                    "prompt version reached SQLite INTEGER limit"
                )
            version = maxv + 1                   # 另存为新版本
        else:                                    # overwrite 当前激活版本
            version = cur["version"] if cur else (maxv or 1)
        # 历史行:overwrite 保留原 created_at/note(note 给定才覆盖);new/首版用 now。
        prev = connection.execute(
            "SELECT created_at, note FROM prompt_override_versions WHERE scope=? "
            "AND domain=? AND pipeline=? AND document_kind=? AND step=? AND version=?",
            (*key, version),
        ).fetchone()
        created_at = prev["created_at"] if prev else now
        eff_note = note if note is not None else (prev["note"] if prev else "")
        connection.execute(
            """INSERT OR REPLACE INTO prompt_override_versions
               (scope, domain, pipeline, document_kind, step, version, content, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (*key, version, content or "", eff_note or "", created_at),
        )
        connection.execute(
            """INSERT OR REPLACE INTO prompt_overrides
               (scope, domain, pipeline, document_kind, step, content, version, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (*key, content or "", version, now),
        )
        return version

    def delete_prompt_override_in_tx(
        self, connection, scope: str, domain: str | None, pipeline: str, step: str,
        document_kind: str | None = None,
    ) -> None:
        """删某步的 prompt 覆盖(恢复默认)——连同其全部历史版本一并删。无则 no-op。"""
        scope, dom = self._norm_override_key(scope, domain)
        connection.execute(
            "DELETE FROM prompt_overrides WHERE scope=? AND domain=? AND pipeline=? "
            "AND document_kind=? AND step=?",
            (scope, dom, pipeline, document_kind or "", step),
        )
        connection.execute(
            "DELETE FROM prompt_override_versions WHERE scope=? AND domain=? "
            "AND pipeline=? AND document_kind=? AND step=?",
            (scope, dom, pipeline, document_kind or "", step),
        )

    def deactivate_prompt_override_in_tx(
        self, connection, scope: str, domain: str | None, pipeline: str, step: str,
        document_kind: str | None = None,
    ) -> None:
        """停用某步覆盖(恢复内置默认)——非破坏:只删主表 prompt_overrides 那一行(激活指针),
        prompt_override_versions 全部历史版本完整保留(下拉里仍能看到 v1/v2…,可重新激活)。
        删指针后 resolve_prompt_overrides 返回空 → 派发回内置默认。无指针则 no-op。
        注:version 列 NOT NULL 不可空,故用删激活行而非置 NULL 表达停用。"""
        scope, dom = self._norm_override_key(scope, domain)
        connection.execute(
            "DELETE FROM prompt_overrides WHERE scope=? AND domain=? AND pipeline=? "
            "AND document_kind=? AND step=?",
            (scope, dom, pipeline, document_kind or "", step),
        )

    def set_active_prompt_version_in_tx(
        self, connection, scope: str, domain: str | None, pipeline: str, step: str,
        version: int, document_kind: str | None = None,
    ) -> bool:
        """把激活指针指向某历史版本(re-activate):主表 content/version 同步成该版本,
        下次派发即用它。该版本不存在于 prompt_override_versions → 返回 False(不动);成功 True。
        主表此前可能无行(已 deactivate 状态)——直接 INSERT OR REPLACE 重建激活指针。"""
        if not _valid_prompt_version(version):
            return False
        scope, dom = self._norm_override_key(scope, domain)
        key = (scope, dom, pipeline, document_kind or "", step)
        row = connection.execute(
            "SELECT content FROM prompt_override_versions WHERE scope=? AND domain=? "
            "AND pipeline=? AND document_kind=? AND step=? AND version=?",
            (*key, version),
        ).fetchone()
        if row is None:
            return False
        connection.execute(
            """INSERT OR REPLACE INTO prompt_overrides
               (scope, domain, pipeline, document_kind, step, content, version, updated_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (*key, row["content"], version, _db._now_iso()),
        )
        return True
