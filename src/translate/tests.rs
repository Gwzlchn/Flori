use std::{collections::BTreeMap, ffi::OsString, fs, path::PathBuf};

use super::*;
use crate::{
    ai::tests::{Fixture, job_spec},
    pipeline::{AiTool, StepExit, escape_markdown},
};

fn fixture_text(name: &str) -> String {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/translate")
        .join(name);
    fs::read_to_string(path).unwrap()
}

fn arxiv_segments(md: &str) -> Vec<Segment> {
    split_blocks(md)
        .iter()
        .filter(|b| !b.keep)
        .map(|b| protect(&b.text, Pipeline::Arxiv))
        .collect()
}

/// Detection exactly as each path runs it: arXiv through blocks and placeholders, a transcript
/// line by line as video sections.
fn detect_as(pipeline: Pipeline, text: &str) -> &'static str {
    let segments: Vec<Segment> = match pipeline {
        Pipeline::Arxiv => arxiv_segments(text),
        Pipeline::Video => text.lines().map(|l| protect(l, Pipeline::Video)).collect(),
    };
    detect(segments.iter().map(|s| s.text.as_str()))
}

#[test]
fn detection_samples() {
    // The seven samples of the translation research, then extra edge cases.
    let cases = [
        ("lang-en.md", Pipeline::Arxiv, "en"),
        // An English paper quoting a few Chinese examples is still English (Han ratio 0.09).
        ("lang-en-zh.md", Pipeline::Arxiv, "en"),
        ("lang-zh.md", Pipeline::Arxiv, "zh"),
        // Spoken Chinese full of English terms, as a transcript.
        ("lang-spoken-mixed.md", Pipeline::Video, "zh"),
        ("lang-ja.md", Pipeline::Arxiv, "ja"),
        ("lang-ko.md", Pipeline::Arxiv, "ko"),
        ("lang-short.md", Pipeline::Arxiv, "unknown"),
        ("lang-mixed.md", Pipeline::Arxiv, "zh"),
        // Formulas and code would outvote the Chinese text if they were counted.
        ("lang-formula.md", Pipeline::Arxiv, "zh"),
        // So would the URLs.
        ("lang-urls.md", Pipeline::Arxiv, "zh"),
        // Latin letters count as en, other scripts do not.
        ("lang-fr.md", Pipeline::Arxiv, "en"),
        ("lang-ru.md", Pipeline::Arxiv, "other"),
    ];
    for (name, pipeline, want) in cases {
        assert_eq!(detect_as(pipeline, &fixture_text(name)), want, "{name}");
    }
    // × and ÷ are not letters: a Chinese note full of 8×8×8 shapes stays Chinese.
    for sign in ['×', '÷'] {
        let shapes = format!("8{sign}8{sign}8{sign}8 ").repeat(270);
        let note = format!("{}\n\n输入尺寸 {shapes}\n", fixture_text("lang-zh.md"));
        assert_eq!(detect_as(Pipeline::Arxiv, &note), "zh", "{sign}");
    }
}

#[test]
fn stripping_matters_for_formula_and_url_samples() {
    // Counting the raw text, the latin letters of code, formulas and URLs win.
    for name in ["lang-formula.md", "lang-urls.md"] {
        assert_ne!(detect([fixture_text(name).as_str()]), "zh", "{name}");
    }
}

#[test]
fn blocks_keep_math_code_tables_images_and_references_whole() {
    let paper = fixture_text("paper.md");
    let blocks = split_blocks(&paper);
    let kept: Vec<&str> = blocks
        .iter()
        .filter(|b| b.keep)
        .map(|b| b.text.as_str())
        .collect();
    assert_eq!(kept.len(), 7, "{kept:#?}");
    assert!(kept[0].starts_with("$$\n") && kept[0].ends_with("$$") && kept[0].contains("\n\nG(x)"));
    assert!(
        kept[1].starts_with("```python")
            && kept[1].contains("\n\n    logits")
            && kept[1].ends_with("```")
    );
    assert!(kept[2].starts_with("| Model") && kept[2].lines().count() == 4);
    assert_eq!(kept[3], "![](assets/fig1.png)");
    assert_eq!(
        &kept[4..],
        [
            "## References",
            "[1] A. Author. Outrageously large neural networks. 2017.",
            "[2] B. Author. Switch transformers. 2021."
        ]
    );
    // The appendix heading closes the References section.
    let texts: Vec<&str> = blocks
        .iter()
        .filter(|b| !b.keep)
        .map(|b| b.text.as_str())
        .collect();
    assert_eq!(texts.len(), 6);
    assert_eq!(texts[4], "## A Training details");
    // Nothing protected leaks into what is sent, whatever the chunk size.
    let segments = arxiv_segments(&paper);
    for limit in [1, 200, CHUNK_CHARS] {
        let chunks = chunk(&segments, limit);
        assert_eq!(
            chunks.iter().map(|r| r.len()).sum::<usize>(),
            segments.len()
        );
        for seg in &segments {
            for marker in ["$$", "```", "| Model", "[1] A. Author", "fig1.png"] {
                assert!(!seg.text.contains(marker), "{marker} in {:?}", seg.text);
            }
        }
    }
    assert_eq!(chunk(&segments, 1).len(), segments.len());
    assert_eq!(chunk(&segments, CHUNK_CHARS).len(), 1);
}

