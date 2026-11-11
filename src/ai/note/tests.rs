use std::{fs, path::Path};

use super::*;
use crate::{
    ai::tests::{Fixture, image_bytes},
    pipeline::{AiTool, StepExit},
};

fn sent_images(fx: &Fixture) -> Vec<serde_json::Value> {
    let calls = fx.calls();
    assert_eq!(calls.len(), 1);
    calls[0]["images"].as_array().cloned().unwrap_or_default()
}

fn ffmpeg_calls(fx: &Fixture) -> Vec<String> {
    let log = fs::read_to_string(fx.home.path().join("fake-ffmpeg.log")).unwrap_or_default();
    log.lines().map(str::to_owned).collect()
}

async fn run_note(fx: &Fixture, seq: &str) -> (std::path::PathBuf, StepResult) {
    let out = fx.out_dir(seq);
    let result = note_with(fx.job_dir(), &out, &fx.env()).await;
    (out, result)
}

/// Overwrites assets the fixture wrote as text with real image headers.
fn real_images(step_dir: &Path, names: &[&str], kind: &str) {
    for name in names {
        fs::write(
            step_dir.join("assets").join(name),
            image_bytes(kind, 800, 600),
        )
        .unwrap();
    }
}

/// The image list lines of a note the fake echoed: `[N] label` with N a number.
fn listed(note: &str) -> Vec<String> {
    note.lines()
        .filter_map(|l| l.strip_prefix('[')?.split_once("] "))
        .filter(|(n, _)| n.bytes().all(|b| b.is_ascii_digit()))
        .map(|(_, label)| label.to_owned())
        .collect()
}

/// figures.json from `(label, caption, images)` entries, as arxiv-convert writes it.
fn figures_json(figures: &[(&str, &str, &[&str])]) -> String {
    let entries: Vec<serde_json::Value> = figures
        .iter()
        .map(|(label, caption, images)| {
            serde_json::json!({"label": label, "caption": caption, "images": images})
        })
        .collect();
    serde_json::to_string_pretty(&entries).unwrap()
}

/// A paper with these figures and image assets, all real PNG headers.
fn paper(fx: &Fixture, figures: &[(&str, &str, &[&str])], images: &[&str]) -> std::path::PathBuf {
    fx.step(1, StepName::ArxivFetch, Some("Paper"), &[], &[], &[]);
    let figures = figures_json(figures);
    let files = [
        ("original", "original.md", "# Paper\n"),
        ("figures", "figures.json", figures.as_str()),
    ];
    let dir = fx.step(2, StepName::ArxivConvert, None, &files, images, &[]);
    real_images(&dir, images, "png");
    dir
}

fn svg_fixture(name: &str) -> String {
    fs::read_to_string(
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures/note")
            .join(name),
    )
    .unwrap()
}

fn read_note(out: &Path) -> String {
    fs::read_to_string(out.join("note.md")).unwrap()
}

#[tokio::test]
async fn a_paper_without_figures_sends_no_images() {
    for tool in [AiTool::Claude, AiTool::Codex] {
        let fx = Fixture::new(Pipeline::Arxiv, tool);
        fx.step(
            1,
            StepName::ArxivFetch,
            Some("Mixtral of Experts"),
            &[],
            &[],
            &[],
        );
        // Converted before figures.json existed: a captioned image of original.md is not sent.
        let original = "# Mixtral\n\n![](assets/moe.png)\n\nFigure 1: Mixture of experts.\n";
        let files = [("original", "original.md", original)];
        let dir = fx.step(2, StepName::ArxivConvert, None, &files, &["moe.png"], &[]);
        real_images(&dir, &["moe.png"], "png");
        let (out, result) = run_note(&fx, "03-note").await;
        result.unwrap();
        assert_eq!(
            read_note(&out),
            format!("标题:Mixtral of Experts\n\n{original}")
        );
        let manifest = pipeline::read_manifest(&out).unwrap();
        assert_eq!(manifest.title, None);
        assert_eq!(manifest.files["note"], "note.md");
        assert!(manifest.assets.is_empty());
        assert!(sent_images(&fx).is_empty(), "{tool:?}");
        assert_eq!(fx.calls()[0]["model"], "model-note");
        if tool == AiTool::Claude {
            // The image instructions only go along with images.
            let prompt = fx.calls()[0]["prompt"].as_str().unwrap().to_owned();
            assert!(!prompt.contains("img:N"), "{prompt}");
        }
    }
}

