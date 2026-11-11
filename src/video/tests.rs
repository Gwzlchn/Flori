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
    // whisper.py cuts Chinese after punctuation only where an ideograph follows, so the pieces
    // join back into the same text.
    let mut zh = String::from("仅用19个交易日,");
    push_text(&mut zh, "吃进1,300万股。");
    assert_eq!(zh, "仅用19个交易日,吃进1,300万股。");
    // Korean separates words with spaces.
    let mut korean = String::from("안녕하세요");
    push_text(&mut korean, "여러분");
    assert_eq!(korean, "안녕하세요 여러분");
}

#[test]
fn variant_rank_orders_tracks_of_one_language() {
    let mut langs = ["zh-TW", "zh-Hant", "zh", "zh-Hans", "zh-CN", "en-US", "en"];
    langs.sort_by_key(|lang| (variant_rank(lang), *lang));
    assert_eq!(
        langs,
        ["en", "zh", "zh-CN", "zh-Hans", "en-US", "zh-Hant", "zh-TW"]
    );
}

/// Writes one-cue `subtitle.<lang>.srt` files (an empty text makes an unusable track) and runs
/// keep_subtitles for `zh` and `en` without AI tracks; returns what it reported and the files
/// left behind.
fn keep(original: Option<&str>, tracks: &[(&str, &str)]) -> (Kept, Vec<String>) {
    let (kept, left) = keep_with(original, &[], tracks);
    assert!(kept.ai.is_empty());
    (kept, left)
}

/// [`keep`] with Bilibili AI tracks in `ai_langs` allowed. Kept files show as `<file>=<cue>`.
fn keep_with(
    original: Option<&str>,
    ai_langs: &[&str],
    tracks: &[(&str, &str)],
) -> (Kept, Vec<String>) {
    let dir = tempfile::tempdir().unwrap();
    for (lang, text) in tracks {
        let srt = format!("1\n00:00:00,000 --> 00:00:01,000\n{text}\n");
        fs::write(dir.path().join(format!("subtitle.{lang}.srt")), srt).unwrap();
    }
    fs::write(dir.path().join("video.mp4"), "x").unwrap();
    // Left by a subtitle run that failed before converting.
    fs::write(dir.path().join("video.en.vtt"), "partial").unwrap();
    let langs = match original {
        Some(o) if o != "zh" && o != "en" => vec![o, "zh"],
        _ => vec!["zh", "en"],
    };
    let kept = keep_subtitles(dir.path(), original, &langs, ai_langs).unwrap();
    let mut left = names(dir.path());
    for name in left.iter_mut().filter(|n| n.ends_with(".srt")) {
        let text = fs::read_to_string(dir.path().join(&*name)).unwrap();
        *name = format!("{name}={}", text.lines().nth(2).unwrap());
    }
    (kept, left)
}

/// What keep_subtitles reports.
fn kept(language: Option<&str>, chinese: bool, manual: &[&str], ai: &[&str]) -> Kept {
    let strings = |langs: &[&str]| langs.iter().map(|l| l.to_string()).collect();
    Kept {
        language: language.map(Into::into),
        chinese,
        manual: strings(manual),
        ai: strings(ai),
    }
}

fn files(names: &[&str]) -> Vec<String> {
    names.iter().map(|n| n.to_string()).collect()
}

