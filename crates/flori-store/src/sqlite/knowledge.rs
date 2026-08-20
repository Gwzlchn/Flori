use std::{collections::BTreeMap, io::Read, str::FromStr};

use flori_core::{ArtifactId, ErrorCode, EvidenceLocator, EvidenceManifest, Sha256Digest};
use sqlx::{Row, Sqlite, Transaction};

use crate::artifact::NasArtifactStore;

use super::StoreError;

mod query;

struct ArtifactText {
    id: String,
    kind: String,
    body: String,
}

pub(super) async fn rebuild_source_projection(
    transaction: &mut Transaction<'_, Sqlite>,
    artifacts: &NasArtifactStore,
    source_id: &str,
    job_id: &str,
) -> Result<(), StoreError> {
    let rows = sqlx::query(
        "SELECT id,kind,size_bytes,sha256,relative_path FROM artifacts \
         WHERE source_id=? AND job_id=? AND retention='published' \
         AND kind IN ('evidence','smart_note','summary','translation','mechanical_note') \
         ORDER BY kind,id",
    )
    .bind(source_id)
    .bind(job_id)
    .fetch_all(&mut **transaction)
    .await?;

    let mut evidence_json = None;
    let mut text_artifacts = Vec::new();
    for row in rows {
        let text = read_verified_text(artifacts, &row)?;
        let kind: String = row.try_get("kind")?;
        let artifact = ArtifactText {
            id: row.try_get("id")?,
            kind: kind.clone(),
            body: text,
        };
        if kind == "evidence" {
            if evidence_json.replace(artifact).is_some() {
                return Err(StoreError::new(ErrorCode::EvidenceInvalid));
            }
        } else {
            text_artifacts.push(artifact);
        }
    }
    let evidence_json = evidence_json.ok_or_else(|| StoreError::new(ErrorCode::EvidenceInvalid))?;
    let manifest: EvidenceManifest = serde_json::from_str(&evidence_json.body)
        .map_err(|_| StoreError::new(ErrorCode::EvidenceInvalid))?;
    manifest
        .validate_structure()
        .map_err(|_| StoreError::new(ErrorCode::EvidenceInvalid))?;

    let artifact_kinds: BTreeMap<String, String> =
        sqlx::query("SELECT id,kind FROM artifacts WHERE source_id=? AND job_id=? ORDER BY id")
            .bind(source_id)
            .bind(job_id)
            .fetch_all(&mut **transaction)
            .await?
            .into_iter()
            .map(|row| Ok((row.try_get("id")?, row.try_get("kind")?)))
            .collect::<Result<_, sqlx::Error>>()?;
    validate_artifact_references(&manifest, &artifact_kinds)?;

    sqlx::query(
        "DELETE FROM search_chunk_evidence WHERE chunk_id IN \
         (SELECT chunk_id FROM search_chunks WHERE source_id=?)",
    )
    .bind(source_id)
    .execute(&mut **transaction)
    .await?;
    sqlx::query("DELETE FROM search_chunks WHERE source_id=?")
        .bind(source_id)
        .execute(&mut **transaction)
        .await?;
    sqlx::query("DELETE FROM evidence WHERE source_id=?")
        .bind(source_id)
        .execute(&mut **transaction)
        .await?;

    insert_evidence(transaction, source_id, job_id, &manifest).await?;
    insert_search_chunks(transaction, source_id, job_id, &manifest, text_artifacts).await
}

fn read_verified_text(
    artifacts: &NasArtifactStore,
    row: &sqlx::sqlite::SqliteRow,
) -> Result<String, StoreError> {
    let size_bytes: i64 = row.try_get("size_bytes").map_err(StoreError::from)?;
    let size_bytes =
        u64::try_from(size_bytes).map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
    let digest: String = row.try_get("sha256").map_err(StoreError::from)?;
    let digest =
        Sha256Digest::parse(digest).map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
    let path: String = row.try_get("relative_path").map_err(StoreError::from)?;
    let mut file = artifacts
        .open_verified_range(&path, size_bytes, &digest, 0, size_bytes)
        .map_err(|error| StoreError::new(error.code()))?;
    let capacity =
        usize::try_from(size_bytes).map_err(|_| StoreError::new(ErrorCode::ArtifactTooLarge))?;
    let mut body = String::with_capacity(capacity);
    file.read_to_string(&mut body)
        .map_err(|_| StoreError::new(ErrorCode::ArtifactInvalidPath))?;
    Ok(body)
}

