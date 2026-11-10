use std::{
    fs,
    path::{Path, PathBuf},
    time::Duration,
};

use super::{fetch::*, frames::*, transcribe::*, *};
use crate::pipeline::{
    self, Manifest, MetaValue, Section, StepExit, StepName, StepResult, read_json, write_manifest,
};

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
fn subtitle_markup_is_stripped_but_spoken_brackets_stay() {
    let text = "1\n00:00:00,000 --> 00:00:01,000\n<i>use</i> Vec<String> here\n\n\
        2\n00:00:01,000 --> 00:00:02,000\n{\\an8}if a < b and c > d\n\n\
        3\n00:00:02,000 --> 00:00:03,000\n<c.colorE5E5E5>{key: value}</c> <b>bold</b>\n\n\
        4\n00:00:03,000 --> 00:00:04,000\n<font color=\"red\">fn main() { run() }</font>\n";
    let texts: Vec<String> = parse_srt(text).into_iter().map(|s| s.text).collect();
    assert_eq!(
        texts,
        [
            "use Vec<String> here",
            "if a < b and c > d",
            "{key: value} bold",
            "fn main() { run() }",
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
    // Korean separates words with spaces.
    let mut korean = String::from("안녕하세요");
    push_text(&mut korean, "여러분");
    assert_eq!(korean, "안녕하세요 여러분");
}

#[test]
fn subtitle_rank_puts_the_original_first() {
    let sorted = |original: Option<&str>| {
        let mut langs = [
            "en", "zh-Hant", "de", "zh-Hans", "en-US", "ai-zh", "zh-TW", "zh",
        ];
        langs.sort_by_key(|lang| (subtitle_rank(lang, original), *lang));
        langs
    };
    // An unknown or a Chinese original: Chinese, then English.
    let chinese_first = [
        "zh", "zh-Hans", "zh-Hant", "zh-TW", "en", "en-US", "de", "ai-zh",
    ];
    assert_eq!(sorted(None), chinese_first);
    assert_eq!(sorted(Some("zh")), chinese_first);
    assert_eq!(subtitle_rank("de", Some("de")).0, 0);
    // Any other known original takes only its own language: neither Chinese nor English.
    assert_eq!(subtitle_rank("zh-Hans", Some("en")).0, u8::MAX);
    assert_eq!(subtitle_rank("en", Some("de")).0, u8::MAX);
    assert_eq!(subtitle_rank("ai-zh", None).0, u8::MAX);
}

/// Writes one-cue `subtitle.<lang>.srt` files (an empty text makes an unusable track) and runs
/// keep_subtitles; returns what it reported and the files left behind.
fn keep(original: Option<&str>, tracks: &[(&str, &str)]) -> ((Option<String>, bool), Vec<String>) {
    let dir = tempfile::tempdir().unwrap();
    for (lang, text) in tracks {
        let srt = format!("1\n00:00:00,000 --> 00:00:01,000\n{text}\n");
        fs::write(dir.path().join(format!("subtitle.{lang}.srt")), srt).unwrap();
    }
    fs::write(dir.path().join("video.mp4"), "x").unwrap();
    // Left by a subtitle run that failed before converting.
    fs::write(dir.path().join("video.en.vtt"), "partial").unwrap();
    let kept = keep_subtitles(dir.path(), original).unwrap();
    let mut left = names(dir.path());
    for file in ["subtitle.srt", "subtitle_zh.srt"] {
        if let Ok(text) = fs::read_to_string(dir.path().join(file)) {
            let cue = text.lines().nth(2).unwrap().to_owned();
            left.iter_mut()
                .filter(|n| *n == file)
                .for_each(|n| *n = format!("{file}={cue}"));
        }
    }
    (kept, left)
}

#[test]
fn subtitles_follow_the_original_language() {
    let both = [
        ("zh-Hans", "你好"),
        ("zh-Hant", "哈囉"),
        ("en", "hello"),
        ("de", "hallo"),
    ];
    let en_and_zh = (
        (Some("en".to_owned()), true),
        vec![
            "subtitle.srt=hello".to_owned(),
            "subtitle_zh.srt=你好".to_owned(),
            "video.mp4".to_owned(),
        ],
    );
    // An English original: Chinese is the translation, not the transcript.
    assert_eq!(keep(Some("en"), &both), en_and_zh);
    // A Chinese or an unknown original takes the Chinese track and has no translation.
    let zh_only = (
        (Some("zh".to_owned()), false),
        vec!["subtitle.srt=你好".to_owned(), "video.mp4".to_owned()],
    );
    assert_eq!(keep(Some("zh"), &both), zh_only);
    assert_eq!(keep(None, &both), zh_only);
    // An unknown original with only English takes it.
    let en_only = (
        (Some("en".to_owned()), false),
        vec!["subtitle.srt=hello".to_owned(), "video.mp4".to_owned()],
    );
    assert_eq!(keep(None, &[("en", "hello")]), en_only);
    // Only a Chinese track of an English video: whisper transcribes, the track translates.
    let translation_only = (
        (None, true),
        vec!["subtitle_zh.srt=你好".to_owned(), "video.mp4".to_owned()],
    );
    assert_eq!(keep(Some("en"), &[("zh-Hans", "你好")]), translation_only);
    // Unknown original with only Chinese: it is the transcript.
    assert_eq!(keep(None, &[("zh-Hans", "你好")]), zh_only);
    // A German original without a German track: English is not a fallback, whisper transcribes.
    let de = [("en", "hello"), ("zh-Hans", "你好")];
    assert_eq!(keep(Some("de"), &de), translation_only);
    // A German original takes its own track; an unusable track never wins.
    let german = (
        (Some("de".to_owned()), true),
        vec![
            "subtitle.srt=hallo".to_owned(),
            "subtitle_zh.srt=你好".to_owned(),
            "video.mp4".to_owned(),
        ],
    );
    assert_eq!(keep(Some("de"), &both), german);
    assert_eq!(
        keep(None, &[("en", ""), ("zh-Hans", "你好")]),
        zh_only,
        "an empty English track is skipped"
    );
    assert_eq!(
        keep(None, &[]),
        ((None, false), vec!["video.mp4".to_owned()])
    );
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
        "ERROR: [BiliBili] BV1x: This video is only available for registered users. Use --cookies-from-browser or --cookies for the authentication.",
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
        // Throttling worded as "Video unavailable" must not become permanent.
        "ERROR: [youtube] x: Video unavailable. This content isn't available, try again later. The current session has been rate-limited by YouTube for up to an hour. It is recommended to use `-t sleep` to add a delay between video requests to avoid exceeding the rate limit.",
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
    let tmp = tempfile::tempdir().unwrap();
    let failure = CookieFile::write(tmp.path(), 1, "abc\ndef").err().unwrap();
    assert_eq!(failure.exit, StepExit::Auth);
    assert_eq!(fs::read_dir(tmp.path()).unwrap().count(), 0);
}

#[test]
fn cookie_file_is_private_replaces_leftovers_and_goes_away_on_drop() {
    use std::os::unix::fs::PermissionsExt;
    let tmp = tempfile::tempdir().unwrap();
    let path = tmp.path().join("flori-bili-7.cookies");
    fs::write(&path, "stale").unwrap();
    let cookie = CookieFile::write(tmp.path(), 7, "abc%2C123").unwrap();
    assert_eq!(cookie.0, path);
    let text = fs::read_to_string(&path).unwrap();
    assert!(text.ends_with(".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tabc%2C123\n"));
    let mode = fs::metadata(&path).unwrap().permissions().mode();
    assert_eq!(mode & 0o777, 0o600);
    drop(cookie);
    assert!(!path.exists());
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
    let white = vec![255; THUMB_SIDE * THUMB_SIDE];
    let candidates = vec![
        (0, black.clone()),        // flat, and opens no gap window
        (1_000, slide_a.clone()),  // kept
        (2_000, slide_b.clone()),  // within 2s of the last kept frame
        (15_000, noisy(&slide_a)), // duplicate of slide_a
        (30_000, slide_b.clone()), // kept
        (45_000, slide_a.clone()), // a returning slide is still a duplicate
        (60_000, white),           // flash
        (61_000, slide(2)),        // kept right after the flash
    ];
    assert_eq!(pick_keyframes(&candidates), [1_000, 30_000, 61_000]);
}

/// `base` with a rectangle shifted by `amp` and a ±`checker` checkerboard on top, clamped.
fn edited(base: &[u8], rect: (usize, usize, usize, usize), amp: i32, checker: i32) -> Vec<u8> {
    let (x0, y0, w, h) = rect;
    base.iter()
        .enumerate()
        .map(|(i, &p)| {
            let (x, y) = (i % THUMB_SIDE, i / THUMB_SIDE);
            let mut v = i32::from(p);
            if (x0..x0 + w).contains(&x) && (y0..y0 + h).contains(&y) {
                v += amp;
            }
            v += if (x + y) % 2 == 1 { checker } else { -checker };
            v.clamp(0, 255) as u8
        })
        .collect()
}

#[test]
fn moderately_close_hashes_are_decided_by_ssim() {
    let base = slide(0);
    let distance =
        |b: &[u8]| (perceptual_hash(&base).unwrap() ^ perceptual_hash(b).unwrap()).count_ones();
    // A dark band along the bottom edge: the hash moves into the middle band, the picture
    // stays the same, so SSIM calls it a duplicate.
    let shaded = edited(&base, (18, 27, 14, 14), -104, 0);
    // A bright block plus heavy texture: same hash band, a different picture.
    let changed = edited(&base, (22, 16, 8, 10), 92, 58);
    for frame in [&shaded, &changed] {
        assert!(
            (PHASH_DUPLICATE + 1..=PHASH_SIMILAR).contains(&distance(frame)),
            "distance {}",
            distance(frame)
        );
    }
    assert!(ssim(&base, &shaded) >= SSIM_DUPLICATE);
    assert!(ssim(&base, &changed) < SSIM_DUPLICATE);
    assert_eq!(pick_keyframes(&[(0, base.clone()), (5_000, shaded)]), [0]);
    assert_eq!(pick_keyframes(&[(0, base), (5_000, changed)]), [0, 5_000]);
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
            start_ms: 60_000,
            frames: vec![],
            // A tilde fence would swallow every later heading; %% hides text in Obsidian.
            text: "~~~ 音乐 ~~~ 你好 100%% sure".into(),
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
         ## 01:00\n\n\
         \\~~~ 音乐 ~~~ 你好 100\\%\\% sure\n\n\
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

fn job_json(platform: &str, id: &str) -> String {
    serde_json::json!({
        "job_id": 7,
        "pipeline": "video",
        "source": {"platform": platform, "id": id, "url": "https://example.invalid/"},
        "domain": "ml",
        "collection": null,
        "translate": "auto",
        "ai": {"tool": "claude", "model_note": "m", "model_translate": "m", "cli_version": "1"},
        "created": "2026-11-08T10:00:00+08:00",
        "public_url": "http://nas.lan:8780",
        "vault_name": "flori",
    })
    .to_string()
}

fn names(dir: &Path) -> Vec<String> {
    let mut names: Vec<_> = fs::read_dir(dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    names.sort();
    names
}

/// A video-fetch run against tests/fixtures/video/fake/{yt-dlp,curl}. The fake yt-dlp acts on
/// words in the video id and logs its arguments to `steps/fake-yt-dlp.log`.
struct FetchRun {
    job: tempfile::TempDir,
    tmp: tempfile::TempDir,
    env: FetchEnv,
}

impl FetchRun {
    fn new(platform: &str, id: &str, sessdata: Option<&str>) -> FetchRun {
        let job = tempfile::tempdir().unwrap();
        fs::write(job.path().join("job.json"), job_json(platform, id)).unwrap();
        fs::create_dir_all(job.path().join("steps/01-video-fetch.tmp")).unwrap();
        let tmp = tempfile::tempdir().unwrap();
        let env = FetchEnv {
            proxy: None,
            sessdata: sessdata.map(Into::into),
            tmp_dir: tmp.path().into(),
            ytdlp: fixture("fake/yt-dlp").into(),
            curl: fixture("fake/curl").into(),
        };
        FetchRun { job, tmp, env }
    }

    fn out(&self) -> PathBuf {
        self.job.path().join("steps/01-video-fetch.tmp")
    }

    async fn run(&self) -> StepResult {
        fetch_with(self.job.path(), &self.out(), &self.env).await
    }

    fn log(&self) -> String {
        fs::read_to_string(self.job.path().join("steps/fake-yt-dlp.log")).unwrap_or_default()
    }

    fn manifest(&self) -> Manifest {
        pipeline::read_manifest(&self.out()).unwrap()
    }
}

#[tokio::test]
async fn failed_subtitles_do_not_cost_the_video() {
    for id in ["video-subs-429", "video-subs-404"] {
        let run = FetchRun::new("youtube", id, None);
        run.run().await.unwrap();
        assert_eq!(names(&run.out()), ["manifest.json", "video.mp4"], "{id}");
        let manifest = run.manifest();
        assert_eq!(manifest.title.as_deref(), Some("Title"));
        assert_eq!(manifest.files.keys().collect::<Vec<_>>(), ["video"]);
        let meta = &manifest.meta;
        assert_eq!(meta["title"], MetaValue::Text("Title".into()));
        assert_eq!(meta["uploader"], MetaValue::Text("U".into()));
        assert_eq!(meta["duration_ms"], MetaValue::Int(12_500));
        assert_eq!(meta["published"], MetaValue::Text("2024-01-02".into()));
        assert!(!meta.contains_key("subtitle_language"));
    }
}

#[tokio::test]
async fn subtitle_is_kept_and_both_runs_skip_live_streams() {
    let run = FetchRun::new("youtube", "video-subs-ok", None);
    run.run().await.unwrap();
    assert_eq!(
        names(&run.out()),
        ["manifest.json", "subtitle.srt", "video.mp4"]
    );
    let manifest = run.manifest();
    assert_eq!(manifest.files["subtitle"], "subtitle.srt");
    assert_eq!(
        manifest.meta["subtitle_language"],
        MetaValue::Text("en".into())
    );
    let log = run.log();
    assert_eq!(log.lines().count(), 2);
    for line in log.lines() {
        assert!(line.contains(" --match-filter !is_live "), "{line}");
        assert!(line.contains("--playlist-items 1 "), "{line}");
        assert!(
            line.ends_with("-- https://www.youtube.com/watch?v=video-subs-ok"),
            "{line}"
        );
    }
}

#[tokio::test]
async fn original_language_picks_the_transcript_and_chinese_becomes_the_translation() {
    struct Case {
        platform: &'static str,
        id: &'static str,
        /// The cue in subtitle.srt and its meta.subtitle_language.
        subtitle: Option<(&'static str, &'static str)>,
        /// The cue in subtitle_zh.srt.
        chinese: Option<&'static str>,
        sub_langs: &'static str,
    }
    let case = |platform, id, subtitle, chinese, sub_langs| Case {
        platform,
        id,
        subtitle,
        chinese,
        sub_langs,
    };
    let cases = [
        case(
            "youtube",
            "video-lang-en-subs-both",
            Some(("hello", "en")),
            Some("你好"),
            "zh.*,en.*",
        ),
        // Unknown original: the Chinese track may be the original, so it is the transcript.
        case(
            "youtube",
            "video-subs-both",
            Some(("你好", "zh")),
            None,
            "zh.*,en.*",
        ),
        // No English track: whisper transcribes, the Chinese track is still the translation.
        case(
            "youtube",
            "video-lang-en-subs-zh",
            None,
            Some("你好"),
            "zh.*,en.*",
        ),
        case(
            "youtube",
            "video-lang-fr-subs-fr",
            Some(("bonjour", "fr")),
            Some("你好"),
            "fr.*,zh.*",
        ),
        // A French original with only English and Chinese tracks: English is never the
        // transcript, so it is not even requested.
        case(
            "youtube",
            "video-lang-fr-subs-both",
            None,
            Some("你好"),
            "fr.*,zh.*",
        ),
        // yt-dlp's Bilibili extractor sets no language; the unknown order takes Chinese.
        case(
            "bilibili",
            "BV-subs-both",
            Some(("你好", "zh")),
            None,
            "zh.*,en.*",
        ),
    ];
    for case in cases {
        let id = case.id;
        let run = FetchRun::new(case.platform, id, None);
        run.run().await.unwrap();
        let cue = |file: &str| {
            let text = fs::read_to_string(run.out().join(file)).ok()?;
            Some(text.lines().nth(2)?.to_owned())
        };
        assert_eq!(
            cue("subtitle.srt").as_deref(),
            case.subtitle.map(|s| s.0),
            "{id}"
        );
        assert_eq!(cue("subtitle_zh.srt").as_deref(), case.chinese, "{id}");
        let manifest = run.manifest();
        let file = |name: &str| manifest.files.get(name).map(String::as_str);
        assert_eq!(
            file("subtitle"),
            case.subtitle.map(|_| "subtitle.srt"),
            "{id}"
        );
        assert_eq!(
            file("subtitle_zh"),
            case.chinese.map(|_| "subtitle_zh.srt"),
            "{id}"
        );
        let language = case.subtitle.map(|s| MetaValue::Text(s.1.into()));
        assert_eq!(
            manifest.meta.get("subtitle_language"),
            language.as_ref(),
            "{id}"
        );
        let subtitle_run = run.log().lines().nth(1).unwrap().to_owned();
        let sub_langs = format!("--sub-langs {} ", case.sub_langs);
        assert!(subtitle_run.contains(&sub_langs), "{id}: {subtitle_run}");
    }
}

#[tokio::test]
async fn running_live_stream_is_a_readable_permanent_failure() {
    let run = FetchRun::new("youtube", "live", None);
    let failure = run.run().await.unwrap_err();
    assert_eq!(failure.exit, StepExit::Permanent);
    assert!(
        failure.message.contains("live stream"),
        "{}",
        failure.message
    );
}

#[tokio::test]
async fn multi_part_title_is_the_video_title_and_the_cookie_goes_away() {
    let run = FetchRun::new("bilibili", "BVmulti", Some("good-cookie"));
    run.run().await.unwrap();
    let manifest = run.manifest();
    assert_eq!(manifest.title.as_deref(), Some("Course"));
    assert_eq!(manifest.meta["title"], MetaValue::Text("Course".into()));
    let log = run.log();
    assert!(log.contains("--cookies "), "{log}");
    assert!(
        log.contains(" -- https://www.bilibili.com/video/BVmulti\n"),
        "{log}"
    );
    assert_eq!(names(run.tmp.path()), Vec::<String>::new());
}

/// Every file under `dir`, recursively.
fn all_files(dir: &Path) -> Vec<PathBuf> {
    let mut files = Vec::new();
    for entry in fs::read_dir(dir).unwrap() {
        let path = entry.unwrap().path();
        if path.is_dir() {
            files.extend(all_files(&path));
        } else {
            files.push(path);
        }
    }
    files
}

#[tokio::test]
async fn cookie_never_reaches_the_job_dir_and_is_removed_after_a_failure() {
    let run = FetchRun::new("bilibili", "BVgone", Some("secret-cookie"));
    let failure = run.run().await.unwrap_err();
    assert_eq!(failure.exit, StepExit::Permanent);
    assert!(run.log().contains("--cookies "));
    assert_eq!(names(run.tmp.path()), Vec::<String>::new());
    for file in all_files(run.job.path()) {
        let bytes = fs::read(&file).unwrap();
        assert!(
            !String::from_utf8_lossy(&bytes).contains("secret-cookie"),
            "{}",
            file.display()
        );
    }
}

#[tokio::test]
async fn expired_sessdata_exits_30_before_downloading() {
    let run = FetchRun::new("bilibili", "BVany", Some("expired"));
    let failure = run.run().await.unwrap_err();
    assert_eq!(failure.exit, StepExit::Auth);
    assert_eq!(run.log(), "");
    assert_eq!(names(run.tmp.path()), Vec::<String>::new());
}

/// The executor's SIGTERM kills yt-dlp too, and the step can see that before the signal.
#[tokio::test]
async fn a_subtitle_run_killed_by_a_signal_is_retried() {
    let run = FetchRun::new("bilibili", "BVsubs-killed", Some("good-cookie"));
    let failure = run.run().await.unwrap_err();
    assert_eq!(failure.exit, StepExit::Retry, "{}", failure.message);
    assert_eq!(names(&run.out()), ["video.mp4"]);
    assert_eq!(names(run.tmp.path()), Vec::<String>::new());
}

/// What SIGTERM does in `fetch`: the download future is dropped mid-run.
#[tokio::test]
async fn dropping_a_running_download_removes_the_cookie() {
    let run = FetchRun::new("bilibili", "BVhang", Some("good-cookie"));
    let stopped = tokio::time::timeout(Duration::from_secs(2), run.run()).await;
    assert!(stopped.is_err(), "the fake yt-dlp should still be running");
    assert!(run.log().contains("--cookies "));
    assert_eq!(names(run.tmp.path()), Vec::<String>::new());
}

/// Job dir with job.json and a finished video-fetch step holding the fixture video and,
/// optionally, its subtitle.
fn fixture_job(with_subtitle: bool) -> tempfile::TempDir {
    let job = tempfile::tempdir().unwrap();
    fs::write(job.path().join("job.json"), job_json("youtube", "local")).unwrap();
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
    assert_eq!(
        names(&framed),
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
    assert_eq!(names(&transcribed), ["manifest.json", "transcript.json"]);
}
