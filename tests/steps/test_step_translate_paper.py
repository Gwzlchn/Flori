"""steps/paper/step_04_translate_paper.py 的测试:论文翻译(非中文→中文译文)。"""

import json
from types import SimpleNamespace

from steps.paper.step_04_translate_paper import TranslatePaperStep
from tests.steps.conftest import make_step_config


def _setup(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["intermediate", "output", "logs"]:
        (job_dir / d).mkdir()
    sections = {
        "title": "AlpaServe", "authors": ["Z. Li"], "abstract": "Statistical multiplexing.",
        "sections": [{"level": 1, "title": "Introduction", "page": 1,
                      "text": "Model serving matters.", "children": []}],
        "total_sections": 1,
    }
    (job_dir / "intermediate" / "sections.json").write_text(json.dumps(sections))
    return job_dir


def test_validate_inputs_missing(tmp_path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "intermediate").mkdir()
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)
    assert step.validate_inputs() == ["output/original.md|intermediate/sections.json"]


def test_paper_markdown_includes_title_and_sections(tmp_path):
    job_dir = _setup(tmp_path)
    sections = json.loads((job_dir / "intermediate" / "sections.json").read_text())
    md = TranslatePaperStep._paper_markdown(sections)
    assert "# AlpaServe" in md
    assert "Introduction" in md
    assert "Model serving matters." in md


def test_execute_writes_translated(tmp_path, monkeypatch):
    job_dir = _setup(tmp_path)
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)
    cap: dict = {}
    monkeypatch.setattr(step, "call_ai",
                        lambda prompt, **k: cap.update(p=prompt) or "# AlpaServe\n\n## 引言\n模型服务很重要。")
    result = step.execute()
    assert result["chars"] > 0
    out = (job_dir / "output" / "translated.md").read_text(encoding="utf-8")
    assert "模型服务很重要" in out
    assert "Model serving matters." in cap["p"]      # prompt 用了论文原文


def test_execute_small_paper_single_chunk(tmp_path, monkeypatch):
    # 小论文 fits:单 chunk = 行为与整篇单调用一致。
    job_dir = _setup(tmp_path)
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)
    calls = []
    monkeypatch.setattr(step, "call_ai", lambda prompt, **k: calls.append(prompt) or "译文")
    result = step.execute()
    assert result["chunks"] == 1 and len(calls) == 1


def test_execute_large_paper_chunks(tmp_path, monkeypatch):
    # 大论文(> CHUNK_CHARS):按段落边界切成多 chunk,逐块调用、按序聚合。
    import json as _json
    from steps.paper.step_04_translate_paper import CHUNK_CHARS

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    for d in ["intermediate", "output", "logs"]:
        (job_dir / d).mkdir()
    secs = [{"level": 1, "title": f"Sec{i}", "page": i + 1,
             "text": f"S{i} " + ("word " * 2000), "children": []} for i in range(4)]
    (job_dir / "intermediate" / "sections.json").write_text(_json.dumps(
        {"title": "Big Paper", "authors": ["A"], "abstract": "Abs.",
         "sections": secs, "total_sections": 4}))

    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)
    md = step._paper_markdown(_json.loads((job_dir / "intermediate" / "sections.json").read_text()))
    assert len(md) > CHUNK_CHARS  # 前提:确实超预算

    calls = []
    monkeypatch.setattr(step, "call_ai",
                        lambda prompt, **k: calls.append(prompt) or f"译文块{len(calls)}")
    result = step.execute()
    assert result["chunks"] > 1
    assert len(calls) == result["chunks"]          # 每 chunk 一次调用
    out = (job_dir / "output" / "translated.md").read_text(encoding="utf-8")
    for i in range(1, result["chunks"] + 1):
        assert f"译文块{i}" in out                  # 按序聚合,块块都在
    assert out.index("译文块1") < out.index(f"译文块{result['chunks']}")


def test_paper_markdown_injects_figures_by_page(tmp_path):
    # 渲染图按页码插到对应顶级章节后:page2 的图归 Method(page2)节;图注成斜体行;无 filename 的跳过。
    sections = {
        "title": "T", "authors": [], "abstract": "",
        "sections": [
            {"level": 1, "title": "Intro", "page": 1, "text": "a", "children": []},
            {"level": 1, "title": "Method", "page": 2, "text": "b", "children": []},
        ],
    }
    figures = [
        {"id": "fig2", "page": 2, "caption": "Figure 2:  arch  diagram", "filename": "fig_02.png", "index": 0},
        {"id": "fig1", "page": 1, "caption": "Figure 1: overview", "filename": "fig_01.png", "index": 0},
        {"id": "fig0", "page": 1, "caption": "no render", "filename": None, "index": 1},
    ]
    md = TranslatePaperStep._paper_markdown(sections, figures)
    i_intro, i_method = md.find("## Intro"), md.find("## Method")
    i_f1, i_f2 = md.find("![](assets/fig_01.png)"), md.find("![](assets/fig_02.png)")
    assert i_intro < i_f1 < i_method < i_f2          # 各归其节
    assert "*Figure 2: arch diagram*" in md          # 图注斜体 + 空白折叠
    assert "no render" not in md                     # 未渲染图不注入