#[test]
fn subtitles_follow_the_original_language() {
    let both = [
        ("zh-Hans", "你好"),
        ("zh-Hant", "哈囉"),
        ("en", "hello"),
        ("de", "hallo"),
    ];
    // An English original: Chinese is the translation, not the transcript.
    assert_eq!(
        keep(Some("en"), &both),
        (
            kept(Some("en"), true, &[], &[]),
            files(&["subtitle.srt=hello", "subtitle_zh.srt=你好", "video.mp4"])
        )
    );
    // A Chinese original takes the Chinese track and has no translation.
    let zh_only = (
        kept(Some("zh"), false, &[], &[]),
        files(&["subtitle.srt=你好", "video.mp4"]),
    );
    assert_eq!(keep(Some("zh"), &both), zh_only);
    // A Chinese original without a Chinese track falls back to English.
    assert_eq!(
        keep(Some("zh"), &[("en", "hello")]),
        (
            kept(Some("en"), false, &[], &[]),
            files(&["subtitle.srt=hello", "video.mp4"])
        )
    );
    // Only a Chinese track of an English video: whisper transcribes, the track translates.
    let translation_only = (
        kept(None, true, &[], &[]),
        files(&["subtitle_zh.srt=你好", "video.mp4"]),
    );
    assert_eq!(keep(Some("en"), &[("zh-Hans", "你好")]), translation_only);
    // A German original without a German track: English is not a fallback, whisper transcribes.
    let de = [("en", "hello"), ("zh-Hans", "你好")];
    assert_eq!(keep(Some("de"), &de), translation_only);
    // A German original takes its own track.
    assert_eq!(
        keep(Some("de"), &both),
        (
            kept(Some("de"), true, &[], &[]),
            files(&["subtitle.srt=hallo", "subtitle_zh.srt=你好", "video.mp4"])
        )
    );
    // An unusable track never wins, neither as the transcript nor as the translation.
    let tracks = [("en", "hello"), ("zh-Hans", ""), ("zh-Hant", "哈囉")];
    assert_eq!(
        keep(Some("en"), &tracks),
        (
            kept(Some("en"), true, &[], &[]),
            files(&["subtitle.srt=hello", "subtitle_zh.srt=哈囉", "video.mp4"])
        ),
        "an empty simplified track gives way to traditional"
    );
    assert_eq!(
        keep(None, &[]),
        (kept(None, false, &[], &[]), files(&["video.mp4"]))
    );
}

#[test]
fn an_unknown_original_keeps_one_candidate_per_language_for_the_audio_to_pick() {
    // TED-Ed (job 12): yt-dlp reported no language, the tracks are English and a community
    // translation into traditional Chinese. Neither becomes subtitle.srt here.
    assert_eq!(
        keep(None, &[("en", "hello"), ("zh-TW", "哈囉")]),
        (
            kept(None, false, &["zh", "en"], &[]),
            files(&[
                "manual_subtitle_en.srt=hello",
                "manual_subtitle_zh.srt=哈囉",
                "video.mp4"
            ])
        )
    );
    // A Chinese video with only a Chinese track; simplified wins over traditional, and a
    // language that was not asked for goes.
    let tracks = [("zh-Hant", "哈囉"), ("zh-Hans", "你好"), ("de", "hallo")];
    assert_eq!(
        keep(None, &tracks),
        (
            kept(None, false, &["zh"], &[]),
            files(&["manual_subtitle_zh.srt=你好", "video.mp4"])
        )
    );
    // An unusable track is no candidate.
    assert_eq!(
        keep(None, &[("zh-Hans", ""), ("en", "hello")]),
        (
            kept(None, false, &["en"], &[]),
            files(&["manual_subtitle_en.srt=hello", "video.mp4"])
        )
    );
}

#[test]
fn ai_tracks_wait_for_video_transcribe_only_without_a_manual_transcript() {
    let ai = [
        ("ai-zh", "一度浮盈3个亿"),
        ("ai-en", "once up 300 million"),
        ("ai-ja", "一時は3億の含み益"),
    ];
    // Bilibili without a manual track: the AI tracks of the requested languages stay as
    // candidates; ai-ja was not requested.
    assert_eq!(
        keep_with(None, &["zh", "en"], &ai),
        (
            kept(None, false, &[], &["en", "zh"]),
            files(&[
                "ai_subtitle_en.srt=once up 300 million",
                "ai_subtitle_zh.srt=一度浮盈3个亿",
                "video.mp4",
            ])
        )
    );
    // With the original unknown, manual tracks are candidates too and the AI tracks stay: the
    // audio may be in a language only an AI track has.
    let mut with_manual = ai.to_vec();
    with_manual.push(("en", "hello"));
    assert_eq!(
        keep_with(None, &["zh", "en"], &with_manual),
        (
            kept(None, false, &["en"], &["en", "zh"]),
            files(&[
                "ai_subtitle_en.srt=once up 300 million",
                "ai_subtitle_zh.srt=一度浮盈3个亿",
                "manual_subtitle_en.srt=hello",
                "video.mp4",
            ])
        )
    );
    // A manual transcript wins; an AI Chinese track is never the translation.
    let tracks = [("en", "hello"), ("ai-zh", "你好")];
    assert_eq!(
        keep_with(Some("en"), &["en", "zh"], &tracks),
        (
            kept(Some("en"), false, &[], &[]),
            files(&["subtitle.srt=hello", "video.mp4"])
        )
    );
    // Without AI languages, as on YouTube, ai- tracks are never kept; nor is an empty one.
    let none = (kept(None, false, &[], &[]), files(&["video.mp4"]));
    assert_eq!(keep_with(None, &[], &ai), none);
    assert_eq!(keep_with(None, &["zh"], &[("ai-zh", "")]), none);
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
fn each_section_keeps_at_most_two_frames_spread_over_it() {
    assert_eq!(evenly_bounded(&[1, 2, 3, 4, 5], 3), [1, 3, 5]);
    assert_eq!(evenly_bounded(&[1, 2], 3), [1, 2]);
    let candidates = vec![
        (0, slide(0)),         // kept: first of its section
        (10_000, slide(1)),    // thinned out: middle of three
        (20_000, slide(2)),    // kept: last of its section
        (40_000, slide(1)),    // kept: the frame it repeats was not kept
        (3_600_000, slide(3)), // an hour in, still kept: no cap on the whole video
    ];
    assert_eq!(pick_keyframes(&candidates), [0, 20_000, 40_000, 3_600_000]);
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
        assert_eq!(meta["width"], MetaValue::Int(1280));
        assert_eq!(meta["height"], MetaValue::Int(720));
        assert_eq!(meta["fps"], MetaValue::Float(29.97));
        assert_eq!(meta["vcodec"], MetaValue::Text("avc1.64001F".into()));
        assert_eq!(meta["acodec"], MetaValue::Text("mp4a.40.2".into()));
        let tags = MetaValue::List(vec!["talk".into(), "ted ed".into()]);
        assert_eq!(meta["tags"], tags);
        // The fake's video.mp4 is the 10 bytes "fake video".
        assert_eq!(meta["filesize_bytes"], MetaValue::Int(10));
    }
}

