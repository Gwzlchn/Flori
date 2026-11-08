use std::path::PathBuf;

use super::{fetch::*, frames::*, transcribe::*, *};
use crate::pipeline::{Manifest, MetaValue, StepExit, write_manifest};

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/video")
        .join(name)
}

fn segment(start_ms: u64, end_ms: u64, text: &str) -> Segment {
    Segment {
        start_ms,
        end_ms,
        text: text.into(),
    }
}

fn exit_of(failure: Option<StepFailure>) -> Option<StepExit> {
    failure.map(|f| f.exit)
}

fn errors(lines: &[&str]) -> Vec<String> {
    lines.iter().map(|l| l.to_string()).collect()
}

#[test]
fn srt_fixture_parses_into_segments() {
    let text = fs::read_to_string(fixture("local-video.srt")).unwrap();
    assert_eq!(
        parse_srt(&text),
        [
            segment(0, 1000, "A source creates one pipeline job."),
            segment(1000, 2000, "Each task emits only declared artifacts."),
            segment(
                2000,
                3000,
                "Successful publication rotates current and previous."
            ),
        ]
    );
}

#[test]
fn srt_parser_tolerates_real_world_noise() {
    let text = "\u{feff}1\r\n00:00:01,200 --> 00:00:03,360 X1:10 X2:20\r\n<i>All right,</i> so\r\nhere   we are\r\n  \r\n2\r\n00:00:04.5 --> 00:00:05,000\r\n{\\an8}第一行\r\n第二行\r\n\r\n3\r\n00:00:06,000 --> 00:00:07,000\r\n<font color=\"red\"></font>\r\n\r\n4\r\nbroken --> line\r\nignored\r\n\r\n5\r\n01:02,000 --> 01:01,000\r\n42\r\n";
    assert_eq!(
        parse_srt(text),
        [
            segment(1200, 3360, "All right, so here we are"),
            segment(4500, 5000, "第一行第二行"),
            segment(62_000, 62_000, "42"),
        ]
    );
}

#[test]
fn srt_time_rejects_garbage() {
    assert_eq!(parse_srt_time("01:02:03,004"), Some(3_723_004));
    assert_eq!(parse_srt_time(" 1:02:03.4 "), Some(3_723_400));
    assert_eq!(parse_srt_time("01:02:03"), None);
    assert_eq!(parse_srt_time("1:-2:03,000"), None);
    assert_eq!(parse_srt_time("00:00:01,0000"), None);
}

#[test]
fn text_join_spaces_only_between_latin_words() {
    let mut out = String::new();
    push_text(&mut out, "Hello");
    push_text(&mut out, "  world ");
    push_text(&mut out, "你好");
    push_text(&mut out, "世界。");
    push_text(&mut out, "Rust");
    push_text(&mut out, "");
    assert_eq!(out, "Hello world你好世界。Rust");
}

#[test]
fn subtitle_rank_prefers_simplified_chinese_then_english() {
    let mut langs = ["en", "zh-Hant", "de", "zh-Hans", "en-US", "ai-zh", "zh-TW"];
    langs.sort_by_key(|lang| (subtitle_rank(lang).unwrap_or(u8::MAX), *lang));
    assert_eq!(
        langs,
        ["zh-Hans", "zh-Hant", "zh-TW", "en", "en-US", "ai-zh", "de"]
    );
    assert_eq!(subtitle_rank("ai-zh"), None);
}

