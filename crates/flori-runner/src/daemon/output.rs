use flori_core::{
    AiAudit, AiAuditSchema, AiResultEnvelope, AiTool, ArtifactDeclaration, ArtifactKind,
    ArtifactManifestEntry, ErrorCode, StartUploadRequest, TaskClaim, VerifyUploadRequest,
};
use sha2::{Digest, Sha256};

use crate::{RunnerClient, manifest_sha256};

use super::invoke::InvocationOutcome;

const UPLOAD_CHUNK_BYTES: usize = 1024 * 1024;

struct Output {
    declaration: ArtifactDeclaration,
    media_type: &'static str,
    bytes: Vec<u8>,
}

pub(super) async fn success(
    client: &RunnerClient,
    claim: &TaskClaim,
    tool: AiTool,
    invocation_keys: &[String],
    outcome: &InvocationOutcome,
) -> Result<(), ErrorCode> {
    let result = outcome.result.as_ref().map_err(|code| *code)?;
    let mut outputs = business_outputs(claim, result)?;
    outputs.push(audit_output(claim, tool, invocation_keys, outcome)?);
    let entries = upload_all(client, claim, outputs).await?;
    let manifest = manifest_sha256(claim.job_id, claim.task_id, claim.exec_id, entries)
        .map_err(|error| error.code())?;
    client
        .complete(claim.exec_id, manifest)
        .await
        .map_err(|error| error.code())?;
    Ok(())
}

pub(super) async fn failure(
    client: &RunnerClient,
    claim: &TaskClaim,
    tool: AiTool,
    invocation_keys: &[String],
    outcome: &InvocationOutcome,
    error_code: ErrorCode,
) -> Result<(), ErrorCode> {
    let audit = audit_output(claim, tool, invocation_keys, outcome)?;
    let manifest = match upload_all(client, claim, vec![audit]).await {
        Ok(entries) => Some(
            manifest_sha256(claim.job_id, claim.task_id, claim.exec_id, entries)
                .map_err(|error| error.code())?,
        ),
        Err(_) => None,
    };
    client
        .fail(
            claim.exec_id,
            &flori_core::FailAttemptRequest {
                error_code,
                manifest_sha256: manifest,
            },
        )
        .await
        .map_err(|error| error.code())?;
    Ok(())
}

fn business_outputs(
    claim: &TaskClaim,
    result: &AiResultEnvelope,
) -> Result<Vec<Output>, ErrorCode> {
    let mut outputs = Vec::new();
    for declaration in &claim.output_declarations {
        let bytes = match (declaration.kind, result) {
            (
                ArtifactKind::Translation,
                AiResultEnvelope::DocumentTranslate {
                    translation_markdown,
                    ..
                },
            ) => Some(translation_markdown.as_bytes().to_vec()),
            (
                ArtifactKind::SmartNote,
                AiResultEnvelope::DocumentNote {
                    smart_note_markdown,
                    ..
                }
                | AiResultEnvelope::VideoNote {
                    smart_note_markdown,
                    ..
                },
            ) => Some(smart_note_markdown.as_bytes().to_vec()),
            (
                ArtifactKind::Summary,
                AiResultEnvelope::DocumentNote {
                    summary_markdown, ..
                }
                | AiResultEnvelope::VideoNote {
                    summary_markdown, ..
                },
            ) => Some(summary_markdown.as_bytes().to_vec()),
            (
                ArtifactKind::Terms,
                AiResultEnvelope::DocumentNote { terms, .. }
                | AiResultEnvelope::VideoNote { terms, .. },
            ) => Some(serde_json::to_vec(terms).map_err(|_| ErrorCode::Internal)?),
            (ArtifactKind::AiAudit | ArtifactKind::TaskLog, _) => None,
            _ => return Err(ErrorCode::CorruptState),
        };
        if let Some(bytes) = bytes {
            outputs.push(output(
                declaration,
                if declaration.kind == ArtifactKind::Terms {
                    "application/json"
                } else {
                    "text/markdown"
                },
                bytes,
            )?);
        }
    }
    Ok(outputs)
}