/// A paper whose figures exercise every rule: a two-panel figure, SVG, an image in no figure,
/// and one too large to send as is.
fn figure_paper(fx: &Fixture, extra: &str) {
    fx.step(1, StepName::ArxivFetch, Some("Paper"), &[], &[], &[]);
    let original = format!(
        "# Paper\n\n![](assets/fig1.png)\n\nFigure 1: Overview of the model.\n\n\
         ![](assets/logo.png)\n\nOur method works.\n\n{extra}\n"
    );
    let figures = figures_json(&[
        ("Figure 1", "Overview of the model.", &["fig1.png"]),
        ("Figure 2", "A vector drawing.", &["fig2.svg"]),
        ("Figure 3", "Ablations.", &["fig3a.png", "fig3b.jpg"]),
        ("Table 1", "Results.", &["big.png"]),
    ]);
    let assets = [
        "fig1.png",
        "fig2.svg",
        "fig3a.png",
        "fig3b.jpg",
        "logo.png",
        "big.png",
    ];
    let files = [
        ("original", "original.md", original.as_str()),
        ("figures", "figures.json", figures.as_str()),
    ];
    let dir = fx.step(2, StepName::ArxivConvert, None, &files, &assets, &[]);
    real_images(&dir, &["fig1.png", "fig3a.png", "logo.png"], "png");
    real_images(&dir, &["fig3b.jpg"], "jpg");
    fs::write(dir.join("assets/fig2.svg"), svg_fixture("figure.svg")).unwrap();
    fs::write(dir.join("assets/big.png"), image_bytes("png", 3000, 2000)).unwrap();
}

#[tokio::test]
async fn a_paper_sends_its_captioned_figures_and_links_them() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    // The fake echoes its input, so references written here come back as the model's.
    figure_paper(
        &fx,
        "See ![总览 | 箭头指向 [路由器] \\ 输出](img:1) and ![](img:5 \"title\") \
         and ![bad](img:99) and ![none](img:) and ![kept](assets/fig1.png).",
    );
    // ffmpeg must not see credentials, only PATH, LANG and TMPDIR.
    let mut env = fx.env();
    env.push(("CLAUDE_CODE_OAUTH_TOKEN".into(), "test-token".into()));
    env.push(("LANG".into(), "C.UTF-8".into()));
    let out = fx.out_dir("03-note");
    note_with(fx.job_dir(), &out, &env).await.unwrap();
    let images = sent_images(&fx);
    assert!(
        fx.calls()[0]["prompt"]
            .as_str()
            .unwrap()
            .contains("![中文描述](img:N)")
    );
    let types: Vec<&str> = images
        .iter()
        .map(|i| i["media_type"].as_str().unwrap())
        .collect();
    // fig2.svg goes as a PNG drawing; logo.png is in no figure.
    let png = "image/png";
    assert_eq!(types, [png, png, png, "image/jpeg", png]);
    // big.png went through ffmpeg, which may only read a local PNG.
    let calls = ffmpeg_calls(&fx);
    assert_eq!(calls.len(), 2);
    assert!(calls[0].contains(" -i pipe:0 "), "{}", calls[0]);
    let (call, env_names) = calls[1].split_once(" | env ").unwrap();
    assert!(
        call.contains("-protocol_whitelist file -f png_pipe -i "),
        "{call}"
    );
    assert!(call.contains("/assets/big.png "), "{call}");
    assert!(
        call.contains("scale='min(1568,iw)':'min(1568,ih)'"),
        "{call}"
    );
    let env_names: Vec<&str> = env_names.split(',').collect();
    for name in ["PATH", "LANG", "TMPDIR"] {
        assert!(env_names.contains(&name), "{env_names:?}");
    }
    for name in ["HOME", "CLAUDE_CODE_OAUTH_TOKEN"] {
        assert!(!env_names.contains(&name), "{env_names:?}");
    }
    // The model got the 1568x1045 copy.
    let head = images[4]["head"].as_str().unwrap();
    assert!(head.ends_with("494844520000062000000415"), "{head}");
    let note = read_note(&out);
    assert!(
        note.contains(
            "图片(序号即消息末尾附图的顺序):\n\
             [1] Figure 1: Overview of the model.\n[2] Figure 2: A vector drawing.\n\
             [3] Figure 3: Ablations.\n[4] Figure 3: Ablations.\n[5] Table 1: Results.\n"
        ),
        "{note}"
    );
    // N=5 links the original big.png, not the shrunk copy; unknown and missing N are dropped.
    assert!(
        note.contains(
            "See ![总览 / 箭头指向 路由器 输出](assets/fig1.png) and ![](assets/big.png) \
             and  and  and ![kept](assets/fig1.png)."
        ),
        "{note}"
    );
    assert!(!note.contains("img:"), "{note}");
}

