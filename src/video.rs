//! The three video steps (contract 1 in docs/contracts.md). video-fetch downloads with yt-dlp,
//! video-transcribe turns a subtitle or whisper output into transcript.json, video-frames picks
//! keyframes and renders sections.json and transcript.md. Tools run as child processes in the
//! step's process group, so the executor's timeout and cancel reach them.

mod fetch;
mod frames;
mod transcribe;

use std::{
    env, fs, io,
    path::{Path, PathBuf},
    process::{ExitStatus, Stdio},
};

use serde::{Deserialize, Serialize, de::DeserializeOwned};
use tokio::process::Command;

pub use fetch::fetch;
pub use frames::frames;
pub use transcribe::transcribe;

use crate::pipeline::{self, Manifest, StepFailure, StepName, StepResult};

/// transcript.json. Segments keep source order; whisper and SRT are both time ordered.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Transcript {
    language: Option<String>,
    segments: Vec<Segment>,
}

#[derive(Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Segment {
    start_ms: u64,
    end_ms: u64,
    text: String,
}

/// One entry of sections.json. Translate reads this type back.
#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Section {
    pub start_ms: u64,
    /// Basenames under video-frames' `assets/`.
    pub frames: Vec<String>,
    pub text: String,
}

/// Appends with a space only between two non-CJK characters, so Chinese text is not split by
/// spaces and English words are not glued together.
fn push_text(out: &mut String, piece: &str) {
    let piece = piece.split_whitespace().collect::<Vec<_>>().join(" ");
    if let (Some(prev), Some(next)) = (out.chars().last(), piece.chars().next())
        && !is_wide(prev)
        && !is_wide(next)
    {
        out.push(' ');
    }
    out.push_str(&piece);
}

/// CJK ideographs and punctuation, kana, hangul and full-width forms.
fn is_wide(c: char) -> bool {
    matches!(c, '\u{2E80}'..='\u{9FFF}' | '\u{AC00}'..='\u{D7AF}')
        || matches!(c, '\u{F900}'..='\u{FAFF}' | '\u{FF00}'..='\u{FFEF}')
}

fn env_value(name: &str) -> Option<String> {
    env::var(name).ok().filter(|v| !v.is_empty())
}

fn prior_step(job_dir: &Path, step: StepName) -> Result<(PathBuf, Manifest), StepFailure> {
    let dir = pipeline::find_step(job_dir, step)?
        .ok_or_else(|| StepFailure::permanent(format!("{step} output is missing")))?;
    let manifest = pipeline::read_manifest(&dir)?;
    Ok((dir, manifest))
}

fn logical_file(dir: &Path, manifest: &Manifest, name: &str) -> Result<PathBuf, StepFailure> {
    let path = manifest
        .files
        .get(name)
        .ok_or_else(|| StepFailure::permanent(format!("{} has no {name} file", dir.display())))?;
    Ok(dir.join(path))
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

fn read_json<T: DeserializeOwned>(path: &Path) -> Result<T, StepFailure> {
    let text = fs::read_to_string(path)?;
    serde_json::from_str(&text)
        .map_err(|e| StepFailure::permanent(format!("{}: {e}", path.display())))
}

fn write_json<T: Serialize>(path: &Path, value: &T) -> io::Result<()> {
    let mut text = serde_json::to_string_pretty(value).map_err(io::Error::other)?;
    text.push('\n');
    fs::write(path, text)
}

#[cfg(test)]
mod tests;
