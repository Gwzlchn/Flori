//! The three video steps (contract 1 in docs/contracts.md). video-fetch downloads with yt-dlp,
//! video-transcribe turns a subtitle or whisper output into transcript.json, video-frames picks
//! keyframes and renders sections.json and transcript.md. Tools run as child processes in the
//! step's process group, so the executor's timeout and cancel reach them.

mod fetch;
mod frames;
mod transcribe;

use std::{
    env,
    process::{ExitStatus, Stdio},
};

use serde::{Deserialize, Serialize};
use tokio::process::Command;

pub use fetch::fetch;
pub use frames::frames;
pub(crate) use transcribe::parse_srt;
pub use transcribe::transcribe;

use crate::pipeline::{StepFailure, StepResult};

/// transcript.json. Segments keep source order; whisper and SRT are both time ordered.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Transcript {
    language: Option<String>,
    segments: Vec<Segment>,
}

#[derive(Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Segment {
    pub(crate) start_ms: u64,
    end_ms: u64,
    pub(crate) text: String,
}

/// Appends with a space only between two non-CJK characters, so Chinese text is not split by
/// spaces and English words are not glued together.
pub(crate) fn push_text(out: &mut String, piece: &str) {
    let piece = piece.split_whitespace().collect::<Vec<_>>().join(" ");
    if let (Some(prev), Some(next)) = (out.chars().last(), piece.chars().next())
        && !is_wide(prev)
        && !is_wide(next)
    {
        out.push(' ');
    }
    out.push_str(&piece);
}

/// CJK ideographs and punctuation, kana and full-width forms. Hangul is left out: Korean
/// separates words with spaces.
fn is_wide(c: char) -> bool {
    matches!(c, '\u{2E80}'..='\u{9FFF}' | '\u{F900}'..='\u{FAFF}' | '\u{FF00}'..='\u{FFEF}')
}

fn env_value(name: &str) -> Option<String> {
    env::var(name).ok().filter(|v| !v.is_empty())
}

/// Media tools on a finished download fail deterministically; only a signal (OOM kill) is retried.
async fn run_checked(mut cmd: Command, tool: &str) -> StepResult {
    let status = cmd
        .stdin(Stdio::null())
        .status()
        .await
        .map_err(|e| StepFailure::permanent(format!("{tool}: {e}")))?;
    if status.success() {
        Ok(())
    } else {
        Err(exit_failure(tool, status))
    }
}

fn exit_failure(tool: &str, status: ExitStatus) -> StepFailure {
    match status.code() {
        Some(code) => StepFailure::permanent(format!("{tool} exited with {code}")),
        None => StepFailure::retry(format!("{tool} was killed by a signal")),
    }
}

#[cfg(test)]
mod tests;
