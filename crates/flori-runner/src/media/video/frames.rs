use std::{ffi::OsString, path::Path, time::Duration};

use flori_core::{ArtifactId, VideoKeyframe};

use super::{
    VideoMediaError,
    process::run_tool,
    scene::{FORCED_INTERVAL_MS, LONG_SCENE_MS, Scene, detect_scenes},
    similarity::{perceptual_hash, ssim},
};

const MAX_KEYFRAMES: usize = 12;
const DYNAMIC_SSIM: f64 = 0.85;
const DUPLICATE_SSIM: f64 = 0.92;
const PHASH_DUPLICATE: u32 = 6;
const PHASH_DIFFERENT: u32 = 10;

#[derive(Debug, Eq, PartialEq)]
pub(crate) struct FrameOutput {
    pub logical_name: String,
    pub timestamp_ms: u64,
    pub bytes: Vec<u8>,
}

impl FrameOutput {
    pub(crate) fn keyframe(
        &self,
        artifact_id: ArtifactId,
    ) -> Result<VideoKeyframe, VideoMediaError> {
        let keyframe = VideoKeyframe::from_artifact_name(artifact_id, &self.logical_name)
            .map_err(|_| VideoMediaError::InvalidFrameRequest)?;
        if keyframe.timestamp_ms != self.timestamp_ms {
            return Err(VideoMediaError::InvalidFrameRequest);
        }
        Ok(keyframe)
    }
}

#[allow(clippy::too_many_arguments)]
pub(crate) async fn extract_keyframes(
    ffmpeg: &Path,
    input: &Path,
    duration_ms: u64,
    frame_rate_num: u32,
    frame_rate_den: u32,
    requested_frames: usize,
    timeout: Duration,
    max_output_bytes: usize,
) -> Result<Vec<FrameOutput>, VideoMediaError> {
    if duration_ms < 2
        || frame_rate_num == 0
        || frame_rate_den == 0
        || requested_frames == 0
        || requested_frames > MAX_KEYFRAMES
    {
        return Err(VideoMediaError::InvalidFrameRequest);
    }
    let scenes = detect_scenes(ffmpeg, input, duration_ms, timeout, max_output_bytes).await?;
    let mut timestamps = Vec::new();
    for scene in scenes {
        let selected = representative(
            ffmpeg,
            input,
            scene,
            frame_rate_num,
            frame_rate_den,
            timeout,
            max_output_bytes,
        )
        .await?;
        timestamps.extend(scene_timestamps(scene, selected));
    }
    timestamps.sort_unstable();
    timestamps.dedup();
    let timestamps = evenly_bounded(&timestamps, requested_frames);
    let mut frames = Vec::with_capacity(timestamps.len());
    let mut fingerprints = Vec::with_capacity(timestamps.len());
    for timestamp_ms in timestamps {
        let pixels = grayscale(ffmpeg, input, timestamp_ms, timeout, max_output_bytes).await?;
        let hash = perceptual_hash(&pixels).ok_or(VideoMediaError::ToolFailed)?;
        if fingerprints
            .iter()
            .any(|(seen_hash, seen_pixels): &(u64, Vec<u8>)| {
                let distance = (hash ^ seen_hash).count_ones();
                distance <= PHASH_DUPLICATE
                    || (distance <= PHASH_DIFFERENT
                        && ssim(&pixels, seen_pixels).is_some_and(|value| value >= DUPLICATE_SSIM))
            })
        {
            continue;
        }
        let bytes = jpeg(ffmpeg, input, timestamp_ms, timeout, max_output_bytes).await?;
        if bytes.is_empty() {
            return Err(VideoMediaError::ToolFailed);
        }
        let logical_name = format!("frames/{timestamp_ms:013}.jpg");
        let frame = FrameOutput {
            logical_name,
            timestamp_ms,
            bytes,
        };
        if frame.keyframe(ArtifactId::generate())?.timestamp_ms >= duration_ms {
            return Err(VideoMediaError::InvalidFrameRequest);
        }
        frames.push(frame);
        fingerprints.push((hash, pixels));
    }
    Ok(frames)
}

