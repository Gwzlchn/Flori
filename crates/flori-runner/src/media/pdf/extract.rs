use std::path::{Path, PathBuf};
use std::time::Duration;

use flori_core::{DocumentStructure, ErrorCode, ResolvedArtifact};

use super::scan::require_digital_pdf;

#[derive(Clone, Debug)]
pub struct PdfExtractConfig {
    pub pdfinfo: PathBuf,
    pub pdftotext: PathBuf,
    pub python: PathBuf,
    pub timeout: Duration,
    pub max_probe_output_bytes: usize,
    pub max_structure_bytes: u64,
}

#[derive(Clone, Debug)]
pub struct PdfExtraction {
    pub structure: DocumentStructure,
    pub output_dir: PathBuf,
}

pub async fn extract_pdf(
    pdf: &ResolvedArtifact,
    input: &Path,
    _output_dir: &Path,
    config: &PdfExtractConfig,
) -> Result<PdfExtraction, ErrorCode> {
    if pdf.kind != flori_core::ArtifactKind::SourceOriginal
        || pdf.media_type != "application/pdf"
        || config.max_probe_output_bytes == 0
        || config.max_structure_bytes == 0
    {
        return Err(ErrorCode::CorruptState);
    }
    require_digital_pdf(
        &config.pdfinfo,
        &config.pdftotext,
        input,
        config.timeout,
        config.max_probe_output_bytes,
    )
    .await?;
    Err(ErrorCode::ToolTemporarilyUnavailable)
}
