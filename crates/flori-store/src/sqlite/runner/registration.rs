use std::str::FromStr;

use flori_core::{ErrorCode, RegisterRunnerRequest, RunnerId, Sha256Digest};
use sqlx::Row;

use super::{
    super::{Store, StoreError},
    normalize::{capabilities, identifier, tags_json},
};

impl Store {
    #[allow(clippy::too_many_arguments)]
    pub async fn create_runner_slot(
        &self,
        name: &str,
        tags: &[String],
        max_concurrency: u16,
        default_model: Option<&str>,
        default_effort: Option<&str>,
        registration_token_digest: &Sha256Digest,
        registration_expires_at_ms: i64,
        now_ms: i64,
    ) -> Result<RunnerId, StoreError> {
        if !identifier(name)
            || max_concurrency == 0
            || now_ms < 0
            || registration_expires_at_ms <= now_ms
            || !valid_default(default_model, default_effort)
        {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let tags_json = tags_json(tags)?;
        let runner_id = RunnerId::generate();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let duplicate: i64 = sqlx::query_scalar(
            "SELECT count(*) FROM runners WHERE name=? OR registration_token_digest=?",
        )
        .bind(name)
        .bind(registration_token_digest.as_str())
        .fetch_one(&mut *transaction)
        .await?;
        if duplicate != 0 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::Conflict));
        }
        sqlx::query(
            "INSERT INTO runners(id,name,state,registration_token_digest, \
             registration_expires_at_ms,config_revision,max_concurrency,tags_json,tools_json, \
             ai_models_json,default_model,default_effort,created_at_ms,updated_at_ms) \
             VALUES(?,?,'disabled',?,?,0,?,?,'[]','[]',?,?,?,?)",
        )
        .bind(runner_id.to_string())
        .bind(name)
        .bind(registration_token_digest.as_str())
        .bind(registration_expires_at_ms)
        .bind(i64::from(max_concurrency))
        .bind(tags_json)
        .bind(default_model)
        .bind(default_effort)
        .bind(now_ms)
        .bind(now_ms)
        .execute(&mut *transaction)
        .await?;
        transaction.commit().await?;
        Ok(runner_id)
    }

    pub async fn register_runner(
        &self,
        registration_token_digest: &Sha256Digest,
        long_token_digest: &Sha256Digest,
        request: &RegisterRunnerRequest,
        now_ms: i64,
    ) -> Result<RunnerId, StoreError> {
        if now_ms < 0 || registration_token_digest == long_token_digest {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let normalized = capabilities(request)?;
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let row = sqlx::query(
            "SELECT id,state,registration_expires_at_ms,default_model,default_effort \
             FROM runners WHERE registration_token_digest=?",
        )
        .bind(registration_token_digest.as_str())
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::CredentialUnavailable));
        };
        if row.try_get::<String, _>("state")? != "disabled"
            || row
                .try_get::<Option<i64>, _>("registration_expires_at_ms")?
                .is_none_or(|expires| expires <= now_ms)
        {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::CredentialUnavailable));
        }
        let default_model: Option<String> = row.try_get("default_model")?;
        let default_effort: Option<String> = row.try_get("default_effort")?;
        if let (Some(model), Some(effort)) = (&default_model, &default_effort)
            && !normalized
                .ai_models
                .iter()
                .any(|entry| entry.model == *model && entry.efforts.contains(effort))
        {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::CapabilityMismatch));
        }
        let runner_id: String = row.try_get("id")?;
        let updated = sqlx::query(
            "UPDATE runners SET state='enabled',token_digest=?,registration_token_digest=NULL, \
             registration_expires_at_ms=NULL,config_revision=config_revision+1,tools_json=?, \
             ai_models_json=?,last_seen_at_ms=?,updated_at_ms=? \
             WHERE id=? AND state='disabled' AND registration_token_digest=?",
        )
        .bind(long_token_digest.as_str())
        .bind(normalized.tools_json)
        .bind(normalized.ai_models_json)
        .bind(now_ms)
        .bind(now_ms)
        .bind(&runner_id)
        .bind(registration_token_digest.as_str())
        .execute(&mut *transaction)
        .await
        .map_err(|error| match &error {
            sqlx::Error::Database(database) if database.is_unique_violation() => {
                StoreError::new(ErrorCode::Conflict)
            }
            _ => error.into(),
        })?;
        if updated.rows_affected() != 1 {
            transaction.rollback().await?;
            return Err(StoreError::new(ErrorCode::CredentialUnavailable));
        }
        transaction.commit().await?;
        RunnerId::from_str(&runner_id).map_err(|_| StoreError::new(ErrorCode::CorruptState))
    }

    pub async fn authenticate_runner(
        &self,
        long_token_digest: &Sha256Digest,
        now_ms: i64,
    ) -> Result<RunnerId, StoreError> {
        if now_ms < 0 {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let row = sqlx::query(
            "UPDATE runners SET last_seen_at_ms=?,updated_at_ms=? \
             WHERE token_digest=? AND state='enabled' RETURNING id",
        )
        .bind(now_ms)
        .bind(now_ms)
        .bind(long_token_digest.as_str())
        .fetch_optional(&self.pool)
        .await?
        .ok_or_else(|| StoreError::new(ErrorCode::CredentialUnavailable))?;
        RunnerId::from_str(row.try_get("id")?).map_err(|_| StoreError::new(ErrorCode::CorruptState))
    }
}

fn valid_default(model: Option<&str>, effort: Option<&str>) -> bool {
    match (model, effort) {
        (None, None) => true,
        (Some(model), Some(effort)) => identifier(model) && identifier(effort),
        _ => false,
    }
}