#[allow(clippy::too_many_arguments)]
async fn representative(
    ffmpeg: &Path,
    input: &Path,
    scene: Scene,
    frame_rate_num: u32,
    frame_rate_den: u32,
    timeout: Duration,
    max_output_bytes: usize,
) -> Result<u64, VideoMediaError> {
    let span = scene.end_ms.saturating_sub(scene.start_ms);
    let head = scene.start_ms.min(scene.end_ms.saturating_sub(1));
    let ratio = scene.start_ms.saturating_add(span.saturating_mul(7) / 10);
    let head_pixels = grayscale(ffmpeg, input, head, timeout, max_output_bytes).await?;
    let ratio_pixels = grayscale(ffmpeg, input, ratio, timeout, max_output_bytes).await?;
    if ssim(&head_pixels, &ratio_pixels).is_some_and(|value| value < DYNAMIC_SSIM) {
        return Ok(ratio.min(scene.end_ms.saturating_sub(1)));
    }
    let five_frames_ms = 5_u64
        .saturating_mul(1_000)
        .saturating_mul(u64::from(frame_rate_den))
        .div_ceil(u64::from(frame_rate_num));
    Ok(scene
        .start_ms
        .saturating_add(five_frames_ms)
        .min(scene.end_ms.saturating_sub(1)))
}

fn evenly_bounded(timestamps: &[u64], limit: usize) -> Vec<u64> {
    if timestamps.len() <= limit {
        return timestamps.to_vec();
    }
    if limit == 1 {
        return vec![timestamps[timestamps.len() / 2]];
    }
    (0..limit)
        .map(|index| timestamps[index * (timestamps.len() - 1) / (limit - 1)])
        .collect()
}

fn scene_timestamps(scene: Scene, representative: u64) -> Vec<u64> {
    let mut timestamps = vec![representative];
    if scene.end_ms.saturating_sub(scene.start_ms) > LONG_SCENE_MS {
        let mut timestamp = scene.start_ms.saturating_add(FORCED_INTERVAL_MS);
        while timestamp < scene.end_ms.saturating_sub(5_000) {
            timestamps.push(timestamp);
            timestamp = timestamp.saturating_add(FORCED_INTERVAL_MS);
        }
    }
    timestamps
}

async fn grayscale(
    ffmpeg: &Path,
    input: &Path,
    timestamp_ms: u64,
    timeout: Duration,
    max_output_bytes: usize,
) -> Result<Vec<u8>, VideoMediaError> {
    let arguments = frame_arguments(
        input,
        timestamp_ms,
        "rawvideo",
        Some("scale=32:32,format=gray"),
    );
    let pixels = run_tool(ffmpeg, &arguments, timeout, max_output_bytes).await?;
    if pixels.len() != 32 * 32 {
        return Err(VideoMediaError::ToolFailed);
    }
    Ok(pixels)
}

async fn jpeg(
    ffmpeg: &Path,
    input: &Path,
    timestamp_ms: u64,
    timeout: Duration,
    max_output_bytes: usize,
) -> Result<Vec<u8>, VideoMediaError> {
    run_tool(
        ffmpeg,
        &frame_arguments(input, timestamp_ms, "mjpeg", None),
        timeout,
        max_output_bytes,
    )
    .await
}

fn frame_arguments(
    input: &Path,
    timestamp_ms: u64,
    codec: &str,
    filter: Option<&str>,
) -> Vec<OsString> {
    let mut arguments = vec![
        OsString::from("-hide_banner"),
        OsString::from("-loglevel"),
        OsString::from("error"),
        OsString::from("-nostdin"),
        OsString::from("-ss"),
        OsString::from(format!(
            "{}.{:03}",
            timestamp_ms / 1_000,
            timestamp_ms % 1_000
        )),
        OsString::from("-i"),
        input.as_os_str().to_owned(),
        OsString::from("-frames:v"),
        OsString::from("1"),
    ];
    if let Some(filter) = filter {
        arguments.extend([OsString::from("-vf"), OsString::from(filter)]);
    }
    arguments.extend([
        OsString::from("-f"),
        OsString::from(if codec == "rawvideo" {
            "rawvideo"
        } else {
            "image2pipe"
        }),
        OsString::from("-vcodec"),
        OsString::from(codec),
        OsString::from("pipe:1"),
    ]);
    arguments
}

#[cfg(test)]
mod tests {
    use super::{Scene, evenly_bounded, scene_timestamps};

    #[test]
    fn candidate_limit_preserves_timeline_edges() {
        assert_eq!(evenly_bounded(&[1, 2, 3, 4, 5], 3), vec![1, 3, 5]);
        assert_eq!(evenly_bounded(&[1, 2, 3], 1), vec![2]);
    }

    #[test]
    fn long_scene_adds_fixed_interval_safety_frames() {
        assert_eq!(
            scene_timestamps(
                Scene {
                    start_ms: 10_000,
                    end_ms: 80_001,
                },
                10_500,
            ),
            vec![10_500, 25_000, 40_000, 55_000, 70_000]
        );
    }
}