#[test]
fn best_usable_subtitle_is_kept_and_the_rest_deleted() {
    let dir = tempfile::tempdir().unwrap();
    let cue = "1\n00:00:00,000 --> 00:00:01,000\nhello\n";
    fs::write(dir.path().join("subtitle.zh-Hans.srt"), "1\n\n").unwrap();
    fs::write(dir.path().join("subtitle.en.srt"), cue).unwrap();
    fs::write(dir.path().join("subtitle.de.srt"), cue).unwrap();
    fs::write(dir.path().join("video.mp4"), "x").unwrap();
    assert_eq!(
        keep_best_subtitle(dir.path()).unwrap().as_deref(),
        Some("en")
    );
    let mut names: Vec<_> = fs::read_dir(dir.path())
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    names.sort();
    assert_eq!(names, ["subtitle.srt", "video.mp4"]);
    assert_eq!(
        fs::read_to_string(dir.path().join("subtitle.srt")).unwrap(),
        cue
    );

    let empty = tempfile::tempdir().unwrap();
    assert_eq!(keep_best_subtitle(empty.path()).unwrap(), None);
}

#[test]
fn ytdlp_errors_are_classified() {
    let permanent = [
        "ERROR: [youtube] aaaaaaaaaaa: This video is unavailable",
        "ERROR: [youtube] x: Private video. Sign in if you've been granted access to this video",
        "ERROR: [BiliBili] 1AB411c7mQ: This video may be deleted or geo-restricted. You might want to try a VPN or a proxy server (with --proxy)",
        "ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate for some users.",
        "ERROR: [BiliBili] BV1x: This video is for premium members only. Use --cookies-from-browser or --cookies for the authentication.",
        "ERROR: [youtube] x: The uploader has not made this video available in your country",
    ];
    for line in permanent {
        assert_eq!(
            exit_of(classify_ytdlp(false, &errors(&[line]))),
            Some(StepExit::Permanent),
            "{line}"
        );
    }
    let retry = [
        "ERROR: [youtube] jNQXAC9IVRw: Unable to download API page: [Errno 111] Connection refused (caused by TransportError('[Errno 111] Connection refused'))",
        "ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies-from-browser or --cookies for the authentication.",
        "ERROR: [BiliBili] BV1x: Unable to download webpage: HTTP Error 412: Precondition Failed",
        "ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
    ];
    for line in retry {
        assert_eq!(
            exit_of(classify_ytdlp(false, &errors(&[line]))),
            Some(StepExit::Retry),
            "{line}"
        );
    }
    // Killed or crashed without an ERROR line.
    assert_eq!(exit_of(classify_ytdlp(false, &[])), Some(StepExit::Retry));
    // The last ERROR line decides.
    let mixed = errors(&[
        "ERROR: [youtube] x: HTTP Error 429: Too Many Requests",
        "ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader",
    ]);
    assert_eq!(
        exit_of(classify_ytdlp(false, &mixed)),
        Some(StepExit::Permanent)
    );
}

