#[path = "../src/media/video.rs"]
#[allow(dead_code, unreachable_pub, unused_imports)]
mod video;

use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    time::Duration,
};

use flori_core::{ArtifactId, TranscriptManifest};
use video::{
    VideoMediaError, extract_keyframes, mechanical_note_with_frames, normalize_srt, probe_video,
};

const SUBTITLE: &str = include_str!("../../../tests/fixtures/vnext/local-video.srt");
const TRANSCRIPT: &str = include_str!("../../../tests/fixtures/vnext/expected/transcript.json");
const MECHANICAL_NOTE: &str =
    include_str!("../../../tests/fixtures/vnext/expected/mechanical-note.md");

#[test]
fn normalizes_golden_subtitle_and_keeps_mechanical_note_faithful() {
    let expected: TranscriptManifest = serde_json::from_str(TRANSCRIPT).expect("golden transcript");
    let actual = normalize_srt(
        expected.source_artifact_id,
        "en",
        expected.duration_ms,
        SUBTITLE,
    )
    .expect("normalize golden SRT");
    assert_eq!(actual, expected);

    let note = mechanical_note_with_frames(&actual, &[]).expect("mechanical note");
    assert_eq!(note, MECHANICAL_NOTE);
}

#[test]
fn accepts_trailing_blank_lines_from_platform_subtitles() {
    let source = ArtifactId::generate();
    let subtitle = "1\r\n00:00:00,000 --> 00:00:01,000\r\ncaption\r\n\r\n";
    let transcript = normalize_srt(source, "en", 1_000, subtitle).expect("platform SRT");
    assert_eq!(transcript.cues.len(), 1);
    assert_eq!(transcript.cues[0].text, "caption");
}

#[test]
fn rejects_overlapping_out_of_range_and_malformed_subtitles() {
    let source = ArtifactId::generate();
    let overlap =
        "1\n00:00:00,000 --> 00:00:02,000\none\n\n2\n00:00:01,999 --> 00:00:03,000\ntwo\n";
    assert_eq!(
        normalize_srt(source, "en", 3_000, overlap),
        Err(VideoMediaError::InvalidSubtitle)
    );
    let out_of_range = "1\n00:00:00,000 --> 00:00:03,001\none\n";
    assert_eq!(
        normalize_srt(source, "en", 3_000, out_of_range),
        Err(VideoMediaError::InvalidSubtitle)
    );
    let malformed = "2\n00:00:00,000 --> 00:00:01,000\none\n";
    assert_eq!(
        normalize_srt(source, "en", 3_000, malformed),
        Err(VideoMediaError::InvalidSubtitle)
    );
    let mut invalid = normalize_srt(source, "en", 3_000, SUBTITLE).expect("valid baseline");
    invalid.cues[1].start_ms = 999;
    assert_eq!(
        mechanical_note_with_frames(&invalid, &[]),
        Err(VideoMediaError::InvalidSubtitle)
    );
}

#[tokio::test]
async fn probes_video_streams_and_duration_with_frozen_arguments() {
    let temp = TempDir::new();
    let ffprobe = temp.script(
        "ffprobe",
        r#"case "$*" in
  *"-show_entries format=duration:stream=codec_type,width,height,avg_frame_rate"*) ;;
  *) exit 9 ;;
esac
printf '%s' '{"streams":[{"codec_type":"video","width":320,"height":180,"avg_frame_rate":"10/1"},{"codec_type":"audio"}],"format":{"duration":"3.000000"}}'
"#,
    );
    let probe = probe_video(
        &ffprobe,
        Path::new("video.mp4"),
        Duration::from_secs(1),
        4_096,
    )
    .await
    .expect("probe");
    assert_eq!(probe.duration_ms, 3_000);
    assert_eq!((probe.width, probe.height), (320, 180));
    assert_eq!((probe.frame_rate_num, probe.frame_rate_den), (10, 1));
    assert_eq!((probe.video_streams, probe.audio_streams), (1, 1));
}

#[tokio::test]
async fn extracts_bounded_named_frames_and_deduplicates_inside_executor() {
    let temp = TempDir::new();
    let ffmpeg = temp.script(
        "ffmpeg",
        r#"seek=''
raw='false'
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-ss' ]; then shift; seek="$1"; fi
  if [ "$1" = 'rawvideo' ]; then raw='true'; fi
  shift
done
if [ -z "$seek" ]; then printf '%s\n' '[Parsed_showinfo] pts_time:2.500' >&2; exit 0; fi
if [ "$raw" = 'true' ]; then head -c 1024 /dev/zero; exit 0; fi
case "$seek" in
  0.500) printf 'jpeg-one' ;;
  *) exit 8 ;;
esac
"#,
    );
    let mut frames = extract_keyframes(
        &ffmpeg,
        Path::new("video.mp4"),
        3_000,
        10,
        1,
        2,
        Duration::from_secs(1),
        1_024,
    )
    .await
    .expect("frames");
    assert_eq!(frames.len(), 1);
    assert_eq!(frames[0].logical_name, "frames/0000000000500.jpg");
    assert_eq!(
        frames[0]
            .keyframe(ArtifactId::generate())
            .unwrap()
            .timestamp_ms,
        500
    );
    frames[0].logical_name = "frames/0000000000999.jpg".into();
    assert_eq!(
        frames[0].keyframe(ArtifactId::generate()),
        Err(VideoMediaError::InvalidFrameRequest)
    );

    let duplicate = temp.script(
        "ffmpeg-duplicate",
        r#"seek=''
raw='false'
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-ss' ]; then shift; seek="$1"; fi
  if [ "$1" = 'rawvideo' ]; then raw='true'; fi
  shift
done
if [ -z "$seek" ]; then exit 0; fi
if [ "$raw" = 'true' ]; then head -c 1024 /dev/zero; else printf 'same-jpeg'; fi
"#,
    );
    let frames = extract_keyframes(
        &duplicate,
        Path::new("video.mp4"),
        3_000,
        10,
        1,
        2,
        Duration::from_secs(1),
        1_024,
    )
    .await
    .expect("deduplicated frames");
    assert_eq!(frames.len(), 1);
}

#[tokio::test]
async fn rejects_tool_timeout_nonzero_and_oversize_output() {
    let temp = TempDir::new();
    let timeout = temp.script("timeout", "while :; do :; done\n");
    assert_eq!(
        probe_video(
            &timeout,
            Path::new("video.mp4"),
            Duration::from_millis(20),
            128,
        )
        .await,
        Err(VideoMediaError::ToolTimedOut)
    );

    let nonzero = temp.script("nonzero", "exit 7\n");
    assert_eq!(
        probe_video(
            &nonzero,
            Path::new("video.mp4"),
            Duration::from_secs(1),
            128,
        )
        .await,
        Err(VideoMediaError::ToolFailed)
    );

    let oversize = temp.script("oversize", "head -c 129 /dev/zero\n");
    assert_eq!(
        probe_video(
            &oversize,
            Path::new("video.mp4"),
            Duration::from_secs(1),
            128,
        )
        .await,
        Err(VideoMediaError::OutputTooLarge)
    );
}

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("flori-video-{}", ArtifactId::generate()));
        fs::create_dir(&path).expect("create temp dir");
        Self(path)
    }

    fn script(&self, name: &str, body: &str) -> PathBuf {
        let path = self.0.join(name);
        fs::write(&path, format!("#!/bin/sh\n{body}")).expect("write tool");
        let mut permissions = fs::metadata(&path).expect("metadata").permissions();
        permissions.set_mode(0o700);
        fs::set_permissions(&path, permissions).expect("executable");
        path
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
