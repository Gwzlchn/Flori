"""通用审计:结构化记录实体的增/删/改,经 structlog→容器 stdout→Dozzle 查看。

设计:
- 可扩展:加新实体只需用新的 entity_type 调本函数,无需建表/改 schema。
- 不建表、不建前端页、不接 events:system,审计统一进现有日志查看系统 Dozzle。
- best-effort:审计写入绝不因异常反过来影响主业务流程。
"""
from __future__ import annotations

import structlog

_log = structlog.get_logger("audit")

# 当前覆盖的实体(可扩展):job / collection / knowledge_base。新实体直接传新字符串即可。
# 动作固定为增删改三类,读操作不审计。
Action = str  # "create" | "update" | "delete"


def audit(
    entity_type: str,
    entity_id: str,
    action: Action,
    actor: str | None = None,
    detail: dict | None = None,
) -> None:
    """记一条审计日志(evt=audit)。

    entity_type: 实体类型,如 "job" / "collection" / "knowledge_base"(可扩展)。
    entity_id:   实体主键。
    action:      "create" / "update" / "delete"。
    actor:       发起来源,如 "api" / "subscription" / "cli" / "scheduler"(默认 api)。
    detail:      额外细节 dict(如删除时"清了什么":队列条数/产物/ai_usage 等)。
    """
    try:
        fields = {
            "evt": "audit",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "action": action,
            "actor": actor or "api",
        }
        if detail:
            fields["detail"] = detail
        _log.info("audit", **fields)
    except Exception:
        # 审计是旁路,绝不反噬主流程。
        pass
