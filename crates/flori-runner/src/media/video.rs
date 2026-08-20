use std::{
    ffi::OsString,
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    },
    time::Duration,
};

use flori_core::{ArtifactId, VideoKeyframe};
use serde::Deserialize;
use tokio::{
    io::{AsyncRead, AsyncReadExt},
    process::Command,
    sync::watch,
};

#[path = "video/subtitle.rs"]
mod subtitle;

pub(crate) use subtitle::{mechanical_note, normalize_srt};

const MAX_KEYFRAMES: usize = 12;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct VideoProbe {
    pub duration_ms: u64,
    pub width: u32,
    pub height: u32,
    pub frame_rate_num: u32,
    pub frame_rate_den: u32,
    pub video_streams: u16,
    pub audio_streams: u16,
}

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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum VideoMediaError {
    InvalidProbe,
    InvalidSubtitle,
    InvalidFrameRequest,
    ToolFailed,
    ToolTimedOut,
    OutputTooLarge,
}

pub(crate) async fn probe_video(
    ffprobe: &Path,
    input: &Path,
    timeout: Duration,
    max_output_bytes: usize,
) -> Result<VideoProbe, VideoMediaError> {
    let args = [
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "format=duration:stream=codec_type,width,height,avg_frame_rate",
    ]
    .into_iter()
    .map(OsString::from)
    .chain([input.as_os_str().to_owned()])
    .collect::<Vec<_>>();
    let output = run_tool(ffprobe, &args, timeout, max_output_bytes).await?;
    let raw: RawProbe =
        serde_json::from_slice(&output).map_err(|_| VideoMediaError::InvalidProbe)?;
    let duration_ms = parse_duration_ms(&raw.format.duration)?;
    let mut video_streams = 0_u16;
    let mut audio_streams = 0_u16;
    let mut primary = None;
    for stream in raw.streams {
        match stream.codec_type.as_str() {
            "video" => {
                video_streams = video_streams
                    .checked_add(1)
                    .ok_or(VideoMediaError::InvalidProbe)?;
                primary.get_or_insert(stream);
            }
            "audio" => {
                audio_streams = audio_streams
                    .checked_add(1)
                    .ok_or(VideoMediaError::InvalidProbe)?;
            }
            _ => {}
        }
    }
    let primary = primary.ok_or(VideoMediaError::InvalidProbe)?;
    let (frame_rate_num, frame_rate_den) = parse_frame_rate(
        primary
            .avg_frame_rate
            .as_deref()
            .ok_or(VideoMediaError::InvalidProbe)?,
    )?;
    let (width, height) = primary
        .width
        .zip(primary.height)
        .filter(|(width, height)| *width > 0 && *height > 0)
        .ok_or(VideoMediaError::InvalidProbe)?;
    Ok(VideoProbe {
        duration_ms,
        width,
        height,
        frame_rate_num,
        frame_rate_den,
        video_streams,
        audio_streams,
    })
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

#[derive(Deserialize)]
struct RawProbe {
    streams: Vec<RawStream>,
    format: RawFormat,
}

#[derive(Deserialize)]
struct RawStream {
    codec_type: String,
    width: Option<u32>,
    height: Option<u32>,
    avg_frame_rate: Option<String>,
}

#[derive(Deserialize)]
struct RawFormat {
    duration: String,
}

async fn run_tool(
    program: &Path,
    arguments: &[OsString],
    timeout: Duration,
    max_output_bytes: usize,
) -> Result<Vec<u8>, VideoMediaError> {
    if !program.is_absolute() || timeout.is_zero() || max_output_bytes == 0 {
        return Err(VideoMediaError::ToolFailed);
    }
    let mut command = Command::new(program);
    command
        .args(arguments)
        .env_clear()
        .env("PATH", "/usr/local/bin:/usr/bin:/bin")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped())
        .kill_on_drop(true);
    let mut child = command.spawn().map_err(|_| VideoMediaError::ToolFailed)?;
    let stdout = child.stdout.take().ok_or(VideoMediaError::ToolFailed)?;
    let stderr = child.stderr.take().ok_or(VideoMediaError::ToolFailed)?;
    let total = Arc::new(AtomicUsize::new(0));
    let (limit_tx, mut limit_rx) = watch::channel(false);
    let _limit_guard = limit_tx.clone();
    let stdout_task = tokio::spawn(read_bounded(
        stdout,
        max_output_bytes,
        total.clone(),
        limit_tx.clone(),
    ));
    let stderr_task = tokio::spawn(read_bounded(stderr, max_output_bytes, total, limit_tx));
    let status = tokio::select! {
        status = child.wait() => status.map_err(|_| VideoMediaError::ToolFailed)?,
        () = tokio::time::sleep(timeout) => {
            let _ = child.kill().await;
            return Err(VideoMediaError::ToolTimedOut);
        }
        result = limit_rx.changed() => {
            let _ = result;
            let _ = child.kill().await;
            return Err(VideoMediaError::OutputTooLarge);
        }
    };
    let stdout = stdout_task.await.map_err(|_| VideoMediaError::ToolFailed)?;
    stderr_task
        .await
        .map_err(|_| VideoMediaError::ToolFailed)??;
    if !status.success() {
        return Err(VideoMediaError::ToolFailed);
    }
    stdout
}

async fn read_bounded(
    mut reader: impl AsyncRead + Unpin,
    max: usize,
    total: Arc<AtomicUsize>,
    limit: watch::Sender<bool>,
) -> Result<Vec<u8>, VideoMediaError> {
    let mut output = Vec::new();
    let mut buffer = [0_u8; 8_192];
    loop {
        let read = reader
            .read(&mut buffer)
            .await
            .map_err(|_| VideoMediaError::ToolFailed)?;
        if read == 0 {
            return Ok(output);
        }
        if total
            .fetch_update(Ordering::Relaxed, Ordering::Relaxed, |current| {
                current.checked_add(read).filter(|next| *next <= max)
            })
            .is_err()
        {
            let _ = limit.send(true);
            return Err(VideoMediaError::OutputTooLarge);
        }
        output.extend_from_slice(&buffer[..read]);
    }
}

fn parse_duration_ms(value: &str) -> Result<u64, VideoMediaError> {
    let seconds = value
        .parse::<f64>()
        .ok()
        .filter(|value| value.is_finite() && *value > 0.0)
        .ok_or(VideoMediaError::InvalidProbe)?;
    let millis = (seconds * 1_000.0).round();
    if millis > u64::MAX as f64 {
        return Err(VideoMediaError::InvalidProbe);
    }
    Ok(millis as u64)
}

fn parse_frame_rate(value: &str) -> Result<(u32, u32), VideoMediaError> {
    let (numerator, denominator) = value.split_once('/').ok_or(VideoMediaError::InvalidProbe)?;
    let parsed = (
        numerator.parse::<u32>().ok(),
        denominator.parse::<u32>().ok(),
    );
    match parsed {
        (Some(numerator), Some(denominator)) if numerator > 0 && denominator > 0 => {
            Ok((numerator, denominator))
        }
        _ => Err(VideoMediaError::InvalidProbe),
    }
}
