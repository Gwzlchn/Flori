//! video-fetch, subtitle normalization, whisper transcription, keyframes with similarity dedup,
//! sections.json and transcript.md.

use std::path::Path;

use crate::pipeline::{StepFailure, StepResult};

pub async fn fetch(_job_dir: &Path, _out_dir: &Path) -> StepResult {
    Err(StepFailure::permanent("video-fetch is not implemented yet"))
}

pub async fn transcribe(_job_dir: &Path, _out_dir: &Path) -> StepResult {
    Err(StepFailure::permanent(
        "video-transcribe is not implemented yet",
    ))
}

pub async fn frames(_job_dir: &Path, _out_dir: &Path) -> StepResult {
    Err(StepFailure::permanent(
        "video-frames is not implemented yet",
    ))
}
