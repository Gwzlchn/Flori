use flori_core::{ErrorCode, PendingMaterializeCommit, Sha256Digest};
use sqlx::Row;

use crate::artifact::{NasArtifactStore, RecoveryAction, UploadRecord};

use crate::sqlite::{Store, StoreError, scheduler};

pub(super) async fn reconcile(
    store: &Store,
    artifacts: &NasArtifactStore,
    owner_id: &str,
    now_ms: i64,
) -> Result<(), StoreError> {
    let mut transaction = store.pool.begin_with("BEGIN IMMEDIATE").await?;
    let rows = sqlx::query(
        "SELECT id,owner_id,request_key,request_sha256,commit_json,target_id \
         FROM uploads WHERE owner_kind='materialize' AND owner_id=? ORDER BY id",
    )
    .bind(owner_id)
    .fetch_all(&mut *transaction)
    .await?;
    if rows.is_empty() {
        transaction.rollback().await?;
        return Ok(());
    }
    let commit_json: String = rows[0].try_get("commit_json")?;
    let pending: PendingMaterializeCommit =
        serde_json::from_str(&commit_json).map_err(|_| corrupt())?;
    if serde_json::to_string(&pending).map_err(|_| corrupt())? != commit_json
        || pending.job_id.to_string() != owner_id
        || pending.artifacts.is_empty()
        || pending.artifacts.len() != rows.len()
    {
        return Err(corrupt());
    }
    let leader_id = pending
        .artifacts
        .first()
        .ok_or_else(corrupt)?
        .upload_id
        .to_string();
    let request_key = request_key(&rows, &leader_id)?;
    let request_sha256 = request_sha256(&rows)?;
    if request_key.is_empty() || Sha256Digest::parse(&request_sha256).is_err() {
        return Err(corrupt());
    }
    ensure_target_job_absent(&mut transaction, owner_id).await?;

    let owner_valid = match scheduler::validate_owner(&mut transaction, &pending).await {
        Ok(()) => true,
        Err(error) if error.code() == ErrorCode::RerunBoundaryInvalid => false,
        Err(error) => return Err(error),
    };
    let mut records = Vec::with_capacity(rows.len());
    for artifact in &pending.artifacts {
        let target_exists: i64 = sqlx::query_scalar("SELECT count(*) FROM artifacts WHERE id=?")
            .bind(artifact.artifact_id.to_string())
            .fetch_one(&mut *transaction)
            .await?;
        if target_exists != 0 {
            return Err(corrupt());
        }
        let (target, source) = scheduler::load_records(
            &mut transaction,
            &pending,
            artifact,
            &request_key,
            &request_sha256,
        )
        .await?;
        if owner_valid {
            if artifacts
                .recovery_action(&source, true)
                .map_err(|error| stored_artifact_error(error.code()))?
                != RecoveryAction::RetryCommit
            {
                return Err(corrupt());
            }
        }
        records.push((artifact.upload_id.to_string(), target));
    }

    if !owner_valid {
        for (_, record) in &records {
            artifacts
                .discard(record)
                .map_err(|error| stored_artifact_error(error.code()))?;
        }
        let deleted =
            sqlx::query("DELETE FROM uploads WHERE owner_kind='materialize' AND owner_id=?")
                .bind(owner_id)
                .execute(&mut *transaction)
                .await?;
        if deleted.rows_affected() != rows.len() as u64 {
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        transaction.commit().await?;
        return Ok(());
    }

    for (upload_id, record) in records {
        apply_action(&mut transaction, artifacts, &upload_id, &record, now_ms).await?;
    }
    transaction.commit().await?;
    Ok(())
}

fn request_key(rows: &[sqlx::sqlite::SqliteRow], leader_id: &str) -> Result<String, StoreError> {
    let mut found = None;
    for row in rows {
        let value: Option<String> = row.try_get("request_key")?;
        if let Some(value) = value
            && (row.try_get::<String, _>("id")? != leader_id || found.replace(value).is_some())
        {
            return Err(corrupt());
        }
    }
    found.ok_or_else(corrupt)
}

fn request_sha256(rows: &[sqlx::sqlite::SqliteRow]) -> Result<String, StoreError> {
    let expected = rows[0]
        .try_get::<Option<String>, _>("request_sha256")?
        .ok_or_else(corrupt)?;
    for row in rows {
        if row
            .try_get::<Option<String>, _>("request_sha256")?
            .as_deref()
            != Some(&expected)
        {
            return Err(corrupt());
        }
    }
    Ok(expected)
}

async fn ensure_target_job_absent(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    owner_id: &str,
) -> Result<(), StoreError> {
    let exists: i64 = sqlx::query_scalar("SELECT count(*) FROM jobs WHERE id=?")
        .bind(owner_id)
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
