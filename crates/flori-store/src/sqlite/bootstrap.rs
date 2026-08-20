use std::{fmt::Write as _, str::FromStr};

use flori_core::{DomainId, ErrorCode, PdfSetupView, PipelineId, PipelineRevisionId, Sha256Digest};
use flori_pipeline::compile;
use sha2::{Digest, Sha256};
use sqlx::Row;

use super::{Store, StoreError};

const DOMAIN_SLUG: &str = "general";
const PIPELINE_KEY: &str = "pdf";

impl Store {
    pub async fn bootstrap_pdf(
        &self,
        pipeline_yaml: &str,
        note_prompt: &str,
        translate_prompt: &str,
        revision_label: &str,
        now_ms: i64,
    ) -> Result<PdfSetupView, StoreError> {
        if note_prompt.is_empty()
            || translate_prompt.is_empty()
            || revision_label.is_empty()
            || now_ms < 0
        {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let compilation = compile(PIPELINE_KEY, pipeline_yaml.as_bytes())
            .map_err(|_| StoreError::new(ErrorCode::PipelineInvalid))?;
        let mut transaction = self.pool.begin_with("BEGIN IMMEDIATE").await?;
        let domain_id = match sqlx::query("SELECT id FROM domains WHERE slug=?")
            .bind(DOMAIN_SLUG)
            .fetch_optional(&mut *transaction)
            .await?
        {
            Some(row) => parse_id(row.try_get("id")?)?,
            None => {
                let id = DomainId::generate();
                sqlx::query(
                    "INSERT INTO domains(id,slug,name,description,profile_text,created_at_ms,updated_at_ms) \
                     VALUES(?,?,'General',NULL,'General technical knowledge.',?,?)",
                )
                .bind(id.to_string())
                .bind(DOMAIN_SLUG)
                .bind(now_ms)
                .bind(now_ms)
                .execute(&mut *transaction)
                .await?;
                id
            }
        };
        for (key, content) in [
            ("document_note", note_prompt),
            ("document_translate", translate_prompt),
        ] {
            sqlx::query(
                "INSERT INTO prompts(key,content,sha256,updated_at_ms) VALUES(?,?,?,?) \
                 ON CONFLICT(key) DO NOTHING",
            )
            .bind(key)
            .bind(content)
            .bind(digest(content).as_str())
            .bind(now_ms)
            .execute(&mut *transaction)
            .await?;
        }
        let pipeline_id = match sqlx::query("SELECT id FROM pipelines WHERE key=?")
            .bind(PIPELINE_KEY)
            .fetch_optional(&mut *transaction)
            .await?
        {
            Some(row) => parse_id(row.try_get("id")?)?,
            None => PipelineId::generate(),
        };
        transaction.commit().await?;
        self.register_pipeline_revision(
            pipeline_id,
            PipelineRevisionId::generate(),
            &compilation,
            revision_label,
            pipeline_yaml,
            now_ms,
        )
        .await?;
        Ok(PdfSetupView {
            domain_id,
            pipeline_id,
        })
    }

    pub async fn pdf_setup(&self) -> Result<Option<PdfSetupView>, StoreError> {
        let row = sqlx::query(
            "SELECT d.id AS domain_id,p.id AS pipeline_id FROM domains d,pipelines p \
             WHERE d.slug=? AND p.key=? AND p.current_revision_id IS NOT NULL",
        )
        .bind(DOMAIN_SLUG)
        .bind(PIPELINE_KEY)
        .fetch_optional(&self.pool)
        .await?;
        row.map(|row| {
            Ok(PdfSetupView {
                domain_id: parse_id(row.try_get("domain_id")?)?,
                pipeline_id: parse_id(row.try_get("pipeline_id")?)?,
            })
        })
        .transpose()
    }
}

fn parse_id<T: FromStr>(value: String) -> Result<T, StoreError> {
    value
        .parse()
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn digest(value: &str) -> Sha256Digest {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(value.as_bytes()) {
        write!(&mut output, "{byte:02x}").expect("writing to String cannot fail");
    }
    Sha256Digest::parse(output).expect("SHA-256 formatter is canonical")
}
