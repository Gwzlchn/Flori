use std::str::FromStr;

use super::super::StoreError;
use flori_core::{
    ArtifactId, ArtifactKind, ErrorCode, PromptSnapshot, ResolvedArtifact, ResolvedProfile,
    ResolvedPrompt, ResolvedSource, ResolvedSourceInput, ResolvedTaskInputs, Sha256Digest,
    SourceId, SourceInputId, SourceKind, TaskInputBindings, TaskInputReference,
};
use sqlx::{Row, Sqlite, Transaction};

pub(super) use super::credential::secret_inputs;

pub(super) async fn resolved_inputs(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
    bindings: &TaskInputBindings,
    download_base: &str,
) -> Result<ResolvedTaskInputs, StoreError> {
    let resolved = match bindings {
        TaskInputBindings::DocumentAcquire { source } => ResolvedTaskInputs::DocumentAcquire {
            source: resolve_source(transaction, job_id, source, download_base).await?,
        },
        TaskInputBindings::DocumentExtract { pdf } => ResolvedTaskInputs::DocumentExtract {
            pdf: one_artifact(transaction, job_id, pdf, download_base).await?,
        },
        TaskInputBindings::AiDocumentTranslate {
            document,
            prompt,
            profile,
        } => ResolvedTaskInputs::AiDocumentTranslate {
            document: one_artifact(transaction, job_id, document, download_base).await?,
            prompt: resolve_prompt(transaction, job_id, prompt).await?,
            profile: resolve_profile(transaction, job_id, profile.as_ref()).await?,
        },
        TaskInputBindings::AiDocumentNote {
            document,
            prompt,
            profile,
        } => ResolvedTaskInputs::AiDocumentNote {
            document: one_artifact(transaction, job_id, document, download_base).await?,
            prompt: resolve_prompt(transaction, job_id, prompt).await?,
            profile: resolve_profile(transaction, job_id, profile.as_ref()).await?,
        },
        TaskInputBindings::VideoAcquire { source } => ResolvedTaskInputs::VideoAcquire {
            source: resolve_source(transaction, job_id, source, download_base).await?,
        },
        TaskInputBindings::VideoSubscription { source } => ResolvedTaskInputs::VideoSubscription {
            source: resolve_source(transaction, job_id, source, download_base).await?,
        },
        TaskInputBindings::VideoTranscribe { video, subtitle } => {
            ResolvedTaskInputs::VideoTranscribe {
                video: one_artifact(transaction, job_id, video, download_base).await?,
                subtitle: match subtitle {
                    Some(reference) => {
                        optional_artifact(transaction, job_id, reference, download_base).await?
                    }
                    None => None,
                },
            }
        }
        TaskInputBindings::VideoFrames { video, transcript } => ResolvedTaskInputs::VideoFrames {
            video: one_artifact(transaction, job_id, video, download_base).await?,
            transcript: one_artifact(transaction, job_id, transcript, download_base).await?,
        },
        TaskInputBindings::VideoMechanicalNote { transcript, frames } => {
            ResolvedTaskInputs::VideoMechanicalNote {
                transcript: one_artifact(transaction, job_id, transcript, download_base).await?,
                frames: artifacts(transaction, job_id, frames, download_base).await?,
            }
        }
        TaskInputBindings::AiVideoNote {
            transcript,
            mechanical_note,
            frames,
            prompt,
            profile,
        } => ResolvedTaskInputs::AiVideoNote {
            transcript: one_artifact(transaction, job_id, transcript, download_base).await?,
            mechanical_note: one_artifact(transaction, job_id, mechanical_note, download_base)
                .await?,
            frames: artifacts(transaction, job_id, frames, download_base).await?,
            prompt: resolve_prompt(transaction, job_id, prompt).await?,
            profile: resolve_profile(transaction, job_id, profile.as_ref()).await?,
        },
        TaskInputBindings::CoreValidate { .. } | TaskInputBindings::CorePublish { .. } => {
            return Err(StoreError::new(ErrorCode::CorruptState));
        }
    };
    Ok(resolved)
}
async fn resolve_source(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
    reference: &TaskInputReference,
    base: &str,
) -> Result<ResolvedSource, StoreError> {
    if !matches!(reference, TaskInputReference::Source) {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    let row = sqlx::query(
        "SELECT s.id,s.kind,s.canonical_ref,i.id AS input_id,i.name,i.media_type,i.size_bytes, \
         i.sha256 FROM jobs j JOIN sources s ON s.id=j.source_id \
         LEFT JOIN source_inputs i ON i.source_id=s.id WHERE j.id=? ORDER BY i.name",
    )
    .bind(job_id)
    .fetch_all(&mut **transaction)
    .await?;
    if row.is_empty() || row.len() > 1 {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    let row = &row[0];
    let input_id: Option<String> = row.try_get("input_id")?;
    let input = input_id
        .map(|id| -> Result<ResolvedSourceInput, StoreError> {
            Ok(ResolvedSourceInput {
                source_input_id: SourceInputId::from_str(&id).map_err(|_| corrupt())?,
                name: row.try_get("name")?,
                media_type: row.try_get("media_type")?,
                size_bytes: to_u64(row.try_get("size_bytes")?)?,
                sha256: parse_digest(row.try_get("sha256")?)?,
                download_url: download_url(base, "source-inputs", &id),
            })
        })
        .transpose()?;
    Ok(ResolvedSource {
        source_id: SourceId::from_str(row.try_get("id")?).map_err(|_| corrupt())?,
        kind: parse_source_kind(row.try_get("kind")?)?,
        canonical_ref: row.try_get("canonical_ref")?,
        input,
    })
}
async fn one_artifact(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
    reference: &TaskInputReference,
    base: &str,
) -> Result<ResolvedArtifact, StoreError> {
    let mut values = artifacts(transaction, job_id, reference, base).await?;
    if values.len() != 1 {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    Ok(values.remove(0))
}
async fn optional_artifact(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
    reference: &TaskInputReference,
    base: &str,
) -> Result<Option<ResolvedArtifact>, StoreError> {
    let mut values = artifacts(transaction, job_id, reference, base).await?;
    if values.len() > 1 {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    Ok(values.pop())
}
async fn artifacts(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
    reference: &TaskInputReference,
    base: &str,
) -> Result<Vec<ResolvedArtifact>, StoreError> {
    let TaskInputReference::NeedArtifact { task, artifact } = reference else {
        return Err(StoreError::new(ErrorCode::CorruptState));
    };
    let prefix = format!("{artifact}/%");
    let rows = sqlx::query(
        "SELECT a.id,a.name,a.kind,a.media_type,a.size_bytes,a.sha256 FROM tasks t \
         JOIN artifacts a ON a.task_id=t.id WHERE t.job_id=? AND t.task_key=? \
         AND ((a.origin='produced' AND t.state='succeeded' \
               AND a.attempt_id=t.current_attempt_id) \
              OR (a.origin='materialized' AND t.state='skipped' AND a.attempt_id IS NULL)) \
         AND (a.name=? OR a.name LIKE ?) ORDER BY a.name",
    )
    .bind(job_id)
    .bind(task)
    .bind(artifact)
    .bind(prefix)
    .fetch_all(&mut **transaction)
    .await?;
    rows.into_iter()
        .map(|row| {
            let id: String = row.try_get("id")?;
            Ok(ResolvedArtifact {
                artifact_id: ArtifactId::from_str(&id).map_err(|_| corrupt())?,
                name: row.try_get("name")?,
                kind: parse_artifact_kind(row.try_get("kind")?)?,
                media_type: row.try_get("media_type")?,
                size_bytes: to_u64(row.try_get("size_bytes")?)?,
                sha256: parse_digest(row.try_get("sha256")?)?,
                download_url: download_url(base, "artifacts", &id),
            })
        })
        .collect()
}
async fn resolve_prompt(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
    reference: &TaskInputReference,
) -> Result<ResolvedPrompt, StoreError> {
    let TaskInputReference::Prompt(key) = reference else {
        return Err(StoreError::new(ErrorCode::CorruptState));
    };
    let snapshot = prompt_snapshot(transaction, job_id).await?;
    let prompt = snapshot
        .prompts
        .into_iter()
        .find(|prompt| prompt.key == *key)
        .ok_or_else(|| StoreError::new(ErrorCode::CorruptState))?;
    Ok(ResolvedPrompt {
        key: prompt.key,
        content: prompt.content,
        sha256: prompt.sha256,
    })
}
async fn resolve_profile(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
    reference: Option<&TaskInputReference>,
) -> Result<Option<ResolvedProfile>, StoreError> {
    let Some(reference) = reference else {
        return Ok(None);
    };
    if !matches!(reference, TaskInputReference::DomainProfile) {
        return Err(StoreError::new(ErrorCode::CorruptState));
    }
    let profile = prompt_snapshot(transaction, job_id).await?.profile;
    Ok(Some(ResolvedProfile {
        domain_id: profile.domain_id,
        content: profile.profile_text,
        sha256: profile.sha256,
    }))
}
async fn prompt_snapshot(
    transaction: &mut Transaction<'_, Sqlite>,
    job_id: &str,
) -> Result<PromptSnapshot, StoreError> {
    let json: String = sqlx::query_scalar("SELECT prompt_snapshot_json FROM jobs WHERE id=?")
        .bind(job_id)
        .fetch_optional(&mut **transaction)
        .await?
        .ok_or_else(|| StoreError::new(ErrorCode::CorruptState))?;
    serde_json::from_str(&json).map_err(|_| StoreError::new(ErrorCode::CorruptState))
}
fn download_url(base: &str, resource: &str, id: &str) -> String {
    format!("{base}/api/v1/{resource}/{id}/content")
}

fn to_u64(value: i64) -> Result<u64, StoreError> {
    value
        .try_into()
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn parse_source_kind(value: &str) -> Result<SourceKind, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}

fn parse_artifact_kind(value: &str) -> Result<ArtifactKind, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}

fn parse_digest(value: String) -> Result<Sha256Digest, StoreError> {
    Sha256Digest::parse(value).map_err(|_| corrupt())
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
