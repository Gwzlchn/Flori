use std::{ffi::OsString, path::Path, time::Duration};

use super::{VideoMediaError, process::run_tool_output};

const MIN_SCENE_MS: u64 = 2_000;
pub(super) const LONG_SCENE_MS: u64 = 60_000;
pub(super) const FORCED_INTERVAL_MS: u64 = 15_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct Scene {
    pub(super) start_ms: u64,
    pub(super) end_ms: u64,
}

pub(super) async fn detect_scenes(
    ffmpeg: &Path,
    input: &Path,
    duration_ms: u64,
    timeout: Duration,
    max_output_bytes: usize,
) -> Result<Vec<Scene>, VideoMediaError> {
    if duration_ms < 2 {
        return Err(VideoMediaError::InvalidFrameRequest);
    }
    let arguments = [
        OsString::from("-hide_banner"),
        OsString::from("-loglevel"),
        OsString::from("info"),
        OsString::from("-nostdin"),
        OsString::from("-i"),
        input.as_os_str().to_owned(),
        OsString::from("-an"),
        OsString::from("-vf"),
        OsString::from("select=gt(scene\\,0.30),showinfo"),
        OsString::from("-f"),
        OsString::from("null"),
        OsString::from("-"),
    ];
    let output = run_tool_output(ffmpeg, &arguments, timeout, max_output_bytes).await?;
    let stderr = std::str::from_utf8(&output.stderr).map_err(|_| VideoMediaError::ToolFailed)?;
    let mut boundaries = vec![0];
    for line in stderr.lines() {
        let Some(value) = line
            .split_ascii_whitespace()
            .find_map(|field| field.strip_prefix("pts_time:"))
        else {
            continue;
        };
        let millis = seconds_to_millis(value)?;
        let previous = *boundaries.last().ok_or(VideoMediaError::ToolFailed)?;
        if millis < duration_ms && millis.saturating_sub(previous) >= MIN_SCENE_MS {
            boundaries.push(millis);
        }
    }
    close_timeline(&mut boundaries, duration_ms);
    Ok(boundaries
        .windows(2)
        .filter_map(|values| {
            (values[0] < values[1]).then_some(Scene {
                start_ms: values[0],
                end_ms: values[1],
            })
        })
        .collect())
}

fn close_timeline(boundaries: &mut Vec<u64>, duration_ms: u64) {
    match boundaries.last_mut() {
        Some(last) if duration_ms.saturating_sub(*last) < MIN_SCENE_MS => *last = duration_ms,
        _ => boundaries.push(duration_ms),
    }
}

fn seconds_to_millis(value: &str) -> Result<u64, VideoMediaError> {
    if value.starts_with('-') || value.is_empty() || value.contains(['e', 'E']) {
        return Err(VideoMediaError::ToolFailed);
    }
    let (whole, fraction) = value.split_once('.').unwrap_or((value, ""));
    let seconds = whole
        .parse::<u64>()
        .map_err(|_| VideoMediaError::ToolFailed)?;
    let mut fraction_ms = 0_u64;
    for (index, byte) in fraction.bytes().take(3).enumerate() {
        if !byte.is_ascii_digit() {
            return Err(VideoMediaError::ToolFailed);
        }
        fraction_ms += u64::from(byte - b'0') * [100, 10, 1][index];
    }
    seconds
        .checked_mul(1_000)
        .and_then(|value| value.checked_add(fraction_ms))
        .ok_or(VideoMediaError::ToolFailed)
}

#[cfg(test)]
mod tests {
    use super::{close_timeline, seconds_to_millis};

    #[test]
    fn parses_bounded_ffmpeg_timestamps() {
        assert_eq!(seconds_to_millis("12.34567"), Ok(12_345));
        assert!(seconds_to_millis("1e3").is_err());
        assert!(seconds_to_millis("-1.0").is_err());
    }

    #[test]
    fn merges_an_undecodable_short_tail_into_the_previous_scene() {
        let mut boundaries = vec![0, 697_238];
        close_timeline(&mut boundaries, 697_301);
        assert_eq!(boundaries, vec![0, 697_301]);
    }
}
