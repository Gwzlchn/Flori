"""scripts/backfill_zh_names.py:导出过滤/批校验/apply 幂等(claude 调用全 mock)。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import backfill_zh_names as bf  # noqa: E402

from tests.current_schema_db import clone_current_schema_database  # noqa: E402


@pytest.fixture
def db(tmp_path, current_schema_db_template):
    d = clone_current_schema_database(current_schema_db_template, tmp_path / "t.db")
    d.add_glossary_suggestion("finance", "Kelly criterion", "j1", definition="凯利准则,最优下注。")
    d.add_glossary_suggestion("finance", "martingale", "j1", definition="一种随机过程")
    d.add_glossary_suggestion("finance", "named already", "j1", zh_name="已有译名")
    return d


class TestExport:
    def test_skips_rows_with_zh_name(self, db, tmp_path):
        out = tmp_path / "todo.json"
        dbfile = db._conn.execute("PRAGMA database_list").fetchone()["file"]
        args = type("A", (), {"out": str(out), "db": dbfile})
        bf.cmd_export(args)
        todo = json.loads(out.read_text())
        terms = {t["term"] for t in todo}
        assert "named already" not in terms          # 已有 zh_name 跳过(幂等)
        assert {"Kelly criterion", "martingale"} <= terms


class TestTranslateValidation:
    BATCH = [{"domain": "finance", "term": "alpha", "definition": ""},
             {"domain": "finance", "term": "beta", "definition": ""}]

    def _cli(self, result_obj):
        out = json.dumps({"result": json.dumps(result_obj, ensure_ascii=False)})
        return type("R", (), {"returncode": 0, "stdout": out.encode(), "stderr": b""})

    def test_key_mismatch_rejected(self):
        with patch.object(bf.subprocess, "run", return_value=self._cli({"alpha": "阿尔法"})):
            assert bf._translate_batch(self.BATCH) is None    # 缺 beta → 整批弃用

    def test_valid_batch_accepted(self):
        with patch.object(bf.subprocess, "run",
                          return_value=self._cli({"alpha": "阿尔法", "beta": None})):
            assert bf._translate_batch(self.BATCH) == {"alpha": "阿尔法", "beta": None}

    def test_non_json_rejected(self):
        r = type("R", (), {"returncode": 0, "stdout": b"not json", "stderr": b""})
        with patch.object(bf.subprocess, "run", return_value=r):
            assert bf._translate_batch(self.BATCH) is None


class TestApplyIdempotent:
    def test_apply_only_fills_empty(self, db, tmp_path):
        dbfile = db._conn.execute("PRAGMA database_list").fetchone()["file"]
        m = tmp_path / "zh.json"
        m.write_text(json.dumps({
            "Kelly criterion": {"domain": "finance", "zh_name": "凯利准则"},
            "named already": {"domain": "finance", "zh_name": "试图覆盖"},
            "missing term": {"domain": "finance", "zh_name": "不存在"},
        }, ensure_ascii=False))
        args = type("A", (), {"map": str(m), "db": dbfile})
        bf.cmd_apply(args)
        assert db.get_glossary_term("finance", "Kelly criterion")["zh_name"] == "凯利准则"
        assert db.get_glossary_term("finance", "named already")["zh_name"] == "已有译名"  # 不覆盖
        # 重跑幂等不炸
        bf.cmd_apply(args)
