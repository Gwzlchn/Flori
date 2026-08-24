#[path = "../src/child_process.rs"]
mod child_process;
#[path = "../src/download/probe.rs"]
mod probe;

use std::{os::unix::fs::PermissionsExt, time::Duration};

use tokio::{fs, sync::watch};

#[tokio::test]
async fn validates_bounded_ffprobe_output() {
    let root = std::env::temp_dir().join(format!("flori-download-probe-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir(&root).expect("root");
    let tool = root.join("ffprobe");
    std::fs::write(
        &tool,
        "#!/bin/sh\nprintf '%s' '{\"streams\":[{\"codec_type\":\"video\"},{\"codec_type\":\"audio\"}],\"format\":{\"duration\":\"3.125\"}}'\n",
    )
    .expect("tool");
    std::fs::set_permissions(&tool, std::fs::Permissions::from_mode(0o755)).expect("mode");
    let input = root.join("video.mp4");
    std::fs::write(&input, b"video").expect("input");
    let (_stop, mut cancel) = watch::channel(false);
    let result = probe::probe(&tool, &input, Duration::from_secs(1), 1024, &mut cancel)
        .await
        .expect("probe");
    assert_eq!(result.duration_ms, 3125);
    assert_eq!(result.video_streams, 1);
    assert_eq!(result.audio_streams, 1);
    fs::remove_dir_all(root).await.expect("cleanup");
}

#[tokio::test]
async fn rejects_invalid_or_oversized_probe_output() {
    let root =
        std::env::temp_dir().join(format!("flori-download-bad-probe-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&root);
    std::fs::create_dir(&root).expect("root");
    let tool = root.join("ffprobe");
    std::fs::write(&tool, "#!/bin/sh\nprintf '%0100d' 0\n").expect("tool");
    std::fs::set_permissions(&tool, std::fs::Permissions::from_mode(0o755)).expect("mode");
    let input = root.join("video.mp4");
    std::fs::write(&input, b"video").expect("input");
    let (_stop, mut cancel) = watch::channel(false);
    assert_eq!(
        probe::probe(&tool, &input, Duration::from_secs(1), 32, &mut cancel)
            .await
            .unwrap_err(),
        flori_core::ErrorCode::ArtifactTooLarge
    );
    fs::remove_dir_all(root).await.expect("cleanup");
}