fn audit_output(
    claim: &TaskClaim,
    tool: AiTool,
    invocation_keys: &[String],
    outcome: &InvocationOutcome,
) -> Result<Output, ErrorCode> {
    let declaration = unique(claim, ArtifactKind::AiAudit)?;
    let audit = AiAudit {
        schema: AiAuditSchema::V1,
        tool,
        model: claim.model.clone().ok_or(ErrorCode::CorruptState)?,
        effort: claim.effort.clone().ok_or(ErrorCode::CorruptState)?,
        prompt_snapshot_sha256: claim.prompt_snapshot_sha256.clone(),
        redacted_arguments: outcome.redacted_arguments.clone(),
        websearch_enabled: claim.executor != flori_core::Executor::AiDocumentTranslate,
        websearch_urls: outcome.websearch_urls.clone(),
        usage_invocation_keys: invocation_keys.to_vec(),
        exit_code: outcome.exit_code,
        timed_out: outcome.timed_out,
        output_sha256: outcome.output_sha256.clone(),
    };
    output(
        declaration,
        "application/json",
        serde_json::to_vec(&audit).map_err(|_| ErrorCode::Internal)?,
    )
}

fn unique(claim: &TaskClaim, kind: ArtifactKind) -> Result<&ArtifactDeclaration, ErrorCode> {
    let mut matching = claim
        .output_declarations
        .iter()
        .filter(|declaration| declaration.kind == kind);
    let declaration = matching.next().ok_or(ErrorCode::CorruptState)?;
    if matching.next().is_some() {
        return Err(ErrorCode::CorruptState);
    }
    Ok(declaration)
}

fn output(
    declaration: &ArtifactDeclaration,
    media_type: &'static str,
    bytes: Vec<u8>,
) -> Result<Output, ErrorCode> {
    if declaration.max_files.is_some()
        || !declaration.kind.accepts_media_type(media_type)
        || u64::try_from(bytes.len()).map_err(|_| ErrorCode::ArtifactTooLarge)?
            > declaration.max_bytes
    {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    Ok(Output {
        declaration: declaration.clone(),
        media_type,
        bytes,
    })
}

async fn upload_all(
    client: &RunnerClient,
    claim: &TaskClaim,
    outputs: Vec<Output>,
) -> Result<Vec<ArtifactManifestEntry>, ErrorCode> {
    let mut entries = Vec::with_capacity(outputs.len());
    for output in outputs {
        let sha256 = digest(&output.bytes)?;
        let size_bytes =
            u64::try_from(output.bytes.len()).map_err(|_| ErrorCode::ArtifactTooLarge)?;
        let request = StartUploadRequest {
            name: output.declaration.name.clone(),
            media_type: output.media_type.to_owned(),
            size_bytes,
            sha256: sha256.clone(),
        };
        let started = client
            .start_upload(claim.exec_id, &request)
            .await
            .map_err(|error| error.code())?;
        validate_entry(&started.artifact, &output.declaration, &request)?;
        let mut offset =
            usize::try_from(started.received_bytes).map_err(|_| ErrorCode::ArtifactTooLarge)?;
        if offset > output.bytes.len() {
            return Err(ErrorCode::CorruptState);
        }
        while offset < output.bytes.len() {
            let end = offset
                .saturating_add(UPLOAD_CHUNK_BYTES)
                .min(output.bytes.len());
            let cursor = client
                .append_upload_chunk(
                    started.upload_id,
                    u64::try_from(offset).map_err(|_| ErrorCode::ArtifactTooLarge)?,
                    output.bytes[offset..end].to_vec(),
                )
                .await
                .map_err(|error| error.code())?;
            if cursor.received_bytes
                != u64::try_from(end).map_err(|_| ErrorCode::ArtifactTooLarge)?
            {
                return Err(ErrorCode::CorruptState);
            }
            offset = end;
        }
        let verified = client
            .verify_upload(
                started.upload_id,
                &VerifyUploadRequest { size_bytes, sha256 },
            )
            .await
            .map_err(|error| error.code())?;
        if verified.upload_id != started.upload_id || verified.artifact != started.artifact {
            return Err(ErrorCode::CorruptState);
        }
        entries.push(verified.artifact);
    }
    Ok(entries)
}

fn validate_entry(
    entry: &ArtifactManifestEntry,
    declaration: &ArtifactDeclaration,
    request: &StartUploadRequest,
) -> Result<(), ErrorCode> {
    if entry.name != request.name
        || entry.kind != declaration.kind
        || entry.media_type != request.media_type
        || entry.size_bytes != request.size_bytes
        || entry.sha256 != request.sha256
        || entry.relative_path.is_empty()
    {
        return Err(ErrorCode::CorruptState);
    }
    Ok(())
}

fn digest(bytes: &[u8]) -> Result<flori_core::Sha256Digest, ErrorCode> {
    flori_core::Sha256Digest::parse(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .map_err(|_| ErrorCode::Internal)
}