#[tokio::test]
async fn codex_gets_the_figures_as_image_files() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Codex);
    figure_paper(&fx, "");
    run_note(&fx, "03-note").await.1.unwrap();
    let images = sent_images(&fx);
    let names: Vec<String> = images
        .iter()
        .map(|i| {
            let path = Path::new(i["path"].as_str().unwrap());
            path.file_name().unwrap().to_string_lossy().into_owned()
        })
        .collect();
    assert_eq!(
        names,
        [
            "fig1.png",
            "image-1.png",
            "fig3a.png",
            "fig3b.jpg",
            "image-4.png"
        ]
    );
}

#[tokio::test]
async fn a_figure_ffmpeg_cannot_shrink_is_left_out() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let figures: [(&str, &str, &[&str]); 2] = [
        ("Figure 1", "Too big.", &["UNDECODABLE.jpg"]),
        ("Figure 2", "Fine.", &["small.png"]),
    ];
    let dir = paper(&fx, &figures, &["UNDECODABLE.jpg", "small.png"]);
    // Over 1 MiB, whatever its pixel size.
    let mut big = image_bytes("jpg", 100, 100);
    big.resize(1_100_000, 0);
    fs::write(dir.join("assets/UNDECODABLE.jpg"), big).unwrap();
    fs::write(dir.join("assets/small.png"), image_bytes("png", 100, 100)).unwrap();
    let (out, result) = run_note(&fx, "03-note").await;
    result.unwrap();
    assert_eq!(ffmpeg_calls(&fx).len(), 1);
    assert_eq!(sent_images(&fx).len(), 1);
    assert!(read_note(&out).contains("[1] Figure 2: Fine.\n"));
}

/// `count` figures `Figure <n>` with one image `f<n>.png` each: their labels and the names.
fn many_figures(count: usize) -> (Vec<String>, Vec<String>) {
    let labels = (1..=count).map(|n| format!("Figure {n}")).collect();
    let names = (1..=count).map(|n| format!("f{n}.png")).collect();
    (labels, names)
}

/// A paper with the figures of [`many_figures`].
fn many_figure_paper(fx: &Fixture, count: usize) -> (std::path::PathBuf, Vec<String>) {
    let (labels, names) = many_figures(count);
    let images: Vec<[&str; 1]> = names.iter().map(|n| [n.as_str()]).collect();
    let figures: Vec<(&str, &str, &[&str])> = labels
        .iter()
        .zip(&images)
        .map(|(label, image)| (label.as_str(), "Plot.", image.as_slice()))
        .collect();
    let assets: Vec<&str> = names.iter().map(String::as_str).collect();
    (paper(fx, &figures, &assets), names)
}

#[tokio::test]
async fn every_figure_is_sent() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    many_figure_paper(&fx, 30);
    let (out, result) = run_note(&fx, "03-note").await;
    result.unwrap();
    assert_eq!(sent_images(&fx).len(), 30);
    let want: Vec<String> = (1..=30).map(|n| format!("Figure {n}: Plot.")).collect();
    assert_eq!(listed(&read_note(&out)), want);
}

#[tokio::test]
async fn figures_over_the_request_budget_are_dropped_evenly() {
    // Sixteen figures of 1 MiB, the most sent without shrinking: 22.4 MB as base64.
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let (dir, names) = many_figure_paper(&fx, 16);
    for name in &names {
        let mut bytes = image_bytes("png", 100, 100);
        bytes.resize(MAX_IMAGE_BYTES as usize, 0);
        fs::write(dir.join("assets").join(name), bytes).unwrap();
    }
    let (out, result) = run_note(&fx, "03-note").await;
    result.unwrap();
    let images = sent_images(&fx);
    let encoded: u64 = images
        .iter()
        .map(|i| i["bytes"].as_u64().unwrap().div_ceil(3) * 4)
        .sum();
    assert!(encoded <= REQUEST_IMAGE_BYTES, "{encoded}");
    assert_eq!(images.len(), 14);
    let labels = listed(&read_note(&out));
    assert_eq!(labels.len(), 14);
    assert_eq!(labels[0], "Figure 1: Plot.");
    assert_eq!(labels[13], "Figure 16: Plot.");
    assert!(ffmpeg_calls(&fx).is_empty());
}