#[test]
fn footnotes_are_translated_without_their_lead() {
    // arxiv-convert writes definitions before the References; other sources may put them last.
    let md = "## 1 Intro\n\nCode is public[^1].\n\n[^1]: All code is at https://example.org/gan.\n\n\
              ## References\n\n[1] A. Author. GANs. 2014.\n\n[^note-2]: Seen after the references.\n";
    let blocks = split_blocks(md);
    let kinds: Vec<(bool, &str, &str)> = blocks
        .iter()
        .map(|b| (b.keep, b.lead.as_str(), b.text.as_str()))
        .collect();
    assert_eq!(
        kinds,
        [
            (false, "", "## 1 Intro"),
            (false, "", "Code is public[^1]."),
            (false, "[^1]: ", "All code is at https://example.org/gan."),
            (true, "", "## References"),
            (true, "", "[1] A. Author. GANs. 2014."),
            (false, "[^note-2]: ", "Seen after the references."),
        ]
    );
    // The AI only sees the definition's text, never its `[^1]: ` lead.
    let note = protect(&blocks[2].text, Pipeline::Arxiv);
    assert_eq!(note.text, "All code is at ⟦0⟧.");
    // A reply with a full-width colon and the URL moved to the end still renders the lead first.
    let reply = r#"{"segments":[{"id":"1","text":"：全部代码见下方链接 ⟦0⟧"}]}"#;
    let translated = note.restore(&check(reply, std::slice::from_ref(&note)).unwrap()[0]);
    let only_note = [Block {
        lead: blocks[2].lead.clone(),
        text: blocks[2].text.clone(),
        keep: false,
    }];
    assert_eq!(
        render_arxiv(&only_note, vec![translated]),
        "[^1]: ：全部代码见下方链接 https://example.org/gan\n"
    );
    // References in the body are placeholders, and dropping one fails validation.
    let body = [protect(&blocks[1].text, Pipeline::Arxiv)];
    assert_eq!(body[0].text, "Code is public⟦0⟧.");
    let dropped = r#"{"segments":[{"id":"1","text":"代码已公开。"}]}"#;
    assert!(check(dropped, &body).unwrap_err().contains("⟦0⟧"));
}

#[tokio::test]
async fn footnote_leads_survive_a_full_round_trip() {
    let md = format!(
        "{}\n\n[^1]: The note text keeps its place[^2].\n\n[^2]: Second note.\n",
        paragraph(1, "")
    );
    let fx = arxiv(&md);
    let (out, result) = translate(&fx).await;
    result.unwrap();
    assert_eq!(fs::read_to_string(out.join("translation.md")).unwrap(), md);
}

#[test]
fn fences_close_only_on_a_matching_fence_line() {
    let md = "Intro text.\n\n\
              ````markdown\n```python\nprint(1)\n```\n\n<img src=x onerror=alert(1)>\n````\n\n\
              Middle text.\n\n\
              ~~~\n```\n~~ not yet\n~~~~ \n\n\
              - item\n\n  ```text\n  inside a list\n\n  ```\n\n\
              Outro text.\n";
    let blocks = split_blocks(md);
    let kinds: Vec<(bool, &str)> = blocks.iter().map(|b| (b.keep, b.text.as_str())).collect();
    assert_eq!(
        kinds,
        [
            (false, "Intro text."),
            (
                true,
                "````markdown\n```python\nprint(1)\n```\n\n<img src=x onerror=alert(1)>\n````"
            ),
            (false, "Middle text."),
            (true, "~~~\n```\n~~ not yet\n~~~~ "),
            (false, "- item"),
            (true, "  ```text\n  inside a list\n\n  ```"),
            (false, "Outro text."),
        ]
    );
}

