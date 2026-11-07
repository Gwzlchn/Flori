//! Chinese detection, chunking with placeholders, per-chunk cache, validation with one re-ask,
//! and the two output layouts (arXiv translation.md, bilingual video transcript.md).

use std::path::Path;

use crate::pipeline::{StepFailure, StepResult};

pub async fn run(_job_dir: &Path, _out_dir: &Path) -> StepResult {
    Err(StepFailure::permanent("translate is not implemented yet"))
}
