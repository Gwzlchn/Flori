use std::str::FromStr;

use flori_core::{
    ErrorCode, EvidenceId, EvidenceLocator, EvidenceView, PdfRect, SearchHit, VideoKeyframe,
};
use sqlx::{Row, sqlite::SqliteRow};

use super::super::{Store, StoreError};

impl Store {
    pub async fn search_current(
        &self,
        query: &str,
        limit: u32,
    ) -> Result<Vec<SearchHit>, StoreError> {
        let query = query.split_whitespace().collect::<Vec<_>>().join(" ");
        let character_count = query.chars().count();
        if character_count == 0 || character_count > 200 || !(1..=100).contains(&limit) {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
        let rows = if character_count < 3 {
            let pattern = format!("%{}%", escape_like(&query));
            sqlx::query(
                "SELECT sc.chunk_id,sc.source_id,sc.job_id,sc.artifact_id,sc.title,sc.body, \
                 (SELECT group_concat(ordered.evidence_id,',') FROM \
                   (SELECT evidence_id FROM search_chunk_evidence \
                    WHERE chunk_id=sc.chunk_id ORDER BY evidence_id) ordered) AS evidence_ids \
                 FROM search_chunks sc JOIN sources s \
                   ON s.id=sc.source_id AND s.current_job_id=sc.job_id \
                 WHERE (sc.title LIKE ? ESCAPE '\\' OR sc.body LIKE ? ESCAPE '\\') \
                 ORDER BY sc.chunk_id LIMIT ?",
            )
            .bind(&pattern)
            .bind(&pattern)
            .bind(i64::from(limit))
            .fetch_all(&self.pool)
            .await?
        } else {
            let phrase = format!("\"{}\"", query.replace('"', "\"\""));
            sqlx::query(
                "SELECT sc.chunk_id,sc.source_id,sc.job_id,sc.artifact_id,sc.title,sc.body, \
                 (SELECT group_concat(ordered.evidence_id,',') FROM \
                   (SELECT evidence_id FROM search_chunk_evidence \
                    WHERE chunk_id=sc.chunk_id ORDER BY evidence_id) ordered) AS evidence_ids \
                 FROM search_chunks sc JOIN sources s \
                   ON s.id=sc.source_id AND s.current_job_id=sc.job_id \
                 WHERE search_chunks MATCH ? \
                 ORDER BY bm25(search_chunks),sc.chunk_id LIMIT ?",
            )
            .bind(phrase)
            .bind(i64::from(limit))
            .fetch_all(&self.pool)
            .await?
        };
        rows.iter().map(parse_search_hit).collect()
    }

    pub async fn get_current_evidence(
        &self,
        evidence_id: EvidenceId,
    ) -> Result<Option<EvidenceView>, StoreError> {
        let row = sqlx::query(
            "SELECT e.id,e.source_id,e.job_id,e.artifact_id,e.locator_kind,e.page, \
             e.x1,e.y1,e.x2,e.y2,e.start_ms,e.end_ms,e.keyframe_artifact_id,e.quote, \
             ka.name AS keyframe_name FROM evidence e \
             JOIN sources s ON s.id=e.source_id AND s.current_job_id=e.job_id \
             LEFT JOIN artifacts ka ON ka.id=e.keyframe_artifact_id WHERE e.id=?",
        )
        .bind(evidence_id.to_string())
        .fetch_optional(&self.pool)
        .await?;
        row.as_ref().map(parse_evidence_view).transpose()
    }
}

fn escape_like(query: &str) -> String {
    let mut escaped = String::with_capacity(query.len());
    for character in query.chars() {
        if matches!(character, '%' | '_' | '\\') {
            escaped.push('\\');
        }
        escaped.push(character);
    }
    escaped
}

fn parse_search_hit(row: &SqliteRow) -> Result<SearchHit, StoreError> {
    let evidence_ids: Option<String> = row.try_get("evidence_ids")?;
    Ok(SearchHit {
        chunk_id: parse_id(row, "chunk_id")?,
        source_id: parse_id(row, "source_id")?,
        job_id: parse_id(row, "job_id")?,
        artifact_id: parse_id(row, "artifact_id")?,
        title: row.try_get("title")?,
        body: row.try_get("body")?,
        evidence_ids: evidence_ids
            .as_deref()
            .map(|ids| {
                ids.split(',')
                    .map(|id| {
                        EvidenceId::from_str(id)
                            .map_err(|_| StoreError::new(ErrorCode::CorruptState))
                    })
                    .collect()
            })
            .transpose()?
            .unwrap_or_default(),
    })
}

fn parse_evidence_view(row: &SqliteRow) -> Result<EvidenceView, StoreError> {
    let locator_kind: String = row.try_get("locator_kind")?;
    let locator = match locator_kind.as_str() {
        "pdf" => EvidenceLocator::Pdf {
            page: required_u32(row, "page")?,
            bbox: PdfRect {
                x1: row.try_get("x1")?,
                y1: row.try_get("y1")?,
                x2: row.try_get("x2")?,
                y2: row.try_get("y2")?,
            },
        },
        "video" => {
            let keyframe_id: Option<String> = row.try_get("keyframe_artifact_id")?;
            let keyframe_name: Option<String> = row.try_get("keyframe_name")?;
            let keyframe = match (keyframe_id, keyframe_name) {
                (None, None) => None,
                (Some(id), Some(name)) => Some(
                    VideoKeyframe::from_artifact_name(parse_text_id(&id)?, &name)
                        .map_err(|_| StoreError::new(ErrorCode::CorruptState))?,
                ),
                _ => return Err(StoreError::new(ErrorCode::CorruptState)),
            };
            EvidenceLocator::Video {
                start_ms: required_u64(row, "start_ms")?,
                end_ms: required_u64(row, "end_ms")?,
                keyframe,
            }
        }
        _ => return Err(StoreError::new(ErrorCode::CorruptState)),
    };
    let view = EvidenceView {
        evidence_id: parse_id(row, "id")?,
        source_id: parse_id(row, "source_id")?,
        job_id: parse_id(row, "job_id")?,
        source_artifact_id: parse_id(row, "artifact_id")?,
        locator,
        quote: row.try_get("quote")?,
    };
    validate_evidence_view(&view)?;
    Ok(view)
}

fn validate_evidence_view(view: &EvidenceView) -> Result<(), StoreError> {
    let manifest = flori_core::EvidenceManifest {
        schema: flori_core::EvidenceManifestSchema::V1,
        items: vec![flori_core::EvidenceEntry {
            evidence_id: view.evidence_id,
            source_artifact_id: view.source_artifact_id,
            locator: view.locator.clone(),
            quote: view.quote.clone(),
        }],
    };
    manifest
        .validate_structure()
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn parse_id<T>(row: &SqliteRow, column: &str) -> Result<T, StoreError>
where
    T: FromStr,
{
    parse_text_id(&row.try_get::<String, _>(column)?)
}

fn parse_text_id<T: FromStr>(value: &str) -> Result<T, StoreError> {
    value
        .parse()
        .map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn required_u32(row: &SqliteRow, column: &str) -> Result<u32, StoreError> {
    let value: i64 = row.try_get(column)?;
    u32::try_from(value).map_err(|_| StoreError::new(ErrorCode::CorruptState))
}

fn required_u64(row: &SqliteRow, column: &str) -> Result<u64, StoreError> {
    let value: i64 = row.try_get(column)?;
    u64::try_from(value).map_err(|_| StoreError::new(ErrorCode::CorruptState))
}
