//! Vault path, title cleaning, frontmatter, assembly in publish.tmp, and idempotent rename.

use std::path::Path;

use crate::pipeline::{StepFailure, StepResult};

pub async fn run(_job_dir: &Path, _out_dir: &Path) -> StepResult {
    Err(StepFailure::permanent("publish is not implemented yet"))
}
