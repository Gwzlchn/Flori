use std::{ffi::OsString, path::Path, time::Duration};

use flori_core::ErrorCode;
use serde::Deserialize;
use tokio::sync::watch;

use crate::child_process::{ChildProcessConfig, ChildTermination, run_child_process};

#[derive(Debug)]
pub(super) struct Probe {
    pub(super) duration_ms: u64,
    pub(super) video_streams: usize,
    pub(super) audio_streams: usize,
}

pub(super) async fn probe(
    ffprobe: &Path,
    input: &Path,
    timeout: Duration,
    max_output_bytes: usize,
    cancel: &mut watch::Receiver<bool>,
) -> Result<Probe, ErrorCode> {
    let arguments = [
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "format=duration:stream=codec_type",
    ]
    .into_iter()
    .map(OsString::from)
    .chain([input.as_os_str().to_owned()])
    .collect();
    let output = run_child_process(
        &ChildProcessConfig {
            executable: ffprobe.to_path_buf(),
            arguments,
            working_directory: input.parent().ok_or(ErrorCode::Internal)?.to_path_buf(),
            environment: vec![
                ("LANG".into(), "C.UTF-8".into()),
                ("PATH".into(), "/usr/bin:/bin".into()),
            ],
            stdin: None,
            timeout,
            max_output_bytes,
        },
        cancel,
    )
    .await?;
    drop(output.stderr);
    if output.termination != ChildTermination::Exited || output.exit_code != Some(0) {
        return Err(match output.termination {
            ChildTermination::TimedOut => ErrorCode::AttemptTimeout,
            ChildTermination::Canceled => ErrorCode::TaskCanceled,
            ChildTermination::Exited => ErrorCode::ExecutorFailed,
        });
    }
    let raw: RawProbe =
        serde_json::from_slice(&output.stdout).map_err(|_| ErrorCode::ExecutorFailed)?;
    let seconds = raw
        .format
        .duration
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite() && *value > 0.0)
        .ok_or(ErrorCode::ExecutorFailed)?;
    let duration_ms = (seconds * 1000.0).round();
    if duration_ms > u64::MAX as f64 {
        return Err(ErrorCode::ExecutorFailed);
    }
    Ok(Probe {
        duration_ms: duration_ms as u64,
        video_streams: raw
            .streams
            .iter()
            .filter(|value| value.codec_type == "video")
            .count(),
        audio_streams: raw
            .streams
            .iter()
            .filter(|value| value.codec_type == "audio")
            .count(),
    })
}

#[derive(Deserialize)]
struct RawProbe {
    streams: Vec<RawStream>,
    format: RawFormat,
}

#[derive(Deserialize)]
struct RawStream {
    codec_type: String,
}

#[derive(Deserialize)]
struct RawFormat {
    duration: String,
}