def test_paper_markdown_no_figures_unchanged(tmp_path):
    sections = {"title": "T", "authors": [], "abstract": "",
                "sections": [{"level": 1, "title": "S", "page": 1, "text": "x", "children": []}]}
    assert "![](" not in TranslatePaperStep._paper_markdown(sections, [])
    assert TranslatePaperStep._paper_markdown(sections) == TranslatePaperStep._paper_markdown(sections, [])

def test_prompt_contains_figure_preserve_rule(tmp_path):
    job_dir = _setup(tmp_path)
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)
    assert "![](assets/" in step._build_prompt("body")   # 保留图片引用规则进了 prompt


def test_prefers_original_md_as_source(tmp_path, monkeypatch):
    # arxiv-html:output/original.md(干净原文,图/公式已在原位)优先于 sections 组装。
    job_dir = _setup(tmp_path)
    (job_dir / "output").mkdir(exist_ok=True)
    (job_dir / "output" / "original.md").write_text(
        "# T\n\n$E=mc^2$\n\n![](assets/x1.png)", encoding="utf-8")
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)

    seen = {}
    def fake_call(prompt, **kw):
        seen["prompt"] = prompt
        return "译文"
    monkeypatch.setattr(step, "call_ai", fake_call)
    step.execute()
    assert "$E=mc^2$" in seen["prompt"]           # 干净原文直通
    assert "![](assets/x1.png)" in seen["prompt"]
    assert "AlpaServe" not in seen["prompt"]      # 未走 sections 组装
    h = step.input_hashes()
    assert "original" in h and "sections" not in h  # 指纹跟主源


def test_pdf_only_direct_translate(tmp_path, monkeypatch):
    # pdf-only:按页区间 chunk,每块 Read 直喂(allowed_tools/add_dirs/max_turns),聚合 translated.md。
    job_dir = _setup(tmp_path)
    (job_dir / "input").mkdir()
    (job_dir / "input" / "source.pdf").write_bytes(b"%PDF fake")
    (job_dir / "intermediate" / "parsed.json").write_text(json.dumps(
        {"source_kind": "pdf-only", "pages": 5}))
    (job_dir / "output").mkdir(exist_ok=True)
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)

    calls = []
    def fake_call(prompt, **kw):
        calls.append((prompt, kw))
        return f"块{len(calls)}译文"
    monkeypatch.setattr(step, "call_ai", fake_call)
    result = step.execute()

    assert result["mode"] == "pdf-direct"
    assert len(calls) == 3                                    # 5 页 / 每块 2 页 → 1-2,3-4,5-5
    p0, kw0 = calls[0]
    assert "第 1 页到第 2 页" in p0 and str(job_dir / "input" / "source.pdf") in p0
    assert kw0["allowed_tools"] == ["Read"]
    assert kw0["add_dirs"] == [str((job_dir / "input").resolve())]
    assert kw0["max_turns"] == 2 * 2 + 4
    p2, kw2 = calls[2]
    assert "第 5 页到第 5 页" in p2 and kw2["max_turns"] == 1 * 2 + 4
    out = (job_dir / "output" / "translated.md").read_text()
    assert out == "块1译文\n\n块2译文\n\n块3译文"


def test_pdf_only_without_pages_fails_loud(tmp_path, monkeypatch):
    from shared.errors import InputInvalidError
    import pytest
    job_dir = _setup(tmp_path)
    (job_dir / "input").mkdir()
    (job_dir / "intermediate" / "parsed.json").write_text(json.dumps(
        {"source_kind": "pdf-only"}))
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)
    with pytest.raises(InputInvalidError):
        step.execute()


