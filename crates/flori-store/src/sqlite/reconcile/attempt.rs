use std::{path::Path, str::FromStr};

use flori_core::{
    ArtifactDeclaration, ArtifactKind, AttemptId, CompiledTaskSpec, ErrorCode,
    PendingAttemptUpload, Sha256Digest, UploadState,
};
use sqlx::{Row, sqlite::SqliteRow};

use crate::artifact::{NasArtifactStore, RecoveryAction, UploadRecord, task_artifact_path};

use crate::sqlite::{Store, StoreError};

use super::server_log;

pub(super) async fn reconcile(
    store: &Store,
    artifacts: &NasArtifactStore,
    owner_id: &str,
    now_ms: i64,
) -> Result<(), StoreError> {
    let attempt_id = AttemptId::from_str(owner_id).map_err(|_| corrupt())?;
    let mut transaction = store.pool.begin_with("BEGIN IMMEDIATE").await?;
    let rows = load_rows(&mut transaction, owner_id).await?;
    if rows.is_empty() {
        let orphaned: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM uploads WHERE owner_kind='attempt' AND owner_id=?",
        )
        .bind(owner_id)
        .fetch_one(&mut *transaction)
        .await?;
        if orphaned != 0 {
            return Err(corrupt());
        }
        transaction.rollback().await?;
        return Ok(());
    }
    let spec_json: String = rows[0].try_get("spec_json")?;
    let spec: CompiledTaskSpec = serde_json::from_str(&spec_json).map_err(|_| corrupt())?;
    if serde_json::to_string(&spec).map_err(|_| corrupt())? != spec_json {
        return Err(corrupt());
    }
    for row in &rows {
        if row.try_get::<String, _>("spec_json")? != spec_json {
            return Err(corrupt());
        }
    }
    let active = active_owner(&rows[0], owner_id, now_ms)?;
    let mut records = Vec::with_capacity(rows.len());
    for row in &rows {
        ensure_no_target(&mut transaction, row).await?;
        let upload_id = row
            .try_get::<String, _>("id")?
            .parse()
            .map_err(|_| corrupt())?;
        records.push(decode_record(row, upload_id, &spec)?);
    }

    if !active {
        for record in &records {
            artifacts
                .discard(record)
                .map_err(|error| stored_artifact_error(error.code()))?;
        }
        let deleted = sqlx::query("DELETE FROM uploads WHERE owner_kind='attempt' AND owner_id=?")
            .bind(owner_id)
            .execute(&mut *transaction)
            .await?;
        if deleted.rows_affected() != rows.len() as u64 {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        transaction.commit().await?;
        return Ok(());
    }

    for (row, record) in rows.iter().zip(&records) {
        if row.try_get::<Option<String>, _>("commit_json")?.is_none() {
            server_log::reconcile_open_log(
                &mut transaction,
                artifacts,
                row,
                record,
                attempt_id,
                now_ms,
            )
            .await?;
        } else {
            apply_action(
                &mut transaction,
                artifacts,
                row.try_get("id")?,
                record,
                now_ms,
            )
            .await?;
        }
    }
    transaction.commit().await?;
    Ok(())
}

async fn load_rows(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    owner_id: &str,
) -> Result<Vec<SqliteRow>, StoreError> {
    sqlx::query(
        "SELECT u.id,u.commit_json,u.name,u.target_id,u.staging_path,u.final_relative_path, \
         u.expected_size_bytes,u.expected_sha256,u.received_bytes,u.state,a.id AS attempt_id, \
         a.runner_id,a.state AS attempt_state,a.lease_expires_at_ms,a.last_log_sequence, \
         t.id AS task_id, \
         t.state AS task_state,t.current_attempt_id,t.spec_json,j.id AS job_id, \
         j.state AS job_state,j.source_id FROM uploads u JOIN attempts a ON a.id=u.owner_id \
         JOIN tasks t ON t.id=a.task_id JOIN jobs j ON j.id=t.job_id \
         WHERE u.owner_kind='attempt' AND u.owner_id=? ORDER BY u.id",
    )
    .bind(owner_id)
    .fetch_all(&mut **transaction)
    .await
    .map_err(Into::into)
}

fn active_owner(row: &SqliteRow, owner_id: &str, now_ms: i64) -> Result<bool, StoreError> {
    Ok(row.try_get::<String, _>("attempt_id")? == owner_id
        && row.try_get::<Option<String>, _>("runner_id")?.is_some()
        && row.try_get::<String, _>("attempt_state")? == "leased"
        && row.try_get::<String, _>("task_state")? == "leased"
        && row.try_get::<String, _>("job_state")? == "running"
        && row
            .try_get::<Option<String>, _>("current_attempt_id")?
            .as_deref()
            == Some(owner_id)
        && row.try_get::<i64, _>("lease_expires_at_ms")? > now_ms)
}