#[test]
fn the_budget_keeps_an_even_share_of_positions() {
    let dir = tempfile::tempdir().unwrap();
    let pictures = |sizes: &[usize]| -> Vec<Picture> {
        sizes
            .iter()
            .enumerate()
            .map(|(i, size)| {
                let path = dir.path().join(format!("p{i}"));
                fs::write(&path, vec![0; *size]).unwrap();
                Picture {
                    basename: format!("p{i}"),
                    asset: path.clone(),
                    path,
                    label: String::new(),
                }
            })
            .collect()
    };
    let kept = |pictures: Vec<Picture>, budget: u64| -> Vec<String> {
        within_budget(pictures, budget)
            .into_iter()
            .map(|p| p.basename)
            .collect()
    };
    // Twenty pictures of 4000 base64 bytes: everything fits, then half of them.
    assert_eq!(kept(pictures(&[3000; 20]), 80_000).len(), 20);
    let half: Vec<String> = [0, 2, 4, 6, 8, 10, 12, 14, 16, 19]
        .iter()
        .map(|i| format!("p{i}"))
        .collect();
    assert_eq!(kept(pictures(&[3000; 20]), 40_000), half);
    // Room for one keeps the first; room for none sends none.
    assert_eq!(kept(pictures(&[3000; 20]), 4_000), ["p0"]);
    assert!(kept(pictures(&[3000; 20]), 100).is_empty());
    // The average allows three, but the three even picks are the large ones: two go.
    let mixed = kept(pictures(&[3000, 300, 3000, 300, 3000]), 8_500);
    assert_eq!(mixed, ["p0", "p4"]);
}

fn frame_sections(frames_per_section: u64, sections: u64) -> (String, Vec<String>) {
    let mut names = Vec::new();
    let json: Vec<serde_json::Value> = (0..sections)
        .map(|s| {
            let frames: Vec<String> = (0..frames_per_section)
                .map(|f| format!("frame-{}.jpg", s * 30_000 + f * 10_000))
                .collect();
            names.extend(frames.iter().cloned());
            let text = format!("Section {s} explains step {s} of the demo in some detail.");
            serde_json::json!({"start_ms": s * 30_000, "frames": frames, "text": text})
        })
        .collect();
    (serde_json::Value::Array(json).to_string(), names)
}

fn video_job(fx: &Fixture, frames_per_section: u64, sections: u64) {
    fx.step(1, StepName::VideoFetch, Some("Talk"), &[], &[], &[]);
    let (sections, names) = frame_sections(frames_per_section, sections);
    let files = [
        ("sections", "sections.json", sections.as_str()),
        ("transcript", "transcript.md", "x"),
    ];
    let assets: Vec<&str> = names.iter().map(String::as_str).collect();
    let dir = fx.step(3, StepName::VideoFrames, None, &files, &assets, &[]);
    real_images(&dir, &assets, "jpg");
}

/// Width and height from the head the fake claude logged for an image.
fn sent_size(image: &serde_json::Value) -> Option<(u32, u32)> {
    let hex = image["head"].as_str()?;
    let bytes: Option<Vec<u8>> = (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(hex.get(i..i + 2)?, 16).ok())
        .collect();
    image_size(&bytes?)
}

#[tokio::test]
async fn a_video_sends_every_keyframe_as_a_small_copy() {
    let fx = Fixture::new(Pipeline::Video, AiTool::Claude);
    // Eighteen 800x600 frames at 0s, 10s, ... 170s.
    video_job(&fx, 3, 6);
    let (out, result) = run_note(&fx, "04-note").await;
    result.unwrap();
    let images = sent_images(&fx);
    assert_eq!(images.len(), 18);
    assert!(images.iter().all(|i| i["media_type"] == "image/jpeg"));
    assert!(images.iter().all(|i| sent_size(i) == Some((768, 576))));
    let shrinks = ffmpeg_calls(&fx);
    assert_eq!(shrinks.len(), 18);
    let scale = format!("min({FRAME_SIDE},iw)");
    assert!(shrinks.iter().all(|call| call.contains(&scale)));
    let note = read_note(&out);
    let times: Vec<&str> = note
        .lines()
        .filter_map(|l| l.strip_prefix('[')?.split_once("] "))
        .filter(|(n, _)| n.bytes().all(|b| b.is_ascii_digit()))
        .map(|(_, rest)| rest.split(' ').next().unwrap())
        .collect();
    let every: Vec<String> = (0..18).map(|i| pipeline::clock(i * 10_000)).collect();
    assert_eq!(times, every);
    assert!(note.contains("[1] 00:00 Section 0 explains step 0 of the demo in some detail.\n"));
    // The transcript itself still follows the list.
    assert!(note.contains("\n[00:30] Section 1 explains"), "{note}");
}

