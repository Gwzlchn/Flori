use std::fs;

use super::*;
use crate::{
    ai::tests::{Fixture, job_spec},
    pipeline::{AiTool, Pipeline, StepExit, TranslateMode},
};

#[test]
fn title_cleaning_vectors() {
    let long_zh = "大模型推理加速全景:从 KV Cache、PagedAttention、投机解码到量化与算子融合的工程实践与踩坑记录(上篇)";
    let cases = [
        ("Attention Is All You Need", "Attention Is All You Need"),
        (
            "DeepSeek-V2: A Strong, Economical, and Efficient Mixture-of-Experts Language Model",
            "DeepSeek-V2 A Strong, Economical, and Efficient Mixture-of-Experts Language Model",
        ),
        (
            "【官方双语】GPT是什么?直观解释Transformer | 深度学习第5章",
            "【官方双语】GPT是什么直观解释Transformer 深度学习第5章",
        ),
        (
            "【ゆっくり解説】量子コンピュータとは何か?",
            "【ゆっくり解説】量子コンピュータとは何か",
        ),
        (
            "[ICML 2024] Scaling Laws for Fine-Grained Mixture of Experts",
            "ICML 2024 Scaling Laws for Fine-Grained Mixture of Experts",
        ),
        (
            "C++ / Rust: <Ownership> vs. *Smart* Pointers",
            "C++ Rust Ownership vs. Smart Pointers",
        ),
        (
            "Mamba:\tLinear-Time Sequence Modeling\nwith Selective State Spaces\u{7}",
            "Mamba Linear-Time Sequence Modeling with Selective State Spaces",
        ),
        // Decomposed input (macOS clipboards) is composed first.
        (
            "Schro\u{308}dinger's \"Cat\" Explained",
            "Schrödinger's Cat Explained",
        ),
        (
            "这个AI太强了!#ChatGPT #人工智能 ^_^ 🤖",
            "这个AI太强了!ChatGPT 人工智能 _ 🤖",
        ),
        // A leading dot would hide the note in Obsidian.
        ("  .NET 9 性能优化... ", "NET 9 性能优化"),
    ];
    for (title, want) in cases {
        assert_eq!(clean_title(title), want, "{title:?}");
    }
    // 100 bytes at most, never splitting a character: 33 CJK characters are 99 bytes.
    let cleaned = clean_title(long_zh);
    assert!(cleaned.len() <= TITLE_BYTES, "{}", cleaned.len());
    assert!(long_zh.replace(':', "").starts_with(&cleaned));
    assert!(cleaned.len() > TITLE_BYTES - 4);
    assert_eq!(clean_title(&"a".repeat(150)).len(), 100);
}

#[test]
fn note_names_never_take_a_neighbour_s_name() {
    let cases = [
        ("transcript", "transcript 笔记.md"),
        ("Transcript", "Transcript 笔记.md"),
        ("original", "original 笔记.md"),
        ("TRANSLATION", "TRANSLATION 笔记.md"),
        ("assets", "assets 笔记.md"),
        ("Transcript of a talk", "Transcript of a talk.md"),
        ("???", "BV1GJ411x7h7.md"),
    ];
    for (title, want) in cases {
        assert_eq!(note_file(title, "BV1GJ411x7h7"), want, "{title}");
    }
}

#[test]
fn a_video_titled_transcript_keeps_both_files() {
    let fx = Fixture::new(Pipeline::Video, AiTool::Claude);
    fx.step(1, StepName::VideoFetch, Some("Transcript"), &[], &[], &[]);
    fx.step(2, StepName::VideoTranscribe, None, &[], &[], &[]);
    let frames = [("transcript", "transcript.md", "转写稿\n")];
    fx.step(3, StepName::VideoFrames, None, &frames, &[], &[]);
    fx.step(
        4,
        StepName::Note,
        None,
        &[("note", "note.md", "笔记\n")],
        &[],
        &[],
    );
    fx.step(5, StepName::Translate, None, &[], &[], &[]);
    let vault = tempfile::tempdir().unwrap();
    publish_into(&fx, vault.path()).unwrap();
    let folder = vault
        .path()
        .join("flori/ml/unsorted/2026-11-08-yt-dQw4w9WgXcQ");
    assert_eq!(
        fs::read_to_string(folder.join("transcript.md")).unwrap(),
        "转写稿\n"
    );
    let note = fs::read_to_string(folder.join("Transcript 笔记.md")).unwrap();
    assert!(note.starts_with("---\ntitle: \"Transcript\"\n") && note.ends_with("笔记\n"));
    let manifest = pipeline::read_manifest(&fx.job_dir().join("steps/05-publish.tmp")).unwrap();
    assert_eq!(
        manifest.meta["note_file"],
        MetaValue::Text("Transcript 笔记.md".into())
    );
}