def test_pdf_only_figure_placeholders_become_jump_links(tmp_path, monkeypatch):
    # 带页码占位 → 追加 [查看原图(原文第 p 页)](#pdf-page=p) 跳原文链接(前端切 tab+iframe #page 跳页);
    # 不再渲染整页图插正文(A4 整页截图切碎阅读流,线上 101 Alphas 实证不可读)。
    # 越界页码不加链接;旧格式【图 N】(无 |页码)原样不动。
    job_dir = _setup(tmp_path)
    (job_dir / "input").mkdir()
    (job_dir / "input" / "source.pdf").write_bytes(b"%PDF fake")
    (job_dir / "intermediate" / "parsed.json").write_text(json.dumps(
        {"source_kind": "pdf-only", "pages": 4}))
    (job_dir / "output").mkdir(exist_ok=True)
    config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
    step = TranslatePaperStep("04_translate_paper", job_dir, config)

    translated = ("正文…\n【图 1|第 2 页】执行概览\n更多\n"
                  "【表 2|第 2 页】配置对比\n【图 3|第 9 页】越界忽略\n"
                  "【图 4】旧格式无页码")
    _n = {"i": 0}
    def _fake_ai(*a, **k):
        _n["i"] += 1
        return translated if _n["i"] == 1 else "尾块正文"
    monkeypatch.setattr(step, "call_ai", _fake_ai)
    def _no_subprocess(*a, **k):
        raise AssertionError("不应再调 pdftoppm 渲染整页图")
    monkeypatch.setattr(step, "run_subprocess", _no_subprocess)

    result = step.execute()
    out = (job_dir / "output" / "translated.md").read_text()
    assert "【图 1|第 2 页】执行概览  [查看原图(原文第 2 页)](#pdf-page=2)" in out
    assert "【表 2|第 2 页】配置对比  [查看原图(原文第 2 页)](#pdf-page=2)" in out
    assert "#pdf-page=9" not in out                       # 越界不加链接
    assert "【图 4】旧格式无页码" in out and "pdf-page.png" not in out
    assert result["figure_pages"] == 2                    # 加链接的占位数


class TestTermConsistency:
    """术语一致性(工单 26-07-06/04 §5 V2):chunk 注入 term_map 命中 + 回收滚动 + term_pairs 落盘。"""

    def _big_job(self, tmp_path, term_map=None):
        import json as _json
        from steps.paper.step_04_translate_paper import CHUNK_CHARS
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        for d in ["intermediate", "output", "logs", "input"]:
            (job_dir / d).mkdir()
        # 两个大节 → 必然 2+ chunk;两节都含 martingale(共享术语)。
        secs = [{"level": 1, "title": f"Sec{i}", "page": i + 1,
                 "text": f"martingale property {i} " + ("word " * 4000), "children": []}
                for i in range(2)]
        (job_dir / "intermediate" / "sections.json").write_text(_json.dumps(
            {"title": "T", "authors": [], "abstract": "", "sections": secs, "total_sections": 2}))
        if term_map is not None:
            (job_dir / "input" / "term_map.json").write_text(
                _json.dumps(term_map, ensure_ascii=False))
        return job_dir

    def test_l1_map_injected_into_every_chunk(self, tmp_path, monkeypatch):
        job_dir = self._big_job(tmp_path, term_map={"martingale": "鞅"})
        config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
        step = TranslatePaperStep("04_translate_paper", job_dir, config)
        prompts = []
        monkeypatch.setattr(step, "call_ai", lambda p, **k: prompts.append(p) or "译文")
        result = step.execute()
        assert result["chunks"] >= 2
        # 命中才注入:含 martingale 的 chunk 注入同一对照;纯 filler 块无术语段(命中过滤)。
        with_term = [p for p in prompts if "martingale" in p.split("--- 论文原文 ---")[-1]]
        assert len(with_term) >= 2
        for p in with_term:
            assert "martingale → 鞅" in p and "术语对照表" in p

    def test_l3_rolls_from_first_chunk_and_lands_in_pairs(self, tmp_path, monkeypatch):
        job_dir = self._big_job(tmp_path, term_map=None)   # 无 L1:首 chunk 译文定名
        config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
        step = TranslatePaperStep("04_translate_paper", job_dir, config)
        prompts = []

        def fake_ai(p, **k):
            prompts.append(p)
            if len(prompts) == 1:               # 首 chunk 产出「鞅(martingale)」×2(复现验证)
                return "首段:鞅（martingale），鞅无处不在"
            return "后续译文"

        monkeypatch.setattr(step, "call_ai", fake_ai)
        result = step.execute()
        assert result["new_terms"] == 1
        assert "martingale → 鞅" in prompts[1]  # 第二 chunk 注入首 chunk 回收的对照
        import json as _json
        pairs = _json.loads((job_dir / "output" / "term_pairs.json").read_text())
        assert pairs == {"martingale": "鞅"}

    def test_no_map_no_terms_block(self, tmp_path, monkeypatch):
        job_dir = self._big_job(tmp_path, term_map=None)
        config = make_step_config(tmp_path, step_name="04_translate_paper", pool="ai")
        step = TranslatePaperStep("04_translate_paper", job_dir, config)
        prompts = []
        monkeypatch.setattr(step, "call_ai", lambda p, **k: prompts.append(p) or "译文")
        step.execute()
        assert all("术语对照表" not in p for p in prompts)   # 空表 prompt 无痕
        assert not (job_dir / "output" / "term_pairs.json").exists()