#[test]
fn a_long_video_sends_at_most_the_api_image_limit() {
    let dir = tempfile::tempdir().unwrap();
    let count = MAX_REQUEST_IMAGES as u64 + 1;
    let sections: Vec<Section> = (0..count)
        .map(|s| {
            let name = format!("frame-{}.jpg", s * 30_000);
            fs::write(dir.path().join(&name), image_bytes("jpg", 10, 10)).unwrap();
            Section {
                start_ms: s * 30_000,
                frames: vec![name],
                text: String::new(),
            }
        })
        .collect();
    let frames = video_frames(&sections, dir.path());
    assert_eq!(frames.len(), MAX_REQUEST_IMAGES);
    assert_eq!(frames[0].basename, "frame-0.jpg");
    let last = format!("frame-{}.jpg", (count - 1) * 30_000);
    assert_eq!(frames[MAX_REQUEST_IMAGES - 1].basename, last);
}

#[tokio::test]
async fn a_frame_label_keeps_150_characters_of_its_section() {
    let fx = Fixture::new(Pipeline::Video, AiTool::Claude);
    fx.step(1, StepName::VideoFetch, Some("Talk"), &[], &[], &[]);
    let long = "长".repeat(400);
    let sections = serde_json::json!([{"start_ms": 0, "frames": ["frame-5000.jpg"], "text": long}]);
    let sections = sections.to_string();
    let files = [("sections", "sections.json", sections.as_str())];
    let dir = fx.step(
        3,
        StepName::VideoFrames,
        None,
        &files,
        &["frame-5000.jpg"],
        &[],
    );
    real_images(&dir, &["frame-5000.jpg"], "jpg");
    let (out, result) = run_note(&fx, "04-note").await;
    result.unwrap();
    let label = format!("[1] 00:05 {}\n", "长".repeat(FRAME_TEXT_CHARS));
    assert!(read_note(&out).contains(&label));
}

#[tokio::test]
async fn a_video_without_frames_still_gets_a_note() {
    let fx = Fixture::new(Pipeline::Video, AiTool::Claude);
    fx.step(1, StepName::VideoFetch, Some("Talk"), &[], &[], &[]);
    let sections = r#"[{"start_ms":0,"frames":[],"text":"Hello there."},
        {"start_ms":30000,"frames":[],"text":"  "},
        {"start_ms":3690000,"frames":[],"text":"Bye."}]"#;
    let files = [("sections", "sections.json", sections)];
    fx.step(3, StepName::VideoFrames, None, &files, &[], &[]);
    let (out, result) = run_note(&fx, "04-note").await;
    result.unwrap();
    assert_eq!(
        read_note(&out),
        "标题:Talk\n\n[00:00] Hello there.\n[1:01:30] Bye.\n"
    );
    assert!(sent_images(&fx).is_empty());
}

#[tokio::test]
async fn failures_keep_their_class_with_images_attached() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    figure_paper(&fx, "FAKE_AUTH");
    let (out, result) = run_note(&fx, "03-note").await;
    assert_eq!(result.unwrap_err().exit, StepExit::Auth);
    assert!(!out.join(pipeline::MANIFEST_FILE).exists());
    assert_eq!(sent_images(&fx).len(), 5);

    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    figure_paper(&fx, "FAKE_RATE_LIMIT");
    assert_eq!(
        run_note(&fx, "03-note").await.1.unwrap_err().exit,
        StepExit::Retry
    );
}

#[test]
fn image_references_are_linked_or_dropped() {
    let pictures: Vec<Picture> = ["a.png", "b.jpg"]
        .iter()
        .map(|name| Picture {
            basename: (*name).into(),
            asset: Path::new("/tmp").join(name),
            path: Path::new("/tmp").join(name),
            label: String::new(),
        })
        .collect();
    let cases = [
        ("x ![图](img:1) y", "x ![图](assets/a.png) y"),
        ("![](img:2)![](img:1)", "![](assets/b.jpg)![](assets/a.png)"),
        ("drop ![no](img:3) and ![zero](img:0).", "drop  and ."),
        ("![a [b] c\\d|e](img:2)", "![a b cd/e](assets/b.jpg)"),
        // Images under assets/ and plain links stay; any other image is dropped.
        (
            "![x](assets/a.png) ![y](https://e.org/i.png) [l](img:1)",
            "![x](assets/a.png)  [l](img:1)",
        ),
        (
            "![a](data:image/png;base64,AAAA)![b](<http://e.org/x.png>)![c](../x.png)",
            "",
        ),
        ("![up](assets/../../etc/passwd)![d](/etc/x.png)", ""),
        ("[link](https://e.org) stays", "[link](https://e.org) stays"),
        ("![open](img:1\nnext", "![open](img:1\nnext"),
        ("![t](img:2 \"title\")", "![t](assets/b.jpg)"),
    ];
    for (note, want) in cases {
        assert_eq!(link_images(note, &pictures), want, "{note}");
    }
}

