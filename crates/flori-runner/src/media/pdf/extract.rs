use std::collections::BTreeSet;
use std::ffi::OsString;
use std::path::{Path, PathBuf};
use std::time::Duration;

use flori_core::{ArtifactKind, DocumentStructure, ErrorCode, ResolvedArtifact};
use sha2::{Digest, Sha256};
use tokio::fs::{self, OpenOptions};
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use super::process::run_bounded;
use super::scan::require_digital_pdf;

const EXTRACTOR: &[u8] = include_bytes!("extractor.py");
const PNG_MAGIC: &[u8] = b"\x89PNG\r\n\x1a\n";

#[derive(Clone, Debug)]
pub struct PdfExtractConfig {
    pub pdfinfo: PathBuf,
    pub pdftotext: PathBuf,
    pub python: PathBuf,
    pub timeout: Duration,
    pub max_probe_output_bytes: usize,
    pub max_structure_bytes: u64,
    pub max_asset_bytes: u64,
    pub max_assets: usize,
}

#[derive(Clone, Debug)]
pub struct PdfExtraction {
    pub structure: DocumentStructure,
    pub output_dir: PathBuf,
}

pub async fn extract_pdf(
    pdf: &ResolvedArtifact,
    input: &Path,
    output_dir: &Path,
    config: &PdfExtractConfig,
) -> Result<PdfExtraction, ErrorCode> {
    if pdf.kind != flori_core::ArtifactKind::SourceOriginal
        || pdf.media_type != "application/pdf"
        || config.max_probe_output_bytes == 0
        || config.max_structure_bytes == 0
        || config.max_asset_bytes == 0
        || config.max_assets == 0
        || !input.is_absolute()
        || !output_dir.is_absolute()
    {
        return Err(ErrorCode::CorruptState);
    }
    verify_input(input, pdf).await?;
    require_digital_pdf(
        &config.pdfinfo,
        &config.pdftotext,
        input,
        config.timeout,
        config.max_probe_output_bytes,
    )
    .await?;
    let result = extract_inner(pdf, input, output_dir, config).await;
    if result.is_err() {
        let _ = fs::remove_dir_all(output_dir).await;
    }
    result
}

async fn extract_inner(
    pdf: &ResolvedArtifact,
    input: &Path,
    output_dir: &Path,
    config: &PdfExtractConfig,
) -> Result<PdfExtraction, ErrorCode> {
    fs::create_dir(output_dir)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    for name in ["figures", "tables"] {
        fs::create_dir(output_dir.join(name))
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
    }
    let script = output_dir.join("extractor.py");
    let mut script_file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&script)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    script_file
        .write_all(EXTRACTOR)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    script_file
        .sync_all()
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    drop(script_file);
    let arguments = [
        OsString::from("-I"),
        script.as_os_str().to_owned(),
        input.as_os_str().to_owned(),
        output_dir.as_os_str().to_owned(),
        OsString::from(pdf.artifact_id.to_string()),
    ];
    let process = run_bounded(&config.python, &arguments, config.timeout, 64 * 1024).await;
    fs::remove_file(&script)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let process = process?;
    if !process.stdout.is_empty() || !process.stderr.is_empty() {
        return Err(ErrorCode::ExecutorFailed);
    }
    let document_path = output_dir.join("document.json");
    let bytes = read_limited(&document_path, config.max_structure_bytes).await?;
    let structure: DocumentStructure =
        serde_json::from_slice(&bytes).map_err(|_| ErrorCode::ExecutorFailed)?;
    if structure.source_artifact_id != pdf.artifact_id || structure.validate().is_err() {
        return Err(ErrorCode::ExecutorFailed);
    }
    validate_outputs(output_dir, &structure, config).await?;
    let normalized = serde_json::to_vec(&structure).map_err(|_| ErrorCode::Internal)?;
    if u64::try_from(normalized.len()).map_err(|_| ErrorCode::ArtifactTooLarge)?
        > config.max_structure_bytes
    {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    fs::write(&document_path, normalized)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    Ok(PdfExtraction {
        structure,
        output_dir: output_dir.to_owned(),
    })
}