fn decode_record(
    row: &SqliteRow,
    upload_id: flori_core::UploadId,
    spec: &CompiledTaskSpec,
) -> Result<UploadRecord, StoreError> {
    let name: String = row.try_get("name")?;
    let (declared, basename) = declaration(spec, &name)?;
    let commit: Option<String> = row.try_get("commit_json")?;
    let (size, sha256, declaration_name, final_path) = if let Some(json) = commit {
        let pending: PendingAttemptUpload = serde_json::from_str(&json).map_err(|_| corrupt())?;
        if serde_json::to_string(&pending).map_err(|_| corrupt())? != json
            || pending.artifact_id.to_string() != row.try_get::<String, _>("target_id")?
            || pending.artifact.name != name
            || pending.declaration_name != declared.name
            || pending.artifact.kind != declared.kind
            || !declared
                .kind
                .accepts_media_type(&pending.artifact.media_type)
            || i64::try_from(pending.artifact.size_bytes).map_err(|_| corrupt())?
                != row.try_get::<i64, _>("expected_size_bytes")?
            || pending.artifact.sha256.as_str() != row.try_get::<String, _>("expected_sha256")?
        {
            return Err(corrupt());
        }
        (
            pending.artifact.size_bytes,
            pending.artifact.sha256,
            pending.declaration_name,
            pending.artifact.relative_path,
        )
    } else {
        if declared.kind != ArtifactKind::TaskLog
            || row.try_get::<String, _>("state")? != "receiving"
            || row.try_get::<i64, _>("expected_size_bytes")?
                != i64::try_from(declared.max_bytes).map_err(|_| corrupt())?
        {
            return Err(corrupt());
        }
        let final_path = task_artifact_path(
            row.try_get::<String, _>("source_id")?
                .parse()
                .map_err(|_| corrupt())?,
            row.try_get::<String, _>("job_id")?
                .parse()
                .map_err(|_| corrupt())?,
            row.try_get::<String, _>("task_id")?
                .parse()
                .map_err(|_| corrupt())?,
            row.try_get::<String, _>("target_id")?
                .parse()
                .map_err(|_| corrupt())?,
            &basename,
        )
        .map_err(|_| corrupt())?;
        (
            declared.max_bytes,
            Sha256Digest::parse(row.try_get::<String, _>("expected_sha256")?)
                .map_err(|_| corrupt())?,
            declared.name.clone(),
            final_path,
        )
    };
    if final_path != row.try_get::<String, _>("final_relative_path")? {
        return Err(corrupt());
    }
    let mut record = UploadRecord::new(
        upload_id,
        name,
        final_path,
        size,
        sha256,
        &declaration_name,
        declared.max_bytes,
    )
    .map_err(|_| corrupt())?;
    record
        .restore_progress(
            row.try_get::<i64, _>("received_bytes")?
                .try_into()
                .map_err(|_| corrupt())?,
            parse_state(row.try_get("state")?)?,
        )
        .map_err(|_| corrupt())?;
    if record.staging_relative_path().to_string_lossy()
        != row.try_get::<String, _>("staging_path")?
    {
        return Err(corrupt());
    }
    Ok(record)
}

fn declaration<'a>(
    spec: &'a CompiledTaskSpec,
    name: &str,
) -> Result<(&'a ArtifactDeclaration, String), StoreError> {
    for declaration in &spec.artifacts {
        if declaration.max_files.is_none() && name == declaration.name {
            let basename = Path::new(&declaration.path)
                .file_name()
                .and_then(|value| value.to_str())
                .ok_or_else(corrupt)?;
            return Ok((declaration, basename.to_owned()));
        }
        if declaration.max_files.is_some()
            && let Some(basename) = name.strip_prefix(&format!("{}/", declaration.name))
            && !basename.is_empty()
            && !basename.starts_with('.')
            && !basename.contains(['/', '\\', '\0'])
        {
            return Ok((declaration, basename.to_owned()));
        }
    }
    Err(corrupt())
}

fn parse_state(value: &str) -> Result<UploadState, StoreError> {
    serde_json::from_str(&format!("\"{value}\"")).map_err(|_| corrupt())
}

async fn ensure_no_target(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    row: &SqliteRow,
) -> Result<(), StoreError> {
    let exists: i64 = sqlx::query_scalar("SELECT count(*) FROM artifacts WHERE id=?")
        .bind(row.try_get::<String, _>("target_id")?)
        .fetch_one(&mut **transaction)
        .await?;
    if exists == 0 { Ok(()) } else { Err(corrupt()) }
}

async fn apply_action(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    artifacts: &NasArtifactStore,
    upload_id: &str,
    record: &UploadRecord,
    now_ms: i64,
) -> Result<(), StoreError> {
    let action = artifacts
        .recovery_action(record, true)
        .map_err(|error| stored_artifact_error(error.code()))?;
    match action {
        RecoveryAction::ResumeReceiving | RecoveryAction::RetryCommit => Ok(()),
        RecoveryAction::MoveVerified | RecoveryAction::MarkMoved => {
            if action == RecoveryAction::MoveVerified {
                artifacts
                    .move_verified(record)
                    .map_err(|error| stored_artifact_error(error.code()))?;
            }
            let updated = sqlx::query(
                "UPDATE uploads SET state='moved',updated_at_ms=? WHERE id=? AND state='verified'",
            )
            .bind(now_ms)
            .bind(upload_id)
            .execute(&mut **transaction)
            .await?;
            if updated.rows_affected() == 1 {
                Ok(())
            } else {
                Err(StoreError::new(ErrorCode::Conflict))
            }
        }
        RecoveryAction::DeleteFilesThenLedger | RecoveryAction::DeleteLedger => Err(corrupt()),
    }
}

fn stored_artifact_error(code: ErrorCode) -> StoreError {
    StoreError::new(if code == ErrorCode::StorageUnavailable {
        code
    } else {
        ErrorCode::CorruptState
    })
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