#[test]
fn image_size_reads_png_gif_and_jpeg_headers() {
    assert_eq!(
        image_size(&image_bytes("png", 3000, 2000)),
        Some((3000, 2000))
    );
    assert_eq!(
        image_size(&image_bytes("jpg", 1280, 720)),
        Some((1280, 720))
    );
    let mut gif = b"GIF89a".to_vec();
    gif.extend([0x20, 0x03, 0x58, 0x02]);
    assert_eq!(image_size(&gif), Some((800, 600)));
    // A fill byte before the start-of-frame marker.
    let mut jpeg = vec![0xFF, 0xD8, 0xFF];
    jpeg.extend(&image_bytes("jpg", 10, 20)[8..]);
    assert_eq!(image_size(&jpeg), Some((10, 20)));
    assert_eq!(image_size(b"RIFF....WEBP"), None);
    assert_eq!(image_size(&[0xFF, 0xD8, 0xFF]), None);
}

#[test]
fn large_images_are_shrunk_by_size_or_pixels() {
    let dir = tempfile::tempdir().unwrap();
    let write = |name: &str, bytes: Vec<u8>| {
        let path = dir.path().join(name);
        fs::write(&path, bytes).unwrap();
        path
    };
    let ok = write("ok.png", image_bytes("png", 1568, 1000));
    assert!(!too_large(&ok, FIGURE_SIDE).unwrap());
    assert!(too_large(&ok, FRAME_SIDE).unwrap());
    let frame = write("frame.jpg", image_bytes("jpg", 768, 432));
    assert!(!too_large(&frame, FRAME_SIDE).unwrap());
    let wide = write("wide.png", image_bytes("png", 1569, 10));
    assert!(too_large(&wide, FIGURE_SIDE).unwrap());
    let tall = write("tall.jpg", image_bytes("jpg", 10, 2000));
    assert!(too_large(&tall, FIGURE_SIDE).unwrap());
    let mut heavy = image_bytes("jpg", 100, 100);
    heavy.resize(MAX_IMAGE_BYTES as usize + 1, 0);
    assert!(too_large(&write("heavy.jpg", heavy), FIGURE_SIDE).unwrap());
    let unknown = write("unknown.webp", vec![0; 100]);
    assert!(!too_large(&unknown, FIGURE_SIDE).unwrap());
}

#[tokio::test]
async fn every_image_of_a_figure_gets_its_caption_in_page_order() {
    // Figure 2 of the Transformer paper: two panels, the left one between two text paragraphs,
    // which the old paragraph matching lost.
    let figures: [(&str, &str, &[&str]); 5] = [
        (
            "Figure 1",
            "The Transformer - model architecture.",
            &["ModalNet-21.png"],
        ),
        (
            "Figure 2",
            "(left) Scaled Dot-Product Attention. (right) Multi-Head Attention.",
            &["ModalNet-19.png", "ModalNet-20.png"],
        ),
        // A name convert did not list and one sent already are skipped; so is a figure without
        // a usable image.
        ("Figure 3", "Outside.", &["unlisted.png", "ModalNet-21.png"]),
        ("", "A caption without a tag.", &["panel.png"]),
        ("Table 1", "", &["table.png"]),
    ];
    let images = [
        "ModalNet-19.png",
        "ModalNet-20.png",
        "ModalNet-21.png",
        "panel.png",
        "table.png",
    ];
    // The codex fake records image paths.
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Codex);
    let dir = paper(&fx, &figures, &images);
    fs::write(dir.join("assets/unlisted.png"), image_bytes("png", 10, 10)).unwrap();
    let (out, result) = run_note(&fx, "03-note").await;
    result.unwrap();
    assert_eq!(
        listed(&read_note(&out)),
        [
            "Figure 1: The Transformer - model architecture.",
            "Figure 2: (left) Scaled Dot-Product Attention. (right) Multi-Head Attention.",
            "Figure 2: (left) Scaled Dot-Product Attention. (right) Multi-Head Attention.",
            "A caption without a tag.",
            "Table 1",
        ]
    );
    let sent: Vec<String> = sent_images(&fx)
        .iter()
        .map(|i| {
            i["path"]
                .as_str()
                .unwrap()
                .rsplit('/')
                .next()
                .unwrap()
                .to_owned()
        })
        .collect();
    assert_eq!(
        sent,
        [
            "ModalNet-21.png",
            "ModalNet-19.png",
            "ModalNet-20.png",
            "panel.png",
            "table.png"
        ]
    );
}