#[test]
fn fences_in_numbered_and_nested_lists_stay_whole() {
    // arxiv-convert's list output: continuation lines are indented by the marker's width.
    let tenth = "10. Tenth item.\n    \n    ```\n    x = 1\n    \n    y = 2\n    ```";
    let nested = "- Outer.\n  \n  - Inner.\n    \n    ```\n    for x in xs:\n    \n        print(x)\n    ```";
    let md = format!("{tenth}\n\n{nested}\n\nAfter the lists.\n");
    let blocks = split_blocks(&md);
    let kinds: Vec<(bool, &str)> = blocks.iter().map(|b| (b.keep, b.text.as_str())).collect();
    assert_eq!(
        kinds,
        [
            (false, "10. Tenth item."),
            (true, "    ```\n    x = 1\n    \n    y = 2\n    ```"),
            (false, "- Outer."),
            (false, "  - Inner."),
            (
                true,
                "    ```\n    for x in xs:\n    \n        print(x)\n    ```"
            ),
            (false, "After the lists."),
        ]
    );
}

#[test]
fn fence_open_and_close_edge_cases() {
    assert_eq!(fence("```py`x"), None);
    assert_eq!(fence("~~~ a`b"), Some(('~', 3, 0)));
    // Two backticks are inline code, not a fence.
    assert_eq!(fence("``"), None);
    assert_eq!(fence("``x``"), None);
    let open = fence("  ```").unwrap();
    assert_eq!(open, ('`', 3, 2));
    // A line with an info string opens nothing and closes nothing.
    assert!(!closes("``` x", open));
    // At most 3 spaces deeper than the opening fence.
    assert!(closes("     ```", open));
    assert!(!closes("      ```", open));
    assert!(closes("```` ", open));
    assert!(!closes("~~~", open));
    // The same rules inside split_blocks: the fence ends only at the plain closing line.
    let md = "```\na\n``` x\n\n       ```\nb\n```\n\nAfter.\n";
    let kinds: Vec<(bool, String)> = split_blocks(md)
        .into_iter()
        .map(|b| (b.keep, b.text))
        .collect();
    assert_eq!(
        kinds,
        [
            (true, "```\na\n``` x\n\n       ```\nb\n```".to_owned()),
            (false, "After.".to_owned()),
        ]
    );
}

#[test]
fn display_math_only_opens_on_its_own() {
    let md = "$$x$$ is small and\nthe loss is $y$.\n\n$$ a\n\nb $$\n\n$$z$$\n\nEnd $5\nand $6.\n";
    let blocks = split_blocks(md);
    let kinds: Vec<(bool, &str)> = blocks.iter().map(|b| (b.keep, b.text.as_str())).collect();
    assert_eq!(
        kinds,
        [
            (false, "$$x$$ is small and\nthe loss is $y$."),
            (true, "$$ a\n\nb $$"),
            (true, "$$z$$"),
            (false, "End $5\nand $6."),
        ]
    );
    assert_eq!(
        protect(&blocks[0].text, Pipeline::Arxiv).spans,
        ["$$x$$", "$y$"]
    );
    assert!(protect(&blocks[3].text, Pipeline::Arxiv).spans.is_empty());
}

#[test]
fn inline_spans_become_placeholders_and_come_back() {
    let text = "We study $f(x)$ with `top_k`, see https://example.org/moe. and [3, 5–7]; \
                ![fig](assets/a.png) costs \\$5 [not a cite].";
    let seg = protect(text, Pipeline::Arxiv);
    assert_eq!(
        seg.text,
        "We study ⟦0⟧ with ⟦1⟧, see ⟦2⟧. and ⟦3⟧; ⟦4⟧ costs \\$5 [not a cite]."
    );
    assert_eq!(
        seg.spans,
        [
            "$f(x)$",
            "`top_k`",
            "https://example.org/moe",
            "[3, 5–7]",
            "![fig](assets/a.png)"
        ]
    );
    assert_eq!(seg.restore(&seg.text), text);
    // Translations may move placeholders around.
    assert_eq!(
        seg.restore("⟦4⟧⟦3⟧⟦2⟧⟦1⟧⟦0⟧"),
        "![fig](assets/a.png)[3, 5–7]https://example.org/moe`top_k`$f(x)$"
    );
}

#[test]
fn speech_gets_no_placeholders() {
    let text = "ChatGPT Plus costs $20 a month and Pro costs $200 a month, see `docs` [3].";
    let seg = protect(text, Pipeline::Video);
    assert!(seg.spans.is_empty());
    assert_eq!(seg.text, text);
}

/// The echo the fake CLI gives: every segment comes back unchanged.
fn round_trip(text: &str, pipeline: Pipeline) -> String {
    let part = [protect(text, pipeline)];
    let reply = to_payload(part.iter().map(|s| s.text.as_str()));
    part[0].restore(&check(&reply, &part).unwrap()[0])
}