async fn verify_input(path: &Path, pdf: &ResolvedArtifact) -> Result<(), ErrorCode> {
    let metadata = fs::symlink_metadata(path)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() != pdf.size_bytes
    {
        return Err(ErrorCode::DigestMismatch);
    }
    let mut file = fs::File::open(path)
        .await
        .map_err(|_| ErrorCode::StorageUnavailable)?;
    let mut digest = Sha256::new();
    let mut prefix = [0_u8; 5];
    file.read_exact(&mut prefix)
        .await
        .map_err(|_| ErrorCode::UnsupportedSource)?;
    digest.update(prefix);
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .await
            .map_err(|_| ErrorCode::StorageUnavailable)?;
        if count == 0 {
            break;
        }
        digest.update(&buffer[..count]);
    }
    let actual = digest
        .finalize()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect::<String>();
    if prefix != *b"%PDF-" || actual != pdf.sha256.as_str() {
        return Err(ErrorCode::DigestMismatch);
    }
    Ok(())
}

async fn validate_outputs(
    root: &Path,
    structure: &DocumentStructure,
    config: &PdfExtractConfig,
) -> Result<(), ErrorCode> {
    let expected = structure
        .figures
        .iter()
        .map(|item| (ArtifactKind::Figure, item.artifact_name.as_str()))
        .chain(
            structure
                .tables
                .iter()
                .map(|item| (ArtifactKind::TableRegion, item.artifact_name.as_str())),
        )
        .collect::<Vec<_>>();
    if expected.len() > config.max_assets {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    let mut names = BTreeSet::new();
    for (kind, name) in expected {
        let prefix = if kind == ArtifactKind::Figure {
            "figures/"
        } else {
            "tables/"
        };
        let basename = name
            .strip_prefix(prefix)
            .ok_or(ErrorCode::ArtifactInvalidPath)?;
        if basename.is_empty() || basename.contains('/') || !basename.ends_with(".png") {
            return Err(ErrorCode::ArtifactInvalidPath);
        }
        names.insert(name.to_owned());
        validate_png(&root.join(name), config.max_asset_bytes).await?;
    }
    let found = list_outputs(root).await?;
    if names != found {
        return Err(ErrorCode::ArtifactUndeclared);
    }
    Ok(())
}

async fn validate_png(path: &Path, max_bytes: u64) -> Result<(), ErrorCode> {
    let metadata = fs::symlink_metadata(path)
        .await
        .map_err(|_| ErrorCode::ArtifactInvalidPath)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() == 0 {
        return Err(ErrorCode::ArtifactInvalidPath);
    }
    if metadata.len() > max_bytes {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    let mut magic = [0_u8; 8];
    fs::File::open(path)
        .await
        .map_err(|_| ErrorCode::ArtifactInvalidPath)?
        .read_exact(&mut magic)
        .await
        .map_err(|_| ErrorCode::ArtifactInvalidPath)?;
    if magic != PNG_MAGIC {
        return Err(ErrorCode::ExecutorFailed);
    }
    Ok(())
}

async fn list_outputs(root: &Path) -> Result<BTreeSet<String>, ErrorCode> {
    let mut names = BTreeSet::new();
    for directory in ["figures", "tables"] {
        let mut entries = fs::read_dir(root.join(directory))
            .await
            .map_err(|_| ErrorCode::ArtifactInvalidPath)?;
        while let Some(entry) = entries
            .next_entry()
            .await
            .map_err(|_| ErrorCode::ArtifactInvalidPath)?
        {
            let name = entry
                .file_name()
                .into_string()
                .map_err(|_| ErrorCode::ArtifactInvalidPath)?;
            names.insert(format!("{directory}/{name}"));
        }
    }
    let mut root_entries = fs::read_dir(root)
        .await
        .map_err(|_| ErrorCode::ArtifactInvalidPath)?;
    let mut roots = BTreeSet::new();
    while let Some(entry) = root_entries
        .next_entry()
        .await
        .map_err(|_| ErrorCode::ArtifactInvalidPath)?
    {
        roots.insert(
            entry
                .file_name()
                .into_string()
                .map_err(|_| ErrorCode::ArtifactInvalidPath)?,
        );
    }
    if roots != BTreeSet::from(["document.json".into(), "figures".into(), "tables".into()]) {
        return Err(ErrorCode::ArtifactUndeclared);
    }
    Ok(names)
}

async fn read_limited(path: &Path, max_bytes: u64) -> Result<Vec<u8>, ErrorCode> {
    let metadata = fs::symlink_metadata(path)
        .await
        .map_err(|_| ErrorCode::ExecutorFailed)?;
    if metadata.file_type().is_symlink() || !metadata.is_file() || metadata.len() > max_bytes {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    fs::read(path).await.map_err(|_| ErrorCode::ExecutorFailed)
}