#[tokio::test]
async fn a_broken_figures_file_fails_the_note() {
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    fx.step(1, StepName::ArxivFetch, Some("Paper"), &[], &[], &[]);
    let files = [
        ("original", "original.md", "# Paper\n"),
        ("figures", "figures.json", "[{\"label\": 1}]"),
    ];
    fx.step(2, StepName::ArxivConvert, None, &files, &[], &[]);
    let failure = run_note(&fx, "03-note").await.1.unwrap_err();
    assert_eq!(failure.exit, StepExit::Permanent);
    assert!(fx.calls().is_empty());
}

#[tokio::test]
async fn svg_figures_are_drawn_from_stdin_within_bounds() {
    let figures: [(&str, &str, &[&str]); 6] = [
        ("Figure 1", "A plot.", &["plot.svg"]),
        // External, local and relative references, and a declared size of a million pixels.
        ("Figure 2", "Hostile.", &["hostile.svg"]),
        // Too thin to draw within twice side x side pixels either way.
        ("Figure 3", "Thin.", &["thin.svg"]),
        // An internal doctype subset can define entities that expand without end, or pull in
        // external ones.
        ("Figure 4", "Entities.", &["entities.svg"]),
        ("Figure 5", "External entities.", &["external.svg"]),
        ("Figure 6", "A web page.", &["page.svg"]),
    ];
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let dir = paper(&fx, &figures, &[]);
    let assets = dir.join("assets");
    let thin = r#"<svg xmlns="http://www.w3.org/2000/svg" width="1" height="100000000"/>"#;
    let entities = "<?xml version=\"1.0\"?>\n<!DOCTYPE svg [<!ENTITY a \"aaaa\">]>\n\
                    <svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\">&a;</svg>";
    let external = "<!DOCTYPE svg [ %external; ]>\n<svg width=\"10\" height=\"10\"/>";
    let page = "<!DOCTYPE html><html><body><svg width=\"10\" height=\"10\"></svg></body></html>";
    for (name, text) in [
        ("plot.svg", svg_fixture("figure.svg")),
        ("hostile.svg", svg_fixture("hostile.svg")),
        ("thin.svg", thin.to_owned()),
        ("entities.svg", entities.to_owned()),
        ("external.svg", external.to_owned()),
        ("page.svg", page.to_owned()),
    ] {
        fs::write(assets.join(name), text).unwrap();
    }
    let mut manifest = pipeline::read_manifest(&dir).unwrap();
    manifest.assets = figures.iter().map(|f| f.2[0].to_owned()).collect();
    pipeline::write_manifest(&dir, &manifest).unwrap();

    let (out, result) = run_note(&fx, "03-note").await;
    result.unwrap();
    assert_eq!(
        listed(&read_note(&out)),
        ["Figure 1: A plot.", "Figure 2: Hostile."]
    );
    let images = sent_images(&fx);
    let sizes: Vec<Option<(u32, u32)>> = images.iter().map(sent_size).collect();
    // 200pt x 100pt and a million square, both drawn 1568 wide.
    assert_eq!(sizes, [Some((1568, 784)), Some((1568, 1568))]);
    // The thin drawing is tried both ways; the other three never reach ffmpeg.
    let calls = ffmpeg_calls(&fx);
    assert_eq!(calls.len(), 4, "{calls:#?}");
    for call in &calls {
        let (call, env_names) = call.split_once(" | env ").unwrap();
        assert!(
            call.contains(" -protocol_whitelist pipe -f svg_pipe -frame_size "),
            "{call}"
        );
        assert!(
            call.contains(" -max_pixels 4917248 -i pipe:0 -vf "),
            "{call}"
        );
        // The drawing has no file name: nothing it references can resolve next to it.
        assert!(!call.contains("assets"), "{call}");
        assert!(call.ends_with(".png"), "{call}");
        let env_names: Vec<&str> = env_names.split(',').collect();
        assert!(env_names.contains(&"PATH") && !env_names.contains(&"HOME"));
    }
    assert!(calls[2].contains(" -width 1568 ") && calls[3].contains(" -height 1568 "));
}

