#[path = "../src/media/video.rs"]
#[allow(dead_code, unused_imports)]
mod video;

use std::{
    env, fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    process::Command,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use video::{extract_keyframes, probe_video};

const CONTAINER_VIDEO: &str = "/fixtures/local-video.mp4";
const GOLDEN_FRAME: &[u8] = include_bytes!("../../../tests/fixtures/vnext/keyframe-1000ms.jpg");

#[tokio::test]
async fn locked_media_image_processes_real_golden_video() {
    let image = match env::var("FLORI_RUNNER_MEDIA_IMAGE") {
        Ok(image) => image,
        Err(env::VarError::NotPresent) => {
            eprintln!("FLORI_RUNNER_MEDIA_IMAGE is unset; real media-image acceptance skipped");
            return;
        }
        Err(error) => panic!("FLORI_RUNNER_MEDIA_IMAGE is not valid UTF-8: {error}"),
    };
    assert!(
        valid_image_reference(&image),
        "invalid Docker image reference"
    );
    assert_locked_ffmpeg(&image);

    let fixtures = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/vnext")
        .canonicalize()
        .expect("fixture directory");
    let temp = TempDir::new();
    let ffprobe = temp.docker_wrapper("ffprobe", &image, &fixtures);
    let ffmpeg = temp.docker_wrapper("ffmpeg", &image, &fixtures);

    let probe = probe_video(
        &ffprobe,
        Path::new(CONTAINER_VIDEO),
        Duration::from_secs(30),
        64 * 1024,
    )
    .await
    .expect("real ffprobe");
    assert_eq!(probe.duration_ms, 3_000);
    assert_eq!((probe.width, probe.height), (320, 180));
    assert_eq!((probe.frame_rate_num, probe.frame_rate_den), (10, 1));

    let frames = extract_keyframes(
        &ffmpeg,
        Path::new(CONTAINER_VIDEO),
        probe.duration_ms,
        2,
        Duration::from_secs(30),
        1024 * 1024,
    )
    .await
    .expect("real ffmpeg");
    let frame = frames
        .iter()
        .find(|frame| frame.timestamp_ms == 1_000)
        .expect("1000 ms frame");
    assert_eq!(frame.logical_name, "frames/0000000001000.jpg");
    assert_eq!(frame.bytes, GOLDEN_FRAME);
}

fn assert_locked_ffmpeg(image: &str) {
    let output = Command::new("/usr/bin/docker")
        .args([
            "image",
            "inspect",
            "--format",
            "{{index .Config.Labels \"org.flori.runner.tool.ffmpeg\"}}",
            image,
        ])
        .output()
        .expect("inspect media image");
    assert!(output.status.success(), "media image is unavailable");
    assert_eq!(String::from_utf8_lossy(&output.stdout).trim(), "5.1.9");
}

fn valid_image_reference(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 512
        && !value.starts_with('-')
        && value.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-' | b'/' | b':' | b'@')
        })
}

struct TempDir(PathBuf);

impl TempDir {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path =
            env::temp_dir().join(format!("flori-video-image-{}-{nonce}", std::process::id()));
        fs::create_dir(&path).expect("create temp dir");
        Self(path)
    }

    fn docker_wrapper(&self, tool: &str, image: &str, fixtures: &Path) -> PathBuf {
        let path = self.0.join(tool);
        let binding = format!("{}:/fixtures:ro", fixtures.display());
        let script = format!(
            "#!/bin/sh\nexec /usr/bin/docker run --rm --pull=never --network=none -v {} --entrypoint {} {} \"$@\"\n",
            shell_quote(&binding),
            tool,
            shell_quote(image),
        );
        fs::write(&path, script).expect("write Docker wrapper");
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

fn shell_quote(value: &str) -> String {
    format!("'{}'", value.replace('\'', "'\"'\"'"))
}
