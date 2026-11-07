//! claude and codex adapters behind one `complete` call, failure classification, and the note step.

use std::path::Path;

use crate::pipeline::{StepFailure, StepResult};

pub async fn note(_job_dir: &Path, _out_dir: &Path) -> StepResult {
    Err(StepFailure::permanent("note is not implemented yet"))
}