#[tokio::test]
async fn image_type_comes_from_the_bytes_not_the_name() {
    let figures: [(&str, &str, &[&str]); 2] = [
        ("Figure 1", "A JPEG named png.", &["photo.png"]),
        ("Figure 2", "An HTML page named png.", &["page.png"]),
    ];
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let dir = paper(&fx, &figures, &["photo.png", "page.png"]);
    real_images(&dir, &["photo.png"], "jpg");
    fs::write(dir.join("assets/page.png"), "<!DOCTYPE html><html></html>").unwrap();
    let (out, result) = run_note(&fx, "03-note").await;
    result.unwrap();
    let images = sent_images(&fx);
    assert_eq!(images.len(), 1);
    assert_eq!(images[0]["media_type"], "image/jpeg");
    assert_eq!(listed(&read_note(&out)), ["Figure 1: A JPEG named png."]);
}

#[tokio::test]
async fn an_image_still_too_large_after_shrinking_is_left_out() {
    // 1.1 MB at 100x100: ffmpeg keeps the size, so the copy is just as heavy.
    let figures: [(&str, &str, &[&str]); 2] = [
        ("Figure 1", "Heavy.", &["heavy.jpg"]),
        ("Figure 2", "Fine.", &["small.png"]),
    ];
    let fx = Fixture::new(Pipeline::Arxiv, AiTool::Claude);
    let dir = paper(&fx, &figures, &["heavy.jpg", "small.png"]);
    let mut heavy = image_bytes("jpg", 100, 100);
    heavy.resize(1_100_000, 0);
    fs::write(dir.join("assets/heavy.jpg"), heavy).unwrap();
    let (out, result) = run_note(&fx, "03-note").await;
    result.unwrap();
    assert_eq!(ffmpeg_calls(&fx).len(), 1);
    assert!(ffmpeg_calls(&fx)[0].contains("-f jpeg_pipe -i "));
    assert_eq!(sent_images(&fx).len(), 1);
    assert_eq!(listed(&read_note(&out)), ["Figure 2: Fine."]);
}

/// Raw RGB bytes of an image, decoded by the real ffmpeg.
fn rgb(path: &Path) -> Vec<u8> {
    let output = std::process::Command::new("ffmpeg")
        .args(["-v", "error", "-i"])
        .arg(path)
        .args(["-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        .output()
        .unwrap();
    assert!(output.status.success());
    output.stdout
}

/// The image's ffmpeg with its librsvg decoder; run the ignored tests inside flori:local.
#[tokio::test]
#[ignore = "needs ffmpeg with librsvg"]
async fn real_ffmpeg_draws_svg_on_white_without_its_references() {
    let dir = tempfile::tempdir().unwrap();
    // A red picture next to the drawing, where hostile.svg's relative references point.
    let made = std::process::Command::new("ffmpeg")
        .args(["-v", "error", "-f", "lavfi", "-i", "color=red:s=16x16"])
        .args(["-frames:v", "1", "-update", "1"])
        .arg(dir.path().join("secret.png"))
        .status()
        .unwrap();
    assert!(made.success());
    let env = crate::ai::inherited();
    let draw = |name: &str, text: String| {
        let path = dir.path().join(name);
        fs::write(&path, text).unwrap();
        let (env, dir) = (env.clone(), dir.path().to_owned());
        async move { rasterize(&env, &path, FIGURE_SIDE, &dir, 0).await }
    };

    let png = draw("hostile.svg", svg_fixture("hostile.svg"))
        .await
        .unwrap();
    assert_eq!(image_size(&fs::read(&png).unwrap()), Some((1568, 1568)));
    // Nothing it references was drawn: every pixel is the white background.
    assert!(rgb(&png).iter().all(|&b| b == 255));

    let png = draw("figure.svg", svg_fixture("figure.svg")).await.unwrap();
    assert_eq!(image_size(&fs::read(&png).unwrap()), Some((1568, 784)));
    let pixels = rgb(&png);
    // Transparent areas come out white, the drawing itself does not.
    assert_eq!(pixels[..3], [255, 255, 255]);
    assert!(pixels.iter().any(|&b| b < 128));

    let thin = r#"<svg xmlns="http://www.w3.org/2000/svg" width="1" height="100000000"/>"#;
    let failure = draw("thin.svg", thin.to_owned())
        .await
        .unwrap_err()
        .to_string();
    assert!(failure.contains("is invalid"), "{failure}");
}
