use std::{fmt::Write, str::FromStr};

use super::super::{Store, StoreError};
use flori_core::{
    CONTRACT_REVISION, ErrorCode, PIPELINE_COMPILER_VERSION, PipelineId, PipelineRevisionId,
};
use flori_pipeline::Compilation;
use sha2::{Digest, Sha256};
use sqlx::Row;

impl Store {
    pub async fn register_pipeline_revision(
        &self,
        pipeline_id: PipelineId,
        revision_id: PipelineRevisionId,
        compilation: &Compilation,
        git_commit: &str,
        yaml_text: &str,
        now_ms: i64,
    ) -> Result<PipelineRevisionId, StoreError> {
        if git_commit.is_empty()
            || yaml_text.is_empty()
            || now_ms < 0
            || !valid_compilation(compilation)
        {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let pipeline_id = pipeline_id.to_string();
        let revision_id = revision_id.to_string();
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let pipeline = sqlx::query("SELECT id FROM pipelines WHERE key=?")
            .bind(&compilation.pipeline.pipeline_key)
            .fetch_optional(&mut *transaction)
            .await?;
        if let Some(row) = pipeline {
            if row.try_get::<String, _>("id")? != pipeline_id {
                transaction.rollback().await?;
                return Err(StoreError::new(ErrorCode::Conflict));
            }
        } else {
            sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,?,?)")
                .bind(&pipeline_id)
                .bind(&compilation.pipeline.pipeline_key)
                .bind(now_ms)
                .execute(&mut *transaction)
                .await?;
        }

        if let Some(row) =
            sqlx::query("SELECT id FROM pipeline_revisions WHERE pipeline_id=? AND yaml_sha256=?")
                .bind(&pipeline_id)
                .bind(&compilation.sha256)
                .fetch_optional(&mut *transaction)
                .await?
        {
            let existing: String = row.try_get("id")?;
            sqlx::query("UPDATE pipelines SET current_revision_id=? WHERE id=?")
                .bind(&existing)
                .bind(&pipeline_id)
                .execute(&mut *transaction)
                .await?;
            transaction.commit().await?;
            return PipelineRevisionId::from_str(&existing)
                .map_err(|_| StoreError::new(ErrorCode::CorruptState));
        }

        sqlx::query(
            "INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit, \
             yaml_sha256,yaml_text,created_at_ms) VALUES(?,?,?,?,?,?,?)",
        )
        .bind(&revision_id)
        .bind(&pipeline_id)
        .bind(i64::from(PIPELINE_COMPILER_VERSION))
        .bind(git_commit)
        .bind(&compilation.sha256)
        .bind(yaml_text)
        .bind(now_ms)
        .execute(&mut *transaction)
        .await?;
        sqlx::query("UPDATE pipelines SET current_revision_id=? WHERE id=?")
            .bind(&revision_id)
            .bind(&pipeline_id)
            .execute(&mut *transaction)
            .await?;
        transaction.commit().await?;
        Ok(revision_id
            .parse()
            .expect("revision ID was produced by its strong type"))
    }
}

pub(super) fn valid_compilation(compilation: &Compilation) -> bool {
    if compilation.pipeline.contract_revision != CONTRACT_REVISION
        || compilation.pipeline.compiler_version != PIPELINE_COMPILER_VERSION
    {
        return false;
    }
    let mut digest = String::with_capacity(64);
    for byte in Sha256::digest(compilation.canonical_json.as_bytes()) {
        write!(&mut digest, "{byte:02x}").expect("writing to String cannot fail");
    }
    digest == compilation.sha256
}