#[test]
fn literal_placeholder_brackets_cannot_forge_a_placeholder() {
    for pipeline in [Pipeline::Arxiv, Pipeline::Video] {
        for text in ["Use ⟦0⟧ and `code` here ⟦1⟧.", "Only ⟦0⟧.", "\\⟦0⟧ ⟦⟦9⟧"]
        {
            assert_eq!(round_trip(text, pipeline), text, "{pipeline:?} {text}");
        }
    }
    let seg = protect("Only ⟦0⟧.", Pipeline::Video);
    assert_eq!(seg.spans, ["⟦"]);
}

#[test]
fn check_rejects_bad_replies_with_feedback() {
    let part = [
        protect("Use `x` here.", Pipeline::Arxiv),
        protect("Plain.", Pipeline::Arxiv),
    ];
    let ok = r#"{"segments":[{"id":"1","text":"这里用 ⟦0⟧。"},{"id":"2","text":"简单。"}]}"#;
    assert_eq!(check(ok, &part).unwrap(), ["这里用 ⟦0⟧。", "简单。"]);
    assert!(check(&format!("```json\n{ok}\n```"), &part).is_ok());
    let cases = [
        ("not json", "JSON"),
        (r#"{"segments":[{"id":"1","text":"⟦0⟧"}]}"#, "应有 2 个"),
        (
            r#"{"segments":[{"id":"2","text":"⟦0⟧"},{"id":"1","text":"a"}]}"#,
            "id 应为",
        ),
        (
            r#"{"segments":[{"id":"1","text":"没有占位符"},{"id":"2","text":"a"}]}"#,
            "⟦0⟧",
        ),
        (
            r#"{"segments":[{"id":"1","text":"⟦0⟧⟦0⟧"},{"id":"2","text":"a"}]}"#,
            "⟦0⟧",
        ),
        (
            r#"{"segments":[{"id":"1","text":"⟦0⟧"},{"id":"2","text":" "}]}"#,
            "为空",
        ),
    ];
    for (reply, needle) in cases {
        let problem = check(reply, &part).unwrap_err();
        assert!(problem.contains(needle), "{reply}: {problem}");
    }
}

fn arxiv(original: &str) -> Fixture {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    fx.step(
        2,
        StepName::ArxivConvert,
        None,
        &[("original", "original.md", original)],
        &[],
        &[],
    );
    fx
}

/// A video job whose video-transcribe recorded `language` and whose sections are given.
fn video(sections: &serde_json::Value, language: &str) -> Fixture {
    video_with_subtitle(sections, language, None)
}

/// The same, with video-fetch's Chinese manual subtitle when `subtitle_zh` is given.
fn video_with_subtitle(
    sections: &serde_json::Value,
    language: &str,
    subtitle_zh: Option<&str>,
) -> Fixture {
    let fx = Fixture::new(Pipeline::Video, AiTool::Claude);
    let fetched: Vec<_> = subtitle_zh
        .map(|srt| ("subtitle_zh", "subtitle_zh.srt", srt))
        .into_iter()
        .collect();
    fx.step(1, StepName::VideoFetch, Some("Talk"), &fetched, &[], &[]);
    let meta = [("language", MetaValue::Text(language.into()))];
    fx.step(2, StepName::VideoTranscribe, None, &[], &[], &meta);
    let sections = sections.to_string();
    let files = [
        ("sections", "sections.json", sections.as_str()),
        ("transcript", "transcript.md", "old"),
    ];
    fx.step(
        3,
        StepName::VideoFrames,
        None,
        &files,
        &["frame-0.jpg"],
        &[],
    );
    fx
}

fn with_cache(fx: &Fixture) -> Vec<(OsString, OsString)> {
    let mut env = fx.env();
    env.push(("FLORI_CACHE_DIR".into(), cache_dir(fx).into()));
    env
}

fn cache_dir(fx: &Fixture) -> PathBuf {
    fx.job_dir().join("cache/translate")
}

async fn translate(fx: &Fixture) -> (PathBuf, StepResult) {
    let out = fx.out_dir("04-translate");
    let result = run_with(fx.job_dir(), &out, &with_cache(fx)).await;
    (out, result)
}

fn meta(out: &Path) -> BTreeMap<String, MetaValue> {
    pipeline::read_manifest(out).unwrap().meta
}

fn text(value: &str) -> MetaValue {
    MetaValue::Text(value.into())
}

#[tokio::test]
async fn arxiv_translation_keeps_protected_blocks_byte_for_byte() {
    let paper = fixture_text("paper.md");
    let fx = arxiv(&paper);
    let (out, result) = translate(&fx).await;
    result.unwrap();
    // The fake echoes its input, so the restored translation equals the original.
    assert_eq!(
        fs::read_to_string(out.join("translation.md")).unwrap(),
        paper
    );
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.files["translation"], "translation.md");
    assert_eq!(manifest.meta["language"], text("en"));
    assert_eq!(manifest.meta["translated"], MetaValue::Bool(true));
    assert_eq!(manifest.meta["translation_source"], text("ai"));
    assert!(!manifest.meta.contains_key("skipped_reason"));
    let calls = fx.calls();
    assert_eq!(calls.len(), 1);
    assert_eq!(calls[0]["model"], "model-translate");
}

