use std::str::FromStr;

use super::{
    super::{Store, StoreError},
    wire::source_kind,
};
use flori_core::{CollectionId, DomainId, ErrorCode, Sha256Digest, SourceId, SourceKind};
use sqlx::{Row, Sqlite, Transaction};

#[derive(Clone, Copy, Debug)]
pub struct CreateSource<'a> {
    pub kind: SourceKind,
    pub canonical_ref: &'a str,
    pub title: Option<&'a str>,
    pub domain_id: DomainId,
    pub collection_ids: &'a [CollectionId],
    pub request_key: &'a str,
    pub request_sha256: &'a str,
    pub created_at_ms: i64,
}

impl Store {
    pub async fn create_source(&self, input: CreateSource<'_>) -> Result<SourceId, StoreError> {
        if input.canonical_ref.is_empty()
            || input.request_key.is_empty()
            || input.created_at_ms < 0
            || Sha256Digest::parse(input.request_sha256).is_err()
            || !input
                .collection_ids
                .windows(2)
                .all(|pair| pair[0] < pair[1])
        {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        if let Some(row) = sqlx::query("SELECT id,request_sha256 FROM sources WHERE request_key=?")
            .bind(input.request_key)
            .fetch_optional(&mut *transaction)
            .await?
        {
            let matches = row.try_get::<String, _>("request_sha256")? == input.request_sha256;
            let id: String = row.try_get("id")?;
            transaction.rollback().await?;
            return if matches {
                SourceId::from_str(&id).map_err(|_| StoreError::new(ErrorCode::CorruptState))
            } else {
                Err(StoreError::new(ErrorCode::IdempotencyConflict))
            };
        }
        validate_collections(&mut transaction, input.domain_id, input.collection_ids).await?;
        let source_id = SourceId::generate();
        sqlx::query(
            "INSERT INTO sources(id,kind,canonical_ref,title,domain_id,request_key, \
             request_sha256,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?)",
        )
        .bind(source_id.to_string())
        .bind(source_kind(input.kind))
        .bind(input.canonical_ref)
        .bind(input.title)
        .bind(input.domain_id.to_string())
        .bind(input.request_key)
        .bind(input.request_sha256)
        .bind(input.created_at_ms)
        .bind(input.created_at_ms)
        .execute(&mut *transaction)
        .await
        .map_err(|error| match &error {
            sqlx::Error::Database(database) if database.is_unique_violation() => {
                StoreError::new(ErrorCode::Conflict)
            }
            _ => error.into(),
        })?;
        for collection_id in input.collection_ids {
            sqlx::query(
                "INSERT INTO collection_sources(collection_id,source_id,added_at_ms) VALUES(?,?,?)",
            )
            .bind(collection_id.to_string())
            .bind(source_id.to_string())
            .bind(input.created_at_ms)
            .execute(&mut *transaction)
            .await?;
        }
        transaction.commit().await?;
        Ok(source_id)
    }
}

async fn validate_collections(
    transaction: &mut Transaction<'_, Sqlite>,
    domain_id: DomainId,
    collection_ids: &[CollectionId],
) -> Result<(), StoreError> {
    let domain_id = domain_id.to_string();
    for collection_id in collection_ids {
        let collection_domain: Option<String> =
            sqlx::query_scalar("SELECT domain_id FROM collections WHERE id=?")
                .bind(collection_id.to_string())
                .fetch_optional(&mut **transaction)
                .await?;
        if collection_domain.as_deref() != Some(domain_id.as_str()) {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
    }
    Ok(())
}