#[test]
fn media_values_are_checked_one_by_one() {
    let meta = |value: serde_json::Value| {
        let media: FetchedMedia = serde_json::from_value(value).unwrap();
        media_meta(&media)
            .into_iter()
            .collect::<std::collections::BTreeMap<_, _>>()
    };
    let odd = meta(serde_json::json!({
        "width": 0, "height": 1e9, "fps": -1, "vcodec": "none", "acodec": "<script>",
        "tags": "talk",
    }));
    assert!(odd.is_empty(), "{odd:?}");
    let odd = meta(serde_json::json!({
        "width": "1280", "height": 70_000, "fps": 5000, "vcodec": "x".repeat(65),
        "acodec": ["mp4a"], "tags": [1, null, "a\u{7}b", "x".repeat(101)],
    }));
    assert!(odd.is_empty(), "{odd:?}");
    assert!(meta(serde_json::json!({})).is_empty());
    // Tags stop before their text passes 500 characters; duplicates do not count.
    let tags: Vec<String> = (0..10).map(|i| format!("{i}{}", "x".repeat(99))).collect();
    let mut given = vec![tags[0].clone()];
    given.extend(tags.iter().cloned());
    let kept = meta(serde_json::json!({"tags": given, "width": 3840, "height": 2160, "fps": 60}));
    assert_eq!(kept["tags"], MetaValue::List(tags[..5].to_vec()));
    assert_eq!(kept["width"], MetaValue::Int(3840));
    assert_eq!(kept["fps"], MetaValue::Float(60.0));
}

