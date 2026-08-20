use std::path::Path;

use flori_core::{
    ArtifactDeclaration, ArtifactKind, ArtifactWhen, ErrorCode, Executor, ResolvedTaskInputs,
    SourceKind, TaskClaim,
};

pub(super) fn validate(claim: &TaskClaim) -> Result<(), ErrorCode> {
    if claim.timeout_ms == 0
        || claim.attempt_no == 0
        || claim.task_key.is_empty()
        || claim.model.is_some()
        || claim.effort.is_some()
        || claim.secret_inputs.credential.is_some()
    {
        return Err(ErrorCode::CorruptState);
    }
    match (&claim.executor, &claim.resolved_inputs) {
        (Executor::DocumentAcquire, ResolvedTaskInputs::DocumentAcquire { source }) => {
            if !matches!(
                source.kind,
                SourceKind::Arxiv | SourceKind::PdfUrl | SourceKind::PdfUpload
            ) {
                return Err(ErrorCode::UnsupportedSource);
            }
            shape(
                claim,
                &[(
                    ArtifactKind::SourceOriginal,
                    "original",
                    "output/source.pdf",
                    true,
                    false,
                )],
            )
        }
        (Executor::DocumentExtract, ResolvedTaskInputs::DocumentExtract { pdf }) => {
            if pdf.kind != ArtifactKind::SourceOriginal || pdf.media_type != "application/pdf" {
                return Err(ErrorCode::CorruptState);
            }
            shape(
                claim,
                &[
                    (
                        ArtifactKind::DocumentStructure,
                        "structure",
                        "output/document.json",
                        true,
                        false,
                    ),
                    (
                        ArtifactKind::Figure,
                        "figures",
                        "output/figures/*",
                        false,
                        true,
                    ),
                    (
                        ArtifactKind::TableRegion,
                        "tables",
                        "output/tables/*",
                        false,
                        true,
                    ),
                ],
            )
        }
        _ => Err(ErrorCode::CorruptState),
    }
}

fn shape(
    claim: &TaskClaim,
    business: &[(ArtifactKind, &str, &str, bool, bool)],
) -> Result<(), ErrorCode> {
    if claim.output_declarations.len() != business.len() + 1 {
        return Err(ErrorCode::CorruptState);
    }
    for &(kind, name, path, required, many) in business {
        let declaration = exact(claim, kind)?;
        if declaration.name != name
            || declaration.path != path
            || declaration.required != required
            || declaration.when != ArtifactWhen::OnSuccess
            || declaration.max_bytes == 0
            || declaration.max_files.is_some() != many
            || declaration.max_files == Some(0)
        {
            return Err(ErrorCode::CorruptState);
        }
    }
    let log = exact(claim, ArtifactKind::TaskLog)?;
    if log.name != "log"
        || log.path != "logs/task.ndjson"
        || !log.required
        || log.when != ArtifactWhen::Always
        || log.max_files.is_some()
        || log.max_bytes == 0
    {
        return Err(ErrorCode::CorruptState);
    }
    Ok(())
}

pub(super) fn exact(
    claim: &TaskClaim,
    kind: ArtifactKind,
) -> Result<&ArtifactDeclaration, ErrorCode> {
    let mut declarations = claim
        .output_declarations
        .iter()
        .filter(|item| item.kind == kind);
    let declaration = declarations.next().ok_or(ErrorCode::CorruptState)?;
    if declarations.next().is_some() {
        return Err(ErrorCode::CorruptState);
    }
    Ok(declaration)
}

pub(super) fn basename(name: &str) -> Result<&str, ErrorCode> {
    Path::new(name)
        .file_name()
        .and_then(|value| value.to_str())
        .filter(|value| {
            !value.is_empty() && !value.starts_with('.') && !value.contains(['/', '\\', '\0'])
        })
        .ok_or(ErrorCode::ArtifactInvalidPath)
}