/// About 7000 characters, so every paragraph is a chunk of its own.
fn paragraph(n: usize, marker: &str) -> String {
    format!("Paragraph {n} {marker}. ")
        + &"The router assigns each token to two experts and adds their outputs. ".repeat(100)
}

fn long_paper(count: usize, marked: Option<(usize, &str)>) -> String {
    let paragraphs: Vec<String> = (1..=count)
        .map(|n| {
            paragraph(
                n,
                marked
                    .filter(|(m, _)| *m == n)
                    .map_or("", |(_, marker)| marker),
            )
        })
        .collect();
    paragraphs.join("\n\n") + "\n"
}

#[tokio::test]
async fn retry_after_chunk_three_fails_only_asks_for_three_to_n() {
    let fx = arxiv(&long_paper(5, Some((3, "FAKE_RATE_LIMIT"))));
    let (_, result) = translate(&fx).await;
    let failure = result.unwrap_err();
    assert_eq!(failure.exit, StepExit::Retry);
    assert!(
        failure.message.starts_with("chunk 3/5:"),
        "{}",
        failure.message
    );
    assert_eq!(fx.calls().len(), 3);
    assert_eq!(fs::read_dir(cache_dir(&fx)).unwrap().count(), 2);

    // The rate limit is over: the same chunk now gets a normal reply.
    let fixed = long_paper(5, None);
    let convert = pipeline::find_step(fx.job_dir(), StepName::ArxivConvert)
        .unwrap()
        .unwrap();
    fs::write(convert.join("original.md"), &fixed).unwrap();
    let (out, result) = translate(&fx).await;
    result.unwrap();
    assert_eq!(fx.calls().len(), 3 + 3);
    assert_eq!(
        fs::read_to_string(out.join("translation.md")).unwrap(),
        fixed
    );

    // Everything is cached now.
    translate(&fx).await.1.unwrap();
    assert_eq!(fx.calls().len(), 6);
}

#[tokio::test]
async fn cache_key_includes_the_model() {
    let fx = arxiv(&long_paper(1, None));
    translate(&fx).await.1.unwrap();
    translate(&fx).await.1.unwrap();
    assert_eq!(fx.calls().len(), 1);
    let mut job = job_spec(Pipeline::Arxiv, AiTool::Claude);
    job.ai.model_translate = "other-model".into();
    fx.write_job(&job);
    translate(&fx).await.1.unwrap();
    assert_eq!(fx.calls().len(), 2);
}

#[tokio::test]
async fn exactly_25_chunks_are_still_translated() {
    let fx = arxiv(&long_paper(MAX_CHUNKS, None));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    assert_eq!(meta(&out)["translated"], MetaValue::Bool(true));
    assert_eq!(fx.calls().len(), MAX_CHUNKS);
}

#[tokio::test]
async fn more_than_25_chunks_is_skipped_without_calls() {
    let fx = arxiv(&long_paper(MAX_CHUNKS + 1, None));
    let out = fx.out_dir("04-translate");
    // No FLORI_CACHE_DIR: the step must not get that far.
    run_with(fx.job_dir(), &out, &fx.env()).await.unwrap();
    let meta = meta(&out);
    assert_eq!(meta["skipped_reason"], text("too_long"));
    assert_eq!(meta["chunks"], MetaValue::Int(26));
    assert_eq!(meta["language"], text("en"));
    assert_eq!(meta["translated"], MetaValue::Bool(false));
    assert!(pipeline::read_manifest(&out).unwrap().files.is_empty());
    assert!(fx.calls().is_empty());
}

