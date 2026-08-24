use std::{ffi::OsString, path::Path, time::Duration};

use flori_core::{ArtifactId, VideoKeyframe};

use super::{VideoMediaError, process::run_tool};

const MAX_KEYFRAMES: usize = 12;

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

pub(crate) async fn extract_keyframes(
    ffmpeg: &Path,
    input: &Path,
    duration_ms: u64,
    requested_frames: usize,
    timeout_per_frame: Duration,
    max_frame_bytes: usize,
) -> Result<Vec<FrameOutput>, VideoMediaError> {
    if duration_ms < 2 || requested_frames == 0 || requested_frames > MAX_KEYFRAMES {
        return Err(VideoMediaError::InvalidFrameRequest);
    }
    let count = requested_frames.min(usize::try_from(duration_ms - 1).unwrap_or(usize::MAX));
    let mut frames: Vec<FrameOutput> = Vec::with_capacity(count);
    for index in 1..=count {
        let timestamp_ms = duration_ms.saturating_mul(u64::try_from(index).unwrap_or(u64::MAX))
            / u64::try_from(count + 1).unwrap_or(u64::MAX);
        let seek = format!("{}.{:03}", timestamp_ms / 1_000, timestamp_ms % 1_000);
        let args = [
            OsString::from("-hide_banner"),
            OsString::from("-loglevel"),
            OsString::from("error"),
            OsString::from("-nostdin"),
            OsString::from("-ss"),
            OsString::from(seek),
            OsString::from("-i"),
            input.as_os_str().to_owned(),
            OsString::from("-frames:v"),
            OsString::from("1"),
            OsString::from("-f"),
            OsString::from("image2pipe"),
            OsString::from("-vcodec"),
            OsString::from("mjpeg"),
            OsString::from("pipe:1"),
        ];
        let bytes = run_tool(ffmpeg, &args, timeout_per_frame, max_frame_bytes).await?;
        if bytes.is_empty() {
            return Err(VideoMediaError::ToolFailed);
        }
        if frames.iter().any(|frame| frame.bytes == bytes) {
            continue;
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
    }
    Ok(frames)
}
