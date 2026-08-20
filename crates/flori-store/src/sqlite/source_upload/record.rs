use flori_core::{ErrorCode, PendingSourceCommit, UploadId, UploadState};
use sqlx::{Row, Sqlite, Transaction};

use crate::artifact::{UploadRecord, source_input_path};
use crate::sqlite::StoreError;

pub(in crate::sqlite) struct ActiveSourceUpload {
    pub record: UploadRecord,
    pub pending: PendingSourceCommit,
    pub request_key: String,
    pub request_sha256: String,
}

pub(in crate::sqlite) async fn load(
    transaction: &mut Transaction<'_, Sqlite>,
    upload_id: UploadId,
) -> Result<ActiveSourceUpload, StoreError> {
    let row = sqlx::query(
        "SELECT owner_kind,owner_id,request_key,request_sha256,commit_json,name,target_id, \
         staging_path,final_relative_path,expected_size_bytes,expected_sha256,received_bytes,state \
         FROM uploads WHERE id=?",
    )
    .bind(upload_id.to_string())
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(|| StoreError::new(ErrorCode::NotFound))?;
    let pending: PendingSourceCommit =
        serde_json::from_str(row.try_get("commit_json")?).map_err(|_| corrupt())?;
    let expected_path = source_input_path(
        pending.source_id,
        pending.source_input_id,
        &pending.file_name,
    )
    .map_err(|error| StoreError::new(error.code()))?;
    let size = to_u64(row.try_get("expected_size_bytes")?)?;
    let received = to_u64(row.try_get("received_bytes")?)?;
    let state = parse_state(row.try_get("state")?)?;
    if row.try_get::<String, _>("owner_kind")? != "source"
        || row.try_get::<String, _>("owner_id")? != pending.source_id.to_string()
        || row.try_get::<String, _>("target_id")? != pending.source_input_id.to_string()
        || row.try_get::<String, _>("name")? != "original"
        || row.try_get::<String, _>("staging_path")? != format!(".staging/uploads/{upload_id}")
        || row.try_get::<String, _>("final_relative_path")? != expected_path
        || pending.final_relative_path != expected_path
        || size != pending.size_bytes
        || row.try_get::<String, _>("expected_sha256")? != pending.sha256.as_str()
    {
        return Err(corrupt());
    }
    let mut record = UploadRecord::new(
        upload_id,
        "original",
        expected_path,
        size,
        pending.sha256.clone(),
        "original",
        size,
    )
    .map_err(|_| corrupt())?;
    record
        .restore_progress(received, state)
        .map_err(|_| corrupt())?;
    Ok(ActiveSourceUpload {
        record,
        pending,
        request_key: row.try_get("request_key")?,
        request_sha256: row.try_get("request_sha256")?,
    })
}

fn to_u64(value: i64) -> Result<u64, StoreError> {
    u64::try_from(value).map_err(|_| corrupt())
}

fn parse_state(value: &str) -> Result<UploadState, StoreError> {
    match value {
        "receiving" => Ok(UploadState::Receiving),
        "verified" => Ok(UploadState::Verified),
        "moved" => Ok(UploadState::Moved),
        _ => Err(corrupt()),
    }
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
