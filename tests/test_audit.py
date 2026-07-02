"""tests for shared/audit.py — 通用审计写入,经 structlog 输出到 Dozzle。"""

from structlog.testing import capture_logs

from shared.audit import audit


def test_audit_emits_structured_record():
    with capture_logs() as logs:
        audit("job", "jobs_article_x", "delete", actor="api",
              detail={"queue_tasks_removed": 3})
    rec = [r for r in logs if r.get("evt") == "audit"]
    assert len(rec) == 1
    r = rec[0]
    assert r["entity_type"] == "job"
    assert r["entity_id"] == "jobs_article_x"
    assert r["action"] == "delete"
    assert r["actor"] == "api"
    assert r["detail"] == {"queue_tasks_removed": 3}


def test_audit_defaults_actor_and_omits_empty_detail():
    with capture_logs() as logs:
        audit("collection", "col_1", "create")
    r = [x for x in logs if x.get("evt") == "audit"][0]
    assert r["actor"] == "api"
    assert "detail" not in r


def test_audit_extensible_entity_type():
    # 可扩展:任意 entity_type 都能记(加新实体无需改 schema)。
    with capture_logs() as logs:
        audit("knowledge_base", "deep-learning", "update")
    assert any(r.get("entity_type") == "knowledge_base" for r in logs)