fn arxiv_job(fx: &Fixture, translated: bool) {
    fx.step(
        1,
        StepName::ArxivFetch,
        Some("Mixtral of Experts: Sparse MoE"),
        &[("page", "page.html", "<html>")],
        &[],
        &[],
    );
    fx.step(
        2,
        StepName::ArxivConvert,
        None,
        &[("original", "original.md", "# Mixtral\n")],
        &["fig1.png"],
        &[],
    );
    fx.step(
        3,
        StepName::Note,
        None,
        &[("note", "note.md", "## 一句话总结\n\n稀疏专家。\n")],
        &[],
        &[],
    );
    if translated {
        let meta = [
            ("language", MetaValue::Text("en".into())),
            ("translated", MetaValue::Bool(true)),
        ];
        fx.step(
            4,
            StepName::Translate,
            None,
            &[("translation", "translation.md", "# 混合专家\n")],
            &[],
            &meta,
        );
    } else {
        let meta = [
            ("language", MetaValue::Text("zh".into())),
            ("translated", MetaValue::Bool(false)),
            ("skipped_reason", MetaValue::Text("chinese".into())),
        ];
        fx.step(4, StepName::Translate, None, &[], &[], &meta);
    }
}

fn publish_into(fx: &Fixture, vault: &Path) -> StepResult {
    let out = fx.out_dir("05-publish");
    publish(fx.job_dir(), &out, vault)
}

const FOLDER: &str = "flori/ml/unsorted/2026-11-08-arxiv-2401.04088";

#[test]
fn arxiv_publish_writes_the_folder_and_frontmatter() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    arxiv_job(&fx, true);
    let vault = tempfile::tempdir().unwrap();
    publish_into(&fx, vault.path()).unwrap();
    let folder = vault.path().join(FOLDER);
    let mut names: Vec<String> = fs::read_dir(&folder)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    names.sort();
    assert_eq!(
        names,
        [
            "Mixtral of Experts Sparse MoE.md",
            "assets",
            "original.md",
            "translation.md"
        ]
    );
    assert_eq!(
        fs::read_to_string(folder.join("assets/fig1.png")).unwrap(),
        "image fig1.png"
    );
    assert_eq!(
        fs::read_to_string(folder.join("translation.md")).unwrap(),
        "# 混合专家\n"
    );
    let note = fs::read_to_string(folder.join("Mixtral of Experts Sparse MoE.md")).unwrap();
    let want = r#"---
title: "Mixtral of Experts: Sparse MoE"
aliases: ["Mixtral of Experts: Sparse MoE"]
source_url: "https://arxiv.org/abs/2401.04088"
platform: "arxiv"
source_id: "2401.04088"
domain: "ml"
collection: ""
created: "2026-11-08T10:00:00+08:00"
status: "unread"
language: "en"
translated: true
tags: ["flori","arxiv","ml"]
job_url: "http://nas.lan:8780/jobs/12"
ai_tool: "claude"
ai_model: "model-note"
---

## 一句话总结

稀疏专家。
"#;
    assert_eq!(note, want);
    let manifest = pipeline::read_manifest(&fx.job_dir().join("steps/05-publish.tmp")).unwrap();
    assert_eq!(manifest.meta["vault_path"], MetaValue::Text(FOLDER.into()));
    assert_eq!(
        manifest.meta["note_file"],
        MetaValue::Text("Mixtral of Experts Sparse MoE.md".into())
    );
    assert!(manifest.files.is_empty() && manifest.assets.is_empty() && manifest.title.is_none());
    assert!(!fx.job_dir().join("publish.tmp").exists());
}

#[test]
fn republishing_the_same_job_is_a_no_op_and_others_get_20() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    arxiv_job(&fx, true);
    let vault = tempfile::tempdir().unwrap();
    publish_into(&fx, vault.path()).unwrap();
    let note = vault
        .path()
        .join(FOLDER)
        .join("Mixtral of Experts Sparse MoE.md");
    let before = fs::read_to_string(&note).unwrap();

    // A crash after rename: retry finds its own folder, leaves it alone, writes the manifest.
    fs::create_dir_all(fx.job_dir().join("publish.tmp/assets")).unwrap();
    publish_into(&fx, vault.path()).unwrap();
    assert_eq!(fs::read_to_string(&note).unwrap(), before);
    assert!(!fx.job_dir().join("publish.tmp").exists());
    let manifest = pipeline::read_manifest(&fx.job_dir().join("steps/05-publish.tmp")).unwrap();
    assert_eq!(manifest.meta["vault_path"], MetaValue::Text(FOLDER.into()));

    // Another job for the same source in the same place must not touch it.
    let other = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let mut job = job_spec(Pipeline::Arxiv, AiTool::Claude);
    job.job_id = 13;
    other.write_job(&job);
    arxiv_job(&other, true);
    let failure = publish_into(&other, vault.path()).unwrap_err();
    assert_eq!(failure.exit, StepExit::Permanent);
    assert!(
        failure.message.contains("already exists"),
        "{}",
        failure.message
    );
    assert_eq!(fs::read_to_string(&note).unwrap(), before);
}