#[tokio::test]
async fn chinese_and_unknown_sources_only_write_meta() {
    for (name, reason, language) in [
        ("lang-zh.md", "chinese", "zh"),
        ("lang-short.md", "unknown", "unknown"),
    ] {
        let fx = arxiv(&fixture_text(name));
        let (out, result) = translate(&fx).await;
        result.unwrap();
        let manifest = pipeline::read_manifest(&out).unwrap();
        assert!(manifest.files.is_empty());
        assert_eq!(manifest.meta["skipped_reason"], text(reason));
        assert_eq!(manifest.meta["language"], text(language));
        assert_eq!(manifest.meta["translated"], MetaValue::Bool(false));
        assert!(!out.join("translation.md").exists());
        assert!(fx.calls().is_empty());
    }
}

#[tokio::test]
async fn chinese_video_is_skipped_and_keeps_the_transcribed_language() {
    let lines: Vec<serde_json::Value> = fixture_text("lang-spoken-mixed.md")
        .lines()
        .enumerate()
        .map(|(i, line)| serde_json::json!({"start_ms": i * 30_000, "frames": [], "text": line}))
        .collect();
    let fx = video(&serde_json::Value::Array(lines), "zh");
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert!(manifest.files.is_empty());
    assert_eq!(manifest.meta["skipped_reason"], text("chinese"));
    assert_eq!(manifest.meta["language"], text("zh"));
    assert!(fx.calls().is_empty());
}

#[tokio::test]
async fn invalid_reply_is_asked_again_once() {
    let fx = arxiv(&long_paper(1, Some((1, "FAKE_INVALID_ONCE"))));
    translate(&fx).await.1.unwrap();
    assert_eq!(fx.calls().len(), 2);

    let fx = arxiv(&long_paper(1, Some((1, "FAKE_INVALID"))));
    let failure = translate(&fx).await.1.unwrap_err();
    assert_eq!(failure.exit, StepExit::Retry);
    assert!(
        failure.message.contains("validation twice"),
        "{}",
        failure.message
    );
    assert_eq!(fx.calls().len(), 2);
    assert_eq!(fs::read_dir(cache_dir(&fx)).unwrap().count(), 0);
}

#[tokio::test]
async fn auth_failure_stops_at_once() {
    let fx = arxiv(&long_paper(2, Some((1, "FAKE_AUTH"))));
    let failure = translate(&fx).await.1.unwrap_err();
    assert_eq!(failure.exit, StepExit::Auth);
    assert_eq!(fx.calls().len(), 1);
}

#[tokio::test]
async fn video_transcript_is_bilingual_per_section() {
    let first = "So the key idea is that every token picks two experts out of eight, \
                 and the router learns which experts are good at which kinds of input.";
    let third = "That is why the active parameter count stays at thirteen billion\n\
                 even though the model stores forty seven billion parameters in total.";
    let sections = serde_json::json!([
        {"start_ms": 0, "frames": ["frame-0.jpg"], "text": first},
        {"start_ms": 30000, "frames": ["frame-30000.jpg", "frame-41000.jpg"], "text": " "},
        {"start_ms": 3_690_000, "frames": [], "text": third},
    ]);
    let fx = video(&sections, "fr");
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let want = format!(
        "## 00:00\n\n![](assets/frame-0.jpg)\n\n{first}\n\n> [!quote]- 原文\n> {first}\n\n\
         ## 00:30\n\n![](assets/frame-30000.jpg)\n\n![](assets/frame-41000.jpg)\n\n\
         ## 1:01:30\n\n{third}\n\n> [!quote]- 原文\n\
         > That is why the active parameter count stays at thirteen billion\n\
         > even though the model stores forty seven billion parameters in total.\n"
    );
    assert_eq!(fs::read_to_string(out.join("transcript.md")).unwrap(), want);
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.files["transcript"], "transcript.md");
    assert!(manifest.assets.is_empty());
    // The video path keeps video-transcribe's language code instead of guessing one.
    assert_eq!(manifest.meta["language"], text("fr"));
    // Without a Chinese subtitle the AI translates.
    assert_eq!(manifest.meta["translation_source"], text("ai"));
    assert_eq!(fx.calls().len(), 1);
}