fn validate_artifact_references(
    manifest: &EvidenceManifest,
    artifact_kinds: &BTreeMap<String, String>,
) -> Result<(), StoreError> {
    for item in &manifest.items {
        let source_id = item.source_artifact_id.to_string();
        if !artifact_kinds.contains_key(&source_id) {
            return Err(StoreError::new(ErrorCode::EvidenceInvalid));
        }
        if let EvidenceLocator::Video {
            keyframe: Some(keyframe),
            ..
        } = &item.locator
            && artifact_kinds
                .get(&keyframe.artifact_id.to_string())
                .map(String::as_str)
                != Some("keyframe")
        {
            return Err(StoreError::new(ErrorCode::EvidenceInvalid));
        }
    }
    Ok(())
}

async fn insert_evidence(
    transaction: &mut Transaction<'_, Sqlite>,
    source_id: &str,
    job_id: &str,
    manifest: &EvidenceManifest,
) -> Result<(), StoreError> {
    for item in &manifest.items {
        let mut query = sqlx::query(
            "INSERT INTO evidence(id,source_id,job_id,artifact_id,locator_kind,page,x1,y1,x2,y2,\
             start_ms,end_ms,keyframe_artifact_id,quote) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        )
        .bind(item.evidence_id.to_string())
        .bind(source_id)
        .bind(job_id)
        .bind(item.source_artifact_id.to_string());
        query = match &item.locator {
            EvidenceLocator::Pdf { page, bbox } => query
                .bind("pdf")
                .bind(i64::from(*page))
                .bind(bbox.x1)
                .bind(bbox.y1)
                .bind(bbox.x2)
                .bind(bbox.y2)
                .bind(Option::<i64>::None)
                .bind(Option::<i64>::None)
                .bind(Option::<String>::None),
            EvidenceLocator::Video {
                start_ms,
                end_ms,
                keyframe,
            } => query
                .bind("video")
                .bind(Option::<i64>::None)
                .bind(Option::<f64>::None)
                .bind(Option::<f64>::None)
                .bind(Option::<f64>::None)
                .bind(Option::<f64>::None)
                .bind(
                    i64::try_from(*start_ms)
                        .map_err(|_| StoreError::new(ErrorCode::EvidenceInvalid))?,
                )
                .bind(
                    i64::try_from(*end_ms)
                        .map_err(|_| StoreError::new(ErrorCode::EvidenceInvalid))?,
                )
                .bind(keyframe.as_ref().map(|frame| frame.artifact_id.to_string())),
        };
        query.bind(&item.quote).execute(&mut **transaction).await?;
    }
    Ok(())
}

async fn insert_search_chunks(
    transaction: &mut Transaction<'_, Sqlite>,
    source_id: &str,
    job_id: &str,
    manifest: &EvidenceManifest,
    artifacts: Vec<ArtifactText>,
) -> Result<(), StoreError> {
    let title: String = sqlx::query_scalar(
        "SELECT COALESCE(NULLIF(title,''),canonical_ref) FROM sources WHERE id=?",
    )
    .bind(source_id)
    .fetch_one(&mut **transaction)
    .await?;
    for artifact in artifacts {
        let artifact_id = ArtifactId::from_str(&artifact.id)
            .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
        let chunk_id = artifact_id.to_string();
        sqlx::query(
            "INSERT INTO search_chunks(chunk_id,source_id,job_id,artifact_id,title,body) \
             VALUES(?,?,?,?,?,?)",
        )
        .bind(&chunk_id)
        .bind(source_id)
        .bind(job_id)
        .bind(&artifact.id)
        .bind(format!("{title} [{}]", artifact.kind))
        .bind(&artifact.body)
        .execute(&mut **transaction)
        .await?;
        for evidence in &manifest.items {
            let marker = format!("[[evidence:{}]]", evidence.evidence_id);
            if artifact.body.contains(&marker) {
                sqlx::query("INSERT INTO search_chunk_evidence(chunk_id,evidence_id) VALUES(?,?)")
                    .bind(&chunk_id)
                    .bind(evidence.evidence_id.to_string())
                    .execute(&mut **transaction)
                    .await?;
            }
        }
    }
    Ok(())
}
