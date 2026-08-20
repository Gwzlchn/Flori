use flori_core::{ArtifactId, TranscriptCue, TranscriptManifest, TranscriptSchema};

use super::VideoMediaError;

pub(crate) fn normalize_srt(
    source_artifact_id: ArtifactId,
    language: &str,
    duration_ms: u64,
    subtitle: &str,
) -> Result<TranscriptManifest, VideoMediaError> {
    let normalized = subtitle
        .trim_start_matches('\u{feff}')
        .replace("\r\n", "\n");
    let mut cues = Vec::new();
    for (expected_index, block) in normalized.split("\n\n").enumerate() {
        let mut lines = block.lines().filter(|line| !line.trim().is_empty());
        let index = lines
            .next()
            .and_then(|value| value.trim().parse::<usize>().ok())
            .ok_or(VideoMediaError::InvalidSubtitle)?;
        if index != expected_index + 1 {
            return Err(VideoMediaError::InvalidSubtitle);
        }
        let timing = lines.next().ok_or(VideoMediaError::InvalidSubtitle)?;
        let (start, end) = timing
            .split_once(" --> ")
            .ok_or(VideoMediaError::InvalidSubtitle)?;
        let text = lines
            .flat_map(str::split_whitespace)
            .collect::<Vec<_>>()
            .join(" ");
        cues.push(TranscriptCue {
            start_ms: parse_srt_time(start)?,
            end_ms: parse_srt_time(end)?,
            text,
        });
    }
    let transcript = TranscriptManifest {
        schema: TranscriptSchema::V1,
        source_artifact_id,
        language: language.to_owned(),
        duration_ms,
        cues,
    };
    transcript
        .validate()
        .map_err(|_| VideoMediaError::InvalidSubtitle)?;
    Ok(transcript)
}

pub(crate) fn mechanical_note(transcript: &TranscriptManifest) -> Result<String, VideoMediaError> {
    transcript
        .validate()
        .map_err(|_| VideoMediaError::InvalidSubtitle)?;
    let mut note = String::from("# 机械笔记\n\n");
    for cue in &transcript.cues {
        note.push_str(&format!(
            "- {}-{}: {}\n",
            display_time(cue.start_ms),
            display_time(cue.end_ms),
            cue.text.trim()
        ));
    }
    note.push_str("\n本笔记只重组字幕中的事实，不添加原因、评价或外部知识。\n");
    Ok(note)
}

fn parse_srt_time(value: &str) -> Result<u64, VideoMediaError> {
    let (clock, millis) = value
        .trim()
        .split_once(',')
        .ok_or(VideoMediaError::InvalidSubtitle)?;
    let mut fields = clock.split(':');
    let hours = fields.next().and_then(|part| part.parse::<u64>().ok());
    let minutes = fields.next().and_then(|part| part.parse::<u64>().ok());
    let seconds = fields.next().and_then(|part| part.parse::<u64>().ok());
    let millis = millis.parse::<u64>().ok();
    match (hours, minutes, seconds, millis, fields.next()) {
        (Some(hours), Some(minutes), Some(seconds), Some(millis), None)
            if minutes < 60 && seconds < 60 && millis < 1_000 =>
        {
            hours
                .checked_mul(3_600_000)
                .and_then(|value| value.checked_add(minutes * 60_000))
                .and_then(|value| value.checked_add(seconds * 1_000))
                .and_then(|value| value.checked_add(millis))
                .ok_or(VideoMediaError::InvalidSubtitle)
        }
        _ => Err(VideoMediaError::InvalidSubtitle),
    }
}

fn display_time(timestamp_ms: u64) -> String {
    let total_seconds = timestamp_ms / 1_000;
    format!(
        "{:02}:{:02}:{:02}.{:03}",
        total_seconds / 3_600,
        total_seconds / 60 % 60,
        total_seconds % 60,
        timestamp_ms % 1_000
    )
}
