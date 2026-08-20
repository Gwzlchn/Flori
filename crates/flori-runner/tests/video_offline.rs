#[path = "../src/media/video.rs"]
mod video;

use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use flori_core::ArtifactId;
use video::{VideoMediaError, extract_keyframes, probe_video};

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
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-ss' ]; then shift; seek="$1"; fi
  shift
done
case "$seek" in
  1.000) printf 'jpeg-one' ;;
  2.000) printf 'jpeg-two' ;;
  *) exit 8 ;;
esac
"#,
    );
    let mut frames = extract_keyframes(
        &ffmpeg,
        Path::new("video.mp4"),
        3_000,
        2,
        Duration::from_secs(1),
        1_024,
    )
    .await
    .expect("frames");
    assert_eq!(frames.len(), 2);
    assert_eq!(frames[0].logical_name, "frames/0000000001000.jpg");
    assert_eq!(frames[1].logical_name, "frames/0000000002000.jpg");
    assert_eq!(
        frames[0]
            .keyframe(ArtifactId::generate())
            .unwrap()
            .timestamp_ms,
        1_000
    );
    frames[0].logical_name = "frames/0000000000999.jpg".into();
    assert_eq!(
        frames[0].keyframe(ArtifactId::generate()),
        Err(VideoMediaError::InvalidFrameRequest)
    );

    let duplicate = temp.script("ffmpeg-duplicate", "printf 'same-jpeg'\n");
    let frames = extract_keyframes(
        &duplicate,
        Path::new("video.mp4"),
        3_000,
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
    let timeout = temp.script("timeout", "sleep 2\n");
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
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("flori-video-{}-{nonce}", std::process::id()));
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