#[tokio::test]
async fn a_chinese_manual_subtitle_is_the_translation_without_any_ai_call() {
    // Short on purpose: too few letters for detection, which a subtitle translation ignores.
    let sections = serde_json::json!([
        {"start_ms": 0, "frames": ["frame-0.jpg"], "text": "Imagine a *tiny* robot."},
        {"start_ms": 30000, "frames": [], "text": "It reads #include <vector>."},
        {"start_ms": 60000, "frames": [], "text": "Nobody *translated* #this <part>."},
        {"start_ms": 90000, "frames": [], "text": " "},
    ]);
    // Cues go to the 30s window of their start: 00:01 and 00:12 to the first section, 00:30 to
    // the second, none to the third.
    let srt = "1\n00:00:01,000 --> 00:00:05,000\n想象一个\n\n\
               2\n00:00:12,000 --> 00:00:16,000\n*很小*的机器人。\n\n\
               3\n00:00:30,000 --> 00:00:34,000\n它读到 #include <vector>。\n";
    let fx = video_with_subtitle(&sections, "en", Some(srt));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    // A section without a cue keeps only its original, escaped like everything else.
    let want = "## 00:00\n\n![](assets/frame-0.jpg)\n\n想象一个\\*很小\\*的机器人。\n\n\
                > [!quote]- 原文\n> Imagine a \\*tiny\\* robot.\n\n\
                ## 00:30\n\n它读到 \\#include \\<vector\\>。\n\n\
                > [!quote]- 原文\n> It reads \\#include \\<vector\\>.\n\n\
                ## 01:00\n\nNobody \\*translated\\* \\#this \\<part\\>.\n\n\
                ## 01:30\n";
    assert_eq!(fs::read_to_string(out.join("transcript.md")).unwrap(), want);
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.files["transcript"], "transcript.md");
    assert_eq!(manifest.meta["translated"], MetaValue::Bool(true));
    assert_eq!(manifest.meta["translation_source"], text("subtitle"));
    assert_eq!(manifest.meta["language"], text("en"));
    assert!(!manifest.meta.contains_key("skipped_reason"));
    assert!(fx.calls().is_empty());
}

/// Time and Chinese of each section in transcript.md, in order; None for an original-only one.
fn chinese_by_section(out: &Path) -> Vec<(String, Option<String>)> {
    let transcript = fs::read_to_string(out.join("transcript.md")).unwrap();
    transcript
        .split("## ")
        .skip(1)
        .map(|part| {
            let (time, body) = part.split_once("\n\n").unwrap_or((part.trim(), ""));
            let paragraphs: Vec<&str> = body
                .split("\n\n")
                .filter(|p| !p.trim().is_empty() && !p.starts_with("![]"))
                .collect();
            let bilingual = paragraphs.iter().any(|p| p.starts_with("> [!quote]"));
            let chinese = bilingual.then(|| paragraphs[0].to_owned());
            (time.trim().to_owned(), chinese)
        })
        .collect()
}

#[tokio::test]
async fn cues_outside_a_section_with_text_are_dropped() {
    // 00:05 is before the first section, 01:05 lands on a keyframe-only section, 03:05 is after
    // the last one: none has an original to pair with, and none may shift into a neighbour.
    let sections = serde_json::json!([
        {"start_ms": 30000, "frames": [], "text": "Welcome back."},
        {"start_ms": 60000, "frames": ["frame-60000.jpg"], "text": ""},
        {"start_ms": 120000, "frames": [], "text": "The demo is over."},
        {"start_ms": 150000, "frames": [], "text": "Thanks for watching."},
    ]);
    let srt = "1\n00:00:05,000 --> 00:00:08,000\n片头\n\n\
               2\n00:00:35,000 --> 00:00:38,000\n欢迎回来。\n\n\
               3\n00:01:05,000 --> 00:01:08,000\n画面说明\n\n\
               4\n00:01:35,000 --> 00:01:38,000\n演示进行中\n\n\
               5\n00:02:05,000 --> 00:02:08,000\n演示结束了。\n\n\
               6\n00:03:05,000 --> 00:03:08,000\n片尾\n";
    let fx = video_with_subtitle(&sections, "en", Some(srt));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let section = |time: &str, chinese: Option<&str>| (time.to_owned(), chinese.map(str::to_owned));
    assert_eq!(
        chinese_by_section(&out),
        [
            section("00:30", Some("欢迎回来。")),
            section("01:00", None),
            section("02:00", Some("演示结束了。")),
            section("02:30", None),
        ]
    );
    assert!(fx.calls().is_empty());
}

#[tokio::test]
async fn a_cue_in_a_window_without_a_section_does_not_join_the_previous_one() {
    // 01:05 falls in 01:00-01:30, where there is no section, right after a section with text.
    let sections = serde_json::json!([
        {"start_ms": 0, "frames": [], "text": "Hello."},
        {"start_ms": 30000, "frames": [], "text": "The demo starts."},
        {"start_ms": 90000, "frames": [], "text": "The demo is over."},
    ]);
    let srt = "1\n00:00:05,000 --> 00:00:08,000\n你好。\n\n\
               2\n00:00:35,000 --> 00:00:38,000\n演示开始。\n\n\
               3\n00:01:05,000 --> 00:01:08,000\n画面说明\n\n\
               4\n00:01:35,000 --> 00:01:38,000\n演示结束了。\n";
    let fx = video_with_subtitle(&sections, "en", Some(srt));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let section = |time: &str, chinese: &str| (time.to_owned(), Some(chinese.to_owned()));
    assert_eq!(
        chinese_by_section(&out),
        [
            section("00:00", "你好。"),
            section("00:30", "演示开始。"),
            section("01:30", "演示结束了。"),
        ]
    );
}