#[tokio::test]
async fn subtitle_is_kept_and_both_runs_skip_live_streams() {
    let run = FetchRun::new("youtube", "video-lang-en-subs-ok", None);
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
            line.ends_with("-- https://www.youtube.com/watch?v=video-lang-en-subs-ok"),
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
        /// `(lang, cue)` of each manual_subtitle_<lang>.srt, then the ai_subtitle_<lang> kept.
        candidates: (
            &'static [(&'static str, &'static str)],
            &'static [&'static str],
        ),
        sub_langs: &'static str,
    }
    let none: (&[(&str, &str)], &[&str]) = (&[], &[]);
    let cases = [
        Case {
            platform: "youtube",
            id: "video-lang-en-subs-both",
            subtitle: Some(("hello", "en")),
            chinese: Some("你好"),
            candidates: none,
            sub_langs: "zh.*,en.*",
        },
        // Unknown original: any track may be a translation, so the audio decides later.
        Case {
            platform: "youtube",
            id: "video-subs-both",
            subtitle: None,
            chinese: None,
            candidates: (&[("en", "hello"), ("zh", "你好")], &[]),
            sub_langs: "zh.*,en.*",
        },
        // No English track: whisper transcribes, the Chinese track is still the translation.
        Case {
            platform: "youtube",
            id: "video-lang-en-subs-zh",
            subtitle: None,
            chinese: Some("你好"),
            candidates: none,
            sub_langs: "zh.*,en.*",
        },
        Case {
            platform: "youtube",
            id: "video-lang-fr-subs-fr",
            subtitle: Some(("bonjour", "fr")),
            chinese: Some("你好"),
            candidates: none,
            sub_langs: "fr.*,zh.*",
        },
        // A French original with only English and Chinese tracks: English is never the
        // transcript, so it is not even requested.
        Case {
            platform: "youtube",
            id: "video-lang-fr-subs-both",
            subtitle: None,
            chinese: Some("你好"),
            candidates: none,
            sub_langs: "fr.*,zh.*",
        },
        // yt-dlp's Bilibili extractor sets no language, so every Bilibili track is a candidate,
        // the AI ones of the requested languages too.
        Case {
            platform: "bilibili",
            id: "BV-subs-both",
            subtitle: None,
            chinese: None,
            candidates: (&[("en", "hello"), ("zh", "你好")], &[]),
            sub_langs: "zh.*,en.*,ai-zh,ai-en",
        },
        Case {
            platform: "bilibili",
            id: "BV-subs-ai-manual",
            subtitle: None,
            chinese: None,
            candidates: (&[("zh", "你好")], &["en", "zh"]),
            sub_langs: "zh.*,en.*,ai-zh,ai-en",
        },
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
        let (manual, ai) = case.candidates;
        for (lang, text) in manual {
            let name = format!("{MANUAL_SUBTITLE}{lang}");
            assert_eq!(file(&name), Some(format!("{name}.srt").as_str()), "{id}");
            assert_eq!(cue(&format!("{name}.srt")).as_deref(), Some(*text), "{id}");
        }
        let prefixed = |prefix: &str| -> Vec<String> {
            let names = manifest.files.keys().filter_map(|k| k.strip_prefix(prefix));
            names.map(str::to_owned).collect()
        };
        let manual: Vec<String> = manual.iter().map(|(l, _)| l.to_string()).collect();
        assert_eq!(prefixed(MANUAL_SUBTITLE), manual, "{id}");
        assert_eq!(prefixed(AI_SUBTITLE), ai.to_vec(), "{id}");
        let subtitle_run = run.log().lines().nth(1).unwrap().to_owned();
        let sub_langs = format!("--sub-langs {} ", case.sub_langs);
        assert!(subtitle_run.contains(&sub_langs), "{id}: {subtitle_run}");
    }
}

