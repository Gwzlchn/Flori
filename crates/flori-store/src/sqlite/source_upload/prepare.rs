use flori_core::{ErrorCode, PendingSourceCommit, SourceId, SourceInputId, SourceKind, UploadId};
use sqlx::Row;

use crate::artifact::{NasArtifactStore, UploadRecord, source_input_path};

use super::{PreparedSourceUpload, StartSourceUpload};
use crate::sqlite::{Store, StoreError, scheduler::source_kind};

impl Store {
    pub async fn start_source_upload(
        &self,
        artifacts: &NasArtifactStore,
        input: StartSourceUpload<'_>,
    ) -> Result<PreparedSourceUpload, StoreError> {
        validate(&input)?;
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        if let Some(row) = sqlx::query("SELECT id,request_sha256 FROM sources WHERE request_key=?")
            .bind(&input.request.request_key)
            .fetch_optional(&mut *transaction)
            .await?
        {
            let source_id = row
                .try_get::<String, _>("id")?
                .parse()
                .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
            let matches =
                row.try_get::<String, _>("request_sha256")? == input.request_sha256.as_str();
            transaction.rollback().await?;
            return if matches {
                Ok(PreparedSourceUpload {
                    source_id,
                    upload_id: None,
                    received_bytes: input.size_bytes,
                })
            } else {
                Err(StoreError::new(ErrorCode::IdempotencyConflict))
            };
        }
        if let Some(row) = sqlx::query(
            "SELECT id,owner_id,request_sha256,expected_size_bytes,received_bytes \
             FROM uploads WHERE owner_kind='source' AND request_key=?",
        )
        .bind(&input.request.request_key)
        .fetch_optional(&mut *transaction)
        .await?
        {
            let matches = row.try_get::<String, _>("request_sha256")?
                == input.request_sha256.as_str()
                && to_u64(row.try_get("expected_size_bytes")?)? == input.size_bytes;
            let source_id = row
                .try_get::<String, _>("owner_id")?
                .parse()
                .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
            let upload_id = row
                .try_get::<String, _>("id")?
                .parse()
                .map_err(|_| StoreError::new(ErrorCode::CorruptState))?;
            let received_bytes = to_u64(row.try_get("received_bytes")?)?;
            transaction.rollback().await?;
            return if matches {
                Ok(PreparedSourceUpload {
                    source_id,
                    upload_id: Some(upload_id),
                    received_bytes,
                })
            } else {
                Err(StoreError::new(ErrorCode::IdempotencyConflict))
            };
        }
        validate_scope(&mut transaction, &input).await?;
        let source_id = SourceId::generate();
        let source_input_id = SourceInputId::generate();
        let upload_id = UploadId::generate();
        let file_name = match input.request.kind {
            SourceKind::PdfUpload => "source.pdf",
            SourceKind::LocalVideo => "source.mp4",
            _ => return Err(StoreError::new(ErrorCode::UnsupportedSource)),
        };
        let final_path = source_input_path(source_id, source_input_id, file_name)
            .map_err(|error| StoreError::new(error.code()))?;
        let pending = PendingSourceCommit {
            source_id,
            source_input_id,
            kind: input.request.kind,
            canonical_ref: format!("sha256:{}", input.request.file_sha256.as_str()),
            title: input.request.title.clone(),
            domain_id: input.request.domain_id,
            collection_ids: input.request.collection_ids.clone(),
            file_name: file_name.into(),
            media_type: input.media_type.into(),
            size_bytes: input.size_bytes,
            sha256: input.request.file_sha256.clone(),
            final_relative_path: final_path.clone(),
            created_at_ms: input.created_at_ms,
        };
        let record = UploadRecord::new(
            upload_id,
            "original",
            &final_path,
            input.size_bytes,
            input.request.file_sha256.clone(),
            "original",
            input.size_bytes,
        )
        .map_err(|error| StoreError::new(error.code()))?;
        artifacts
            .validate_upload(&record)
            .map_err(|error| StoreError::new(error.code()))?;
        sqlx::query(
            "INSERT INTO uploads(id,owner_kind,owner_id,request_key,request_sha256,commit_json, \
             name,target_id,staging_path,final_relative_path,expected_size_bytes,expected_sha256, \
             received_bytes,state,created_at_ms,updated_at_ms) \
             VALUES(?,'source',?,?,?,?,'original',?,?,?,?,?,0,'receiving',?,?)",
        )
        .bind(upload_id.to_string())
        .bind(source_id.to_string())
        .bind(&input.request.request_key)
        .bind(input.request_sha256.as_str())
        .bind(serde_json::to_string(&pending).map_err(|_| StoreError::new(ErrorCode::Internal))?)
        .bind(source_input_id.to_string())
        .bind(record.staging_relative_path().to_string_lossy().as_ref())
        .bind(final_path)
        .bind(
            i64::try_from(input.size_bytes)
                .map_err(|_| StoreError::new(ErrorCode::ArtifactTooLarge))?,
        )
        .bind(input.request.file_sha256.as_str())
        .bind(input.created_at_ms)
        .bind(input.created_at_ms)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(PreparedSourceUpload {
            source_id,
            upload_id: Some(upload_id),
            received_bytes: 0,
        })
    }
}

fn validate(input: &StartSourceUpload<'_>) -> Result<(), StoreError> {
    let expected_media = match input.request.kind {
        SourceKind::PdfUpload => "application/pdf",
        SourceKind::LocalVideo => "video/mp4",
        _ => return Err(StoreError::new(ErrorCode::UnsupportedSource)),
    };
    let sorted = input
        .request
        .collection_ids
        .windows(2)
        .all(|ids| ids[0] < ids[1]);
    if input.request.request_key.is_empty()
        || input.created_at_ms < 0
        || input.size_bytes == 0
        || input.media_type != expected_media
        || !sorted
    {
        return Err(StoreError::new(ErrorCode::InvalidRequest));
    }
    Ok(())
}

async fn validate_scope(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    input: &StartSourceUpload<'_>,
) -> Result<(), StoreError> {
    let domain = sqlx::query_scalar::<_, i64>("SELECT count(*) FROM domains WHERE id=?")
        .bind(input.request.domain_id.to_string())
        .fetch_one(&mut **transaction)
        .await?;
    if domain != 1 {
        return Err(StoreError::new(ErrorCode::NotFound));
    }
    for collection_id in &input.request.collection_ids {
        let count = sqlx::query_scalar::<_, i64>(
            "SELECT count(*) FROM collections WHERE id=? AND domain_id=?",
        )
        .bind(collection_id.to_string())
        .bind(input.request.domain_id.to_string())
        .fetch_one(&mut **transaction)
        .await?;
        if count != 1 {
            return Err(StoreError::new(ErrorCode::NotFound));
        }
    }
    let duplicate = sqlx::query_scalar::<_, i64>(
        "SELECT count(*) FROM sources WHERE kind=? AND canonical_ref=?",
    )
    .bind(source_kind(input.request.kind))
    .bind(format!("sha256:{}", input.request.file_sha256.as_str()))
    .fetch_one(&mut **transaction)
    .await?;
    if duplicate != 0 {
        return Err(StoreError::new(ErrorCode::Conflict));
    }
    Ok(())
}

fn to_u64(value: i64) -> Result<u64, StoreError> {
    u64::try_from(value).map_err(|_| StoreError::new(ErrorCode::CorruptState))
}