#[tokio::test]
async fn a_chinese_subtitle_does_not_override_the_chinese_skip() {
    // A Chinese transcript next to a Chinese subtitle (a misreported language) stays skipped.
    let lines: Vec<serde_json::Value> = fixture_text("lang-spoken-mixed.md")
        .lines()
        .enumerate()
        .map(|(i, line)| serde_json::json!({"start_ms": i * 30_000, "frames": [], "text": line}))
        .collect();
    let srt = "1\n00:00:01,000 --> 00:00:05,000\n好的\n";
    let fx = video_with_subtitle(&serde_json::Value::Array(lines), "zh", Some(srt));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.meta["skipped_reason"], text("chinese"));
    assert_eq!(manifest.meta["translated"], MetaValue::Bool(false));
    assert!(!manifest.meta.contains_key("translation_source"));
    assert!(manifest.files.is_empty());
    assert!(!out.join("transcript.md").exists());
}

#[tokio::test]
async fn a_chinese_subtitle_translates_even_past_the_chunk_limit() {
    // Each section is about 12k characters, one chunk each: the AI path would skip as too_long.
    let long = "The router assigns each token to two experts and adds their outputs. ".repeat(175);
    let sections: Vec<serde_json::Value> = (0..MAX_CHUNKS as u64 + 1)
        .map(|i| serde_json::json!({"start_ms": i * 30_000, "frames": [], "text": long}))
        .collect();
    let srt: String = (0..MAX_CHUNKS as u64 + 1)
        .map(|i| {
            format!(
                "{}\n{} --> {}\n第 {i} 节\n\n",
                i + 1,
                srt_time(i * 30_000),
                srt_time(i * 30_000 + 1000)
            )
        })
        .collect();
    let fx = video_with_subtitle(&serde_json::Value::Array(sections), "en", Some(&srt));
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let manifest = pipeline::read_manifest(&out).unwrap();
    assert_eq!(manifest.meta["translation_source"], text("subtitle"));
    assert_eq!(manifest.meta["translated"], MetaValue::Bool(true));
    assert!(!manifest.meta.contains_key("chunks"));
    assert!(!manifest.meta.contains_key("skipped_reason"));
    assert!(fx.calls().is_empty());
    let segments: Vec<Segment> = (0..=MAX_CHUNKS)
        .map(|_| protect(&long, Pipeline::Video))
        .collect();
    assert_eq!(chunk(&segments, CHUNK_CHARS).len(), MAX_CHUNKS + 1);
}

fn srt_time(ms: u64) -> String {
    let s = ms / 1000;
    format!(
        "{:02}:{:02}:{:02},{:03}",
        s / 3600,
        s / 60 % 60,
        s % 60,
        ms % 1000
    )
}

#[tokio::test]
async fn video_text_is_escaped_and_money_is_translated_whole() {
    let speech = "ChatGPT Plus costs $20 a month and Pro costs $200 a month, so call `__init__` \
                  first, then #include <vector> and *x* without any surprises in the notes. \
                  The rest of this section only exists so that detection sees enough letters.";
    let quoted = "- a list marker\n+ another one at the start of a line, plus ~tilde~ text.";
    let sections = serde_json::json!([
        {"start_ms": 0, "frames": [], "text": speech},
        {"start_ms": 30000, "frames": [], "text": quoted},
    ]);
    let fx = video(&sections, "en");
    let (out, result) = translate(&fx).await;
    result.unwrap();
    let transcript = fs::read_to_string(out.join("transcript.md")).unwrap();
    // The fake echoes, so the "translation" is the whole sentence, dollars included.
    let speech_md = escape_markdown(speech);
    let quoted_md = escape_markdown(quoted);
    let quote_lines: Vec<String> = quoted_md.lines().map(|l| format!("> {l}")).collect();
    let want = format!(
        "## 00:00\n\n{speech_md}\n\n> [!quote]- 原文\n> {speech_md}\n\n\
         ## 00:30\n\n{quoted_md}\n\n> [!quote]- 原文\n{}\n",
        quote_lines.join("\n")
    );
    assert_eq!(transcript, want);
    assert!(speech_md.contains("\\$20 a month and Pro costs \\$200"));
    assert!(quoted_md.starts_with("\\- a list") && quoted_md.contains("\n\\+ another"));
}