#[tokio::test]
async fn bilibili_ai_tracks_are_fetched_for_video_transcribe_to_check() {
    let run = FetchRun::new("bilibili", "BV-subs-ai", Some("good-cookie"));
    run.run().await.unwrap();
    assert_eq!(
        names(&run.out()),
        [
            "ai_subtitle_en.srt",
            "ai_subtitle_zh.srt",
            "manifest.json",
            "video.mp4"
        ]
    );
    let manifest = run.manifest();
    assert_eq!(manifest.files["ai_subtitle_zh"], "ai_subtitle_zh.srt");
    assert_eq!(manifest.files["ai_subtitle_en"], "ai_subtitle_en.srt");
    assert_eq!(manifest.files.len(), 3);
    assert!(!manifest.meta.contains_key("subtitle_language"));
    assert_eq!(
        fs::read_to_string(run.out().join("ai_subtitle_zh.srt")).unwrap(),
        fs::read_to_string(fixture("bili-ai-zh.srt")).unwrap()
    );
    let subtitle_run = run.log().lines().nth(1).unwrap().to_owned();
    assert!(
        subtitle_run.contains(" --sub-langs zh.*,en.*,ai-zh,ai-en "),
        "{subtitle_run}"
    );
    assert!(subtitle_run.contains(" --cookies "), "{subtitle_run}");

    // YouTube asks for no ai- track and keeps none.
    let run = FetchRun::new("youtube", "video-subs-ai", None);
    run.run().await.unwrap();
    assert_eq!(names(&run.out()), ["manifest.json", "video.mp4"]);
    assert!(run.log().contains(" --sub-langs zh.*,en.* "));
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

/// A finished video-fetch step whose video.mp4 holds the spoken language for the fake ffmpeg
/// and whisper.py, plus `(logical name, file, content)` files and text meta.
fn fetched_job(
    spoken: &str,
    files: &[(&str, &str, &str)],
    meta: &[(&str, &str)],
) -> tempfile::TempDir {
    let job = tempfile::tempdir().unwrap();
    fs::write(job.path().join("job.json"), job_json("bilibili", "BVlocal")).unwrap();
    let fetch = job.path().join("steps/01-video-fetch");
    fs::create_dir_all(&fetch).unwrap();
    fs::write(fetch.join(VIDEO_FILE), spoken).unwrap();
    let mut manifest = Manifest::default();
    manifest.files.insert("video".into(), VIDEO_FILE.into());
    for (name, file, text) in files {
        fs::write(fetch.join(file), text).unwrap();
        manifest.files.insert(name.to_string(), file.to_string());
    }
    for (key, value) in meta {
        manifest
            .meta
            .insert(key.to_string(), MetaValue::Text(value.to_string()));
    }
    write_manifest(&fetch, &manifest).unwrap();
    job
}

/// video-transcribe against tests/fixtures/video/fake/{ffmpeg,whisper.py}. Returns the
/// manifest, the transcript and the whisper.py modes that ran, one per line.
async fn transcribe_fake(job: &Path) -> (Manifest, Transcript, String) {
    let env = TranscribeEnv {
        ffmpeg: fixture("fake/ffmpeg").into(),
        python: "python3".into(),
        steps_dir: Some(fixture("fake").to_string_lossy().into_owned()),
    };
    let out = job.join("steps/02-video-transcribe.tmp");
    fs::create_dir(&out).unwrap();
    if let Err(failure) = transcribe_with(job, &out, &env).await {
        panic!("{:?} {}", failure.exit, failure.message);
    }
    // No scratch audio or whisper output is left behind.
    let left = names(&out);
    let outputs = ["manifest.json", "subtitle_zh.srt", "transcript.json"];
    assert!(
        left.iter().all(|n| outputs.contains(&n.as_str())),
        "{left:?}"
    );
    let log = fs::read_to_string(job.join("steps/fake-whisper.log")).unwrap_or_default();
    let transcript = read_json(&out.join(TRANSCRIPT_JSON)).unwrap();
    (pipeline::read_manifest(&out).unwrap(), transcript, log)
}

fn text(value: &str) -> MetaValue {
    MetaValue::Text(value.into())
}

#[tokio::test]
async fn an_ai_subtitle_is_the_transcript_only_in_the_spoken_language() {
    let ai_zh = fs::read_to_string(fixture("bili-ai-zh.srt")).unwrap();
    let ai_en = "1\n00:00:00,280 --> 00:00:02,600\nDuring the 2017 crash recovery\n\n";
    let tracks = [
        ("ai_subtitle_zh", "ai_subtitle_zh.srt", ai_zh.as_str()),
        ("ai_subtitle_en", "ai_subtitle_en.srt", ai_en),
    ];

    // Mandarin audio: ai-zh is the transcript as it is; whisper only detects the language.
    let job = fetched_job("zh", &tracks, &[]);
    let (manifest, transcript, log) = transcribe_fake(job.path()).await;
    assert_eq!(log, "detect\n");
    assert_eq!(transcript.language.as_deref(), Some("zh"));
    assert_eq!(transcript.segments, parse_srt(&ai_zh));
    assert_eq!(
        transcript.segments[5],
        segment(11_520, 13_160, "一度浮盈3个亿")
    );
    assert_eq!(manifest.meta["origin"], text("subtitle"));
    assert_eq!(manifest.meta["subtitle_kind"], text("ai"));
    assert_eq!(manifest.meta["language"], text("zh"));
    assert_eq!(
        manifest.meta["language_probability"],
        MetaValue::Float(0.75)
    );

    // English audio takes the English track.
    let job = fetched_job("en", &tracks, &[]);
    let (manifest, transcript, log) = transcribe_fake(job.path()).await;
    assert_eq!(log, "detect\n");
    assert_eq!(
        transcript.segments,
        [segment(280, 2600, "During the 2017 crash recovery")]
    );
    assert_eq!(manifest.meta["subtitle_kind"], text("ai"));
    assert_eq!(manifest.meta["language"], text("en"));

    // Japanese audio: both tracks are machine translations, so whisper transcribes.
    let job = fetched_job("ja", &tracks, &[]);
    let (manifest, transcript, log) = transcribe_fake(job.path()).await;
    assert_eq!(log, "detect\ntranscribe\n");
    assert_eq!(transcript.segments, [segment(0, 1000, "whisper ja")]);
    assert_eq!(manifest.meta["origin"], text("whisper"));
    assert!(!manifest.meta.contains_key("subtitle_kind"));
    assert_eq!(manifest.meta["language"], text("ja"));
}

#[tokio::test]
async fn the_audio_picks_the_manual_track_when_yt_dlp_did_not_know_the_language() {
    let en = "1\n00:00:06,586 --> 00:00:09,630\nIn 2016, a group of researchers\n";
    let tw = "1\n00:00:06,586 --> 00:00:09,630\n2016 年，一群研究人員——\n";
    let manual = [
        ("manual_subtitle_en", "manual_subtitle_en.srt", en),
        ("manual_subtitle_zh", "manual_subtitle_zh.srt", tw),
    ];
    let translation = |job: &Path| {
        fs::read_to_string(job.join("steps/02-video-transcribe.tmp/subtitle_zh.srt")).ok()
    };

    // TED-Ed (job 12): English audio. The English track is the transcript and the Chinese
    // community translation the human translation, not the other way round.
    let job = fetched_job("en", &manual, &[]);
    let (manifest, transcript, log) = transcribe_fake(job.path()).await;
    assert_eq!(log, "detect\n");
    assert_eq!(transcript.language.as_deref(), Some("en"));
    let researchers = segment(6586, 9630, "In 2016, a group of researchers");
    assert_eq!(transcript.segments, [researchers]);
    assert_eq!(manifest.meta["origin"], text("subtitle"));
    assert_eq!(manifest.meta["subtitle_kind"], text("manual"));
    assert_eq!(manifest.meta["language"], text("en"));
    assert_eq!(manifest.files["subtitle_zh"], "subtitle_zh.srt");
    assert_eq!(translation(job.path()).as_deref(), Some(tw));

    // A Chinese video with only a Chinese track: it is the transcript and nothing translates it.
    let job = fetched_job("zh", &manual[1..], &[]);
    let (manifest, transcript, log) = transcribe_fake(job.path()).await;
    assert_eq!(log, "detect\n");
    assert_eq!(
        transcript.segments,
        [segment(6586, 9630, "2016 年，一群研究人員——")]
    );
    assert_eq!(manifest.meta["subtitle_kind"], text("manual"));
    assert_eq!(manifest.meta["language"], text("zh"));
    assert!(!manifest.files.contains_key("subtitle_zh"));
    assert_eq!(translation(job.path()), None);

    // A manual track in the spoken language comes before the AI one.
    let mut both = manual.to_vec();
    both.push((
        "ai_subtitle_zh",
        "ai_subtitle_zh.srt",
        "1\n00:00:00,000 --> 00:00:01,000\nAI\n",
    ));
    let job = fetched_job("zh", &both, &[]);
    let (manifest, transcript, _) = transcribe_fake(job.path()).await;
    assert_eq!(manifest.meta["subtitle_kind"], text("manual"));
    assert_eq!(transcript.segments[0].text, "2016 年，一群研究人員——");

    // Japanese audio: neither track is the original, whisper transcribes, and the Chinese one
    // still translates it.
    let job = fetched_job("ja", &manual, &[]);
    let (manifest, transcript, log) = transcribe_fake(job.path()).await;
    assert_eq!(log, "detect\ntranscribe\n");
    assert_eq!(transcript.segments, [segment(0, 1000, "whisper ja")]);
    assert_eq!(manifest.meta["origin"], text("whisper"));
    assert_eq!(translation(job.path()).as_deref(), Some(tw));
}

#[tokio::test]
async fn a_manual_subtitle_needs_no_audio_and_plain_whisper_no_detection() {
    let srt = fs::read_to_string(fixture("local-video.srt")).unwrap();
    let files = [("subtitle", "subtitle.srt", srt.as_str())];
    let job = fetched_job("zh", &files, &[("subtitle_language", "en")]);
    let (manifest, transcript, log) = transcribe_fake(job.path()).await;
    assert_eq!(log, "");
    assert_eq!(transcript.segments.len(), 3);
    assert_eq!(manifest.meta["origin"], text("subtitle"));
    assert_eq!(manifest.meta["subtitle_kind"], text("manual"));
    assert_eq!(manifest.meta["language"], text("en"));
    assert_eq!(manifest.meta["language_probability"], MetaValue::Float(1.0));

    let job = fetched_job("zh", &[], &[]);
    let (manifest, transcript, log) = transcribe_fake(job.path()).await;
    assert_eq!(log, "transcribe\n");
    assert_eq!(transcript.segments, [segment(0, 1000, "whisper zh")]);
    assert_eq!(manifest.meta["origin"], text("whisper"));
    assert!(!manifest.meta.contains_key("subtitle_kind"));
}