#[test]
fn clashing_assets_are_permanent() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    fx.step(1, StepName::ArxivFetch, Some("T"), &[], &[], &[]);
    fx.step(
        2,
        StepName::ArxivConvert,
        None,
        &[("original", "original.md", "x")],
        &["fig1.png"],
        &[],
    );
    fx.step(
        3,
        StepName::Note,
        None,
        &[("note", "note.md", "n")],
        &["fig1.png"],
        &[],
    );
    fx.step(4, StepName::Translate, None, &[], &[], &[]);
    let vault = tempfile::tempdir().unwrap();
    let failure = publish_into(&fx, vault.path()).unwrap_err();
    assert_eq!(failure.exit, StepExit::Permanent);
    assert!(failure.message.contains("fig1.png"));
    assert_eq!(fs::read_dir(vault.path()).unwrap().count(), 0);
}

#[test]
fn only_translate_may_replace_a_file() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    fx.step(1, StepName::ArxivFetch, Some("T"), &[], &[], &[]);
    fx.step(
        2,
        StepName::ArxivConvert,
        None,
        &[("original", "original.md", "x")],
        &[],
        &[],
    );
    fx.step(
        3,
        StepName::Note,
        None,
        &[("note", "note.md", "n"), ("original", "o.md", "y")],
        &[],
        &[],
    );
    fx.step(4, StepName::Translate, None, &[], &[], &[]);
    let failure = publish_into(&fx, tempfile::tempdir().unwrap().path()).unwrap_err();
    assert_eq!(failure.exit, StepExit::Permanent);
}

#[test]
fn video_uses_translated_transcript_and_collection() {
    let fx = Fixture::new(Pipeline::Video, AiTool::Codex);
    let mut job = job_spec(Pipeline::Video, AiTool::Codex);
    job.collection = Some("talks".into());
    job.source.platform = Platform::Bilibili;
    job.source.id = "BV1GJ411x7h7".into();
    // 2026-11-07 20:30 UTC is already the 8th in +08:00.
    job.created = "2026-11-07T20:30:00Z".into();
    fx.write_job(&job);
    fx.step(
        1,
        StepName::VideoFetch,
        Some("Talk / Part 1"),
        &[("video", "video.mp4", "mp4")],
        &[],
        &[],
    );
    fx.step(
        2,
        StepName::VideoTranscribe,
        None,
        &[("transcript_json", "transcript.json", "{}")],
        &[],
        &[],
    );
    let frames = [
        ("transcript", "transcript.md", "原始"),
        ("sections", "sections.json", "[]"),
    ];
    fx.step(
        3,
        StepName::VideoFrames,
        None,
        &frames,
        &["frame-0.jpg"],
        &[],
    );
    fx.step(
        4,
        StepName::Note,
        None,
        &[("note", "note.md", "笔记\n")],
        &[],
        &[],
    );
    let meta = [
        ("language", MetaValue::Text("en".into())),
        ("translated", MetaValue::Bool(true)),
    ];
    fx.step(
        5,
        StepName::Translate,
        None,
        &[("transcript", "transcript.md", "双语")],
        &[],
        &meta,
    );
    let vault = tempfile::tempdir().unwrap();
    publish_into(&fx, vault.path()).unwrap();
    let folder = vault
        .path()
        .join("flori/ml/talks/2026-11-08-bili-BV1GJ411x7h7");
    assert_eq!(
        fs::read_to_string(folder.join("transcript.md")).unwrap(),
        "双语"
    );
    assert!(folder.join("assets/frame-0.jpg").exists());
    assert!(!folder.join("video.mp4").exists());
    let note = fs::read_to_string(folder.join("Talk Part 1.md")).unwrap();
    assert!(note.contains("\ncollection: \"talks\"\n"), "{note}");
    assert!(note.contains("\nai_tool: \"codex\"\n"), "{note}");
    assert!(
        note.contains("\ntags: [\"flori\",\"bilibili\",\"ml\"]\n"),
        "{note}"
    );
}

#[test]
fn without_translate_language_is_unknown() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let mut job = job_spec(Pipeline::Arxiv, AiTool::Claude);
    job.translate = TranslateMode::Off;
    job.source.id = "hep-th/9711200".into();
    fx.write_job(&job);
    fx.step(1, StepName::ArxivFetch, None, &[], &[], &[]);
    fx.step(
        2,
        StepName::ArxivConvert,
        None,
        &[("original", "original.md", "x")],
        &[],
        &[],
    );
    fx.step(
        3,
        StepName::Note,
        None,
        &[("note", "note.md", "n\n")],
        &[],
        &[],
    );
    let vault = tempfile::tempdir().unwrap();
    publish_into(&fx, vault.path()).unwrap();
    // No title anywhere: the note is named after the source id.
    let note = vault
        .path()
        .join("flori/ml/unsorted/2026-11-08-arxiv-hep-th_9711200/hep-th9711200.md");
    let text = fs::read_to_string(note).unwrap();
    assert!(
        text.contains("\nlanguage: \"unknown\"\ntranslated: false\n"),
        "{text}"
    );
}
