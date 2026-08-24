use std::{ffi::OsString, path::Path, time::Duration};

use serde::Deserialize;

use super::{VideoMediaError, process::run_tool};

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
    match (
        numerator.parse::<u32>().ok(),
        denominator.parse::<u32>().ok(),
    ) {
        (Some(numerator), Some(denominator)) if numerator > 0 && denominator > 0 => {
            Ok((numerator, denominator))
        }
        _ => Err(VideoMediaError::InvalidProbe),
    }
}