#[test]
fn bilibili_nav_reply_decides_the_login_state() {
    let exit = |body: &str| login_state(body.as_bytes()).err().map(|f| f.exit);
    assert_eq!(
        exit(r#"{"code":0,"data":{"isLogin":true,"uname":"x"}}"#),
        None
    );
    assert_eq!(
        exit(r#"{"code":-101,"message":"账号未登录","ttl":1,"data":{"isLogin":false}}"#),
        Some(StepExit::Auth)
    );
    assert_eq!(
        exit(r#"{"code":-412,"message":"请求被拦截"}"#),
        Some(StepExit::Retry)
    );
    assert_eq!(exit("<html>412</html>"), Some(StepExit::Retry));
}

#[test]
fn malformed_sessdata_is_rejected_before_any_file_is_written() {
    let failure = write_cookies(1, "abc\ndef").unwrap_err();
    assert_eq!(failure.exit, StepExit::Auth);
}

#[test]
fn published_date_needs_eight_digits() {
    assert_eq!(published_date("20050424").as_deref(), Some("2005-04-24"));
    assert_eq!(published_date("2005042"), None);
    assert_eq!(published_date("2005-04-2"), None);
}

#[test]
fn showinfo_lines_give_candidate_times() {
    let stderr = "  Duration: 00:00:03.00, start: 0.000000, bitrate: 111 kb/s\n\
        [Parsed_showinfo_2 @ 0x55] n:   0 pts:      0 pts_time:0       pos: 2649 fmt:yuv420p\n\
        [Parsed_showinfo_2 @ 0x55] color_range:unknown color_space:unknown\n\
        [Parsed_showinfo_2 @ 0x55] n:   1 pts:  12800 pts_time:12.3456 pos: 9 fmt:yuv420p\n\
        [Parsed_showinfo_2 @ 0x55] n:   2 pts:  -10 pts_time:-0.021 pos: 9 fmt:yuv420p\n\
        frame=    1 fps=0.0 q=-0.0 Lsize=       1kB time=00:00:00.10\n";
    assert_eq!(showinfo_times(stderr), [0, 12_346, 0]);
}

/// Asymmetric 32x32 test frame: a sawtooth gradient and a bright block, both moved by `seed`.
/// Synthetic symmetric patterns make most DCT terms zero and the hash unstable.
fn slide(seed: usize) -> Vec<u8> {
    let (bx, by) = (seed * 13 % 20, seed * 7 % 20);
    (0..THUMB_SIDE * THUMB_SIDE)
        .map(|i| {
            let (x, y) = (i % THUMB_SIDE, i / THUMB_SIDE);
            if (bx..bx + 10).contains(&x) && (by..by + 8).contains(&y) {
                250
            } else {
                ((x * (3 + seed) + y * (5 + 2 * seed)) % 200) as u8
            }
        })
        .collect()
}

/// The same frame after a light re-encode: every 7th pixel off by up to 6.
fn noisy(pixels: &[u8]) -> Vec<u8> {
    pixels
        .iter()
        .enumerate()
        .map(|(i, &p)| {
            if i % 7 == 0 {
                p.saturating_add((i % 13) as u8 / 2)
            } else {
                p
            }
        })
        .collect()
}

#[test]
fn similarity_separates_equal_and_different_frames() {
    let (a, b) = (slide(0), slide(1));
    assert!((ssim(&a, &a) - 1.0).abs() < 1e-9);
    assert!(ssim(&a, &noisy(&a)) > SSIM_DUPLICATE);
    assert!(ssim(&a, &b) < 0.5);
    let (ha, hb) = (perceptual_hash(&a).unwrap(), perceptual_hash(&b).unwrap());
    assert!((ha ^ perceptual_hash(&noisy(&a)).unwrap()).count_ones() <= PHASH_DUPLICATE);
    assert!((ha ^ hb).count_ones() > PHASH_SIMILAR);
    assert_eq!(perceptual_hash(&[0; 10]), None);
}

#[test]
fn keyframes_skip_flat_bursts_and_duplicates() {
    let (slide_a, slide_b) = (slide(0), slide(1));
    let black = vec![0; THUMB_SIDE * THUMB_SIDE];
    let candidates = vec![
        (0, black.clone()),        // flat
        (2_000, slide_a.clone()),  // kept
        (3_000, slide_b.clone()),  // within 2s of the previous candidate
        (15_000, noisy(&slide_a)), // duplicate of slide_a
        (30_000, slide_b.clone()), // kept
        (45_000, slide_a.clone()), // a returning slide is still a duplicate
        (60_000, black),           // flat
        (75_000, slide(2)),        // kept
    ];
    assert_eq!(pick_keyframes(&candidates), [2_000, 30_000, 75_000]);
}

#[test]
fn keyframe_cap_keeps_timeline_ends() {
    assert_eq!(evenly_bounded(&[1, 2, 3, 4, 5], 3), [1, 3, 5]);
    assert_eq!(evenly_bounded(&[1, 2], 3), [1, 2]);
    let many: Vec<u64> = (0..200).collect();
    let capped = evenly_bounded(&many, MAX_FRAMES);
    assert_eq!(capped.len(), MAX_FRAMES);
    assert_eq!((capped[0], capped[MAX_FRAMES - 1]), (0, 199));
}

#[test]
fn sections_group_text_and_frames_by_thirty_seconds() {
    let segments = [
        segment(0, 4_000, "Hello"),
        segment(29_999, 31_000, "there."),
        segment(30_000, 32_000, "Next"),
        segment(95_000, 99_000, "第三节"),
    ];
    let frames = [
        (1_000, "frame-1000.jpg".to_string()),
        (61_000, "frame-61000.jpg".to_string()),
        (62_000, "frame-62000.jpg".to_string()),
    ];
    let sections = build_sections(&segments, &frames);
    let expected = [
        (0, vec!["frame-1000.jpg"], "Hello there."),
        (30_000, vec![], "Next"),
        (60_000, vec!["frame-61000.jpg", "frame-62000.jpg"], ""),
        (90_000, vec![], "第三节"),
    ];
    assert_eq!(sections.len(), expected.len());
    for (section, (start_ms, frames, text)) in sections.iter().zip(expected) {
        assert_eq!(section.start_ms, start_ms);
        assert_eq!(section.frames, frames);
        assert_eq!(section.text, text);
    }
}

#[test]
fn transcript_markdown_has_timestamps_frames_and_escaped_text() {
    let sections = [
        Section {
            start_ms: 0,
            frames: vec!["frame-0.jpg".into(), "frame-12000.jpg".into()],
            text: "It costs $5 and $10, see [1] or #rust *now*.".into(),
        },
        Section {
            start_ms: 3_630_000,
            frames: vec![],
            text: "- not a list".into(),
        },
    ];
    assert_eq!(
        render_transcript(&sections),
        "## 00:00\n\n\
         ![](assets/frame-0.jpg)\n\n\
         ![](assets/frame-12000.jpg)\n\n\
         It costs \\$5 and \\$10, see \\[1\\] or \\#rust \\*now\\*.\n\n\
         ## 1:00:30\n\n\
         \\- not a list\n"
    );
    assert_eq!(render_transcript(&[]), "\n");
}

#[test]
fn transcript_json_matches_the_contract_shape() {
    let transcript = Transcript {
        language: None,
        segments: vec![segment(0, 1, "a")],
    };
    assert_eq!(
        serde_json::to_value(&transcript).unwrap(),
        serde_json::json!({"language": null, "segments": [{"start_ms": 0, "end_ms": 1, "text": "a"}]})
    );
    let whisper: WhisperOutput = serde_json::from_str(
        r#"{"language":"en","language_probability":0.97,"segments":[{"start_ms":0,"end_ms":1,"text":"a"}]}"#,
    )
    .unwrap();
    assert_eq!(whisper.segments, transcript.segments);
}

/// Job dir with job.json and a finished video-fetch step holding the fixture video and,
/// optionally, its subtitle.
fn fixture_job(with_subtitle: bool) -> tempfile::TempDir {
    let job = tempfile::tempdir().unwrap();
    fs::write(
        job.path().join("job.json"),
        r#"{"job_id":7,"pipeline":"video","source":{"platform":"youtube","id":"jNQXAC9IVRw","url":"https://www.youtube.com/watch?v=jNQXAC9IVRw"},"domain":"ml","collection":null,"translate":"auto","ai":{"tool":"claude","model_note":"m","model_translate":"m","cli_version":"1"},"created":"2026-11-08T10:00:00+08:00","public_url":"http://nas.lan:8780","vault_name":"flori"}"#,
    )
    .unwrap();
    let fetch = job.path().join("steps/01-video-fetch");
    fs::create_dir_all(&fetch).unwrap();
    fs::copy(fixture("local-video.mp4"), fetch.join(VIDEO_FILE)).unwrap();
    let mut manifest = Manifest {
        title: Some("local".into()),
        ..Manifest::default()
    };
    manifest.files.insert("video".into(), VIDEO_FILE.into());
    if with_subtitle {
        fs::copy(fixture("local-video.srt"), fetch.join(SUBTITLE_FILE)).unwrap();
        manifest
            .files
            .insert("subtitle".into(), SUBTITLE_FILE.into());
        manifest
            .meta
            .insert("subtitle_language".into(), MetaValue::Text("en".into()));
    }
    write_manifest(&fetch, &manifest).unwrap();
    job
}

/// Runs a step into `steps/<nn>-<name>.tmp` and renames it like the executor does.
async fn run_into(job: &Path, dir: &str, step: StepName) -> PathBuf {
    let tmp = job.join(format!("steps/{dir}.tmp"));
    fs::create_dir(&tmp).unwrap();
    let result = match step {
        StepName::VideoTranscribe => transcribe(job, &tmp).await,
        StepName::VideoFrames => frames(job, &tmp).await,
        _ => unreachable!(),
    };
    if let Err(failure) = result {
        panic!("{step} failed: {:?} {}", failure.exit, failure.message);
    }
    let done = job.join(format!("steps/{dir}"));
    fs::rename(&tmp, &done).unwrap();
    done
}

/// Needs ffmpeg on PATH, so it runs inside the tools image.
#[tokio::test]
#[ignore = "needs ffmpeg"]
async fn fixture_runs_transcribe_and_frames() {
    let job = fixture_job(true);
    let transcribed = run_into(job.path(), "02-video-transcribe", StepName::VideoTranscribe).await;
    let manifest = pipeline::read_manifest(&transcribed).unwrap();
    assert_eq!(
        manifest.meta.get("origin"),
        Some(&MetaValue::Text("subtitle".into()))
    );
    assert_eq!(
        manifest.meta.get("language"),
        Some(&MetaValue::Text("en".into()))
    );
    let transcript: Transcript = read_json(&transcribed.join(TRANSCRIPT_JSON)).unwrap();
    assert_eq!(transcript.segments.len(), 3);

    let framed = run_into(job.path(), "03-video-frames", StepName::VideoFrames).await;
    let manifest = pipeline::read_manifest(&framed).unwrap();
    assert_eq!(manifest.files["transcript"], "transcript.md");
    assert_eq!(manifest.files["sections"], "sections.json");
    assert!(!manifest.assets.is_empty());
    for asset in &manifest.assets {
        let bytes = fs::read(framed.join("assets").join(asset)).unwrap();
        assert_eq!(&bytes[..2], [0xFF, 0xD8], "{asset} is not a JPEG");
    }
    let sections: Vec<Section> = read_json(&framed.join("sections.json")).unwrap();
    assert_eq!(sections.len(), 1);
    assert_eq!(sections[0].frames, manifest.assets);
    assert!(
        sections[0]
            .text
            .starts_with("A source creates one pipeline job.")
    );
    let md = fs::read_to_string(framed.join("transcript.md")).unwrap();
    assert!(md.starts_with(&format!(
        "## 00:00\n\n![](assets/{})\n\n",
        manifest.assets[0]
    )));
    let mut left: Vec<_> = fs::read_dir(&framed)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    left.sort();
    assert_eq!(
        left,
        ["assets", "manifest.json", "sections.json", "transcript.md"]
    );
}

/// Needs ffmpeg, python3 with faster-whisper, FLORI_STEPS_DIR and FLORI_WHISPER_MODEL.
#[tokio::test]
#[ignore = "needs ffmpeg and faster-whisper"]
async fn fixture_runs_whisper_without_subtitle() {
    let job = fixture_job(false);
    let transcribed = run_into(job.path(), "02-video-transcribe", StepName::VideoTranscribe).await;
    let manifest = pipeline::read_manifest(&transcribed).unwrap();
    assert_eq!(
        manifest.meta.get("origin"),
        Some(&MetaValue::Text("whisper".into()))
    );
    assert!(matches!(
        manifest.meta.get("language_probability"),
        Some(MetaValue::Float(p)) if (0.0..=1.0).contains(p)
    ));
    let _: Transcript = read_json(&transcribed.join(TRANSCRIPT_JSON)).unwrap();
    let mut left: Vec<_> = fs::read_dir(&transcribed)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    left.sort();
    assert_eq!(left, ["manifest.json", "transcript.json"]);
}
