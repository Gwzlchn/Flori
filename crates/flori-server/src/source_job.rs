use std::fmt::Write as _;

use axum::{
    Json, Router,
    extract::State,
    http::{StatusCode, Uri},
    routing::{get, post},
};
use flori_core::{
    CollectionView, CreateJobRequest, CreateRemoteSource, CreatedJob, CreatedSource, DomainView,
    ErrorCode, JobId, JobView, PdfSetupView, RerunJobRequest, Sha256Digest, SourceId, SourceKind,
    SourceView,
};
use flori_store::CreateSource;
use sha2::{Digest, Sha256};

use crate::{
    error::HttpError,
    protocol::{StrictJson, StrictPath},
    runner::HttpState,
};

pub(super) fn routes() -> Router<HttpState> {
    Router::new()
        .route("/api/v1/pdf/setup", get(pdf_setup))
        .route("/api/v1/domains", get(list_domains))
        .route("/api/v1/collections", get(list_collections))
        .route("/api/v1/sources", get(list_sources).post(create_source))
        .route(
            "/api/v1/sources/{source_id}",
            get(get_source).delete(delete_source),
        )
        .route("/api/v1/sources/{source_id}/jobs", post(create_job))
        .route("/api/v1/jobs/{job_id}", get(get_job))
        .route("/api/v1/jobs/{job_id}/cancel", post(cancel_job))
        .route("/api/v1/jobs/{job_id}/rerun", post(rerun_job))
}

async fn list_domains(State(state): State<HttpState>) -> Result<Json<Vec<DomainView>>, HttpError> {
    Ok(Json(state.store.list_domains().await?))
}

async fn list_collections(
    State(state): State<HttpState>,
) -> Result<Json<Vec<CollectionView>>, HttpError> {
    Ok(Json(state.store.list_collections().await?))
}

async fn list_sources(State(state): State<HttpState>) -> Result<Json<Vec<SourceView>>, HttpError> {
    Ok(Json(state.store.list_sources().await?))
}

async fn delete_source(
    State(state): State<HttpState>,
    StrictPath(source_id): StrictPath<SourceId>,
) -> Result<StatusCode, HttpError> {
    state
        .store
        .delete_source(&state.artifacts, source_id)
        .await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn pdf_setup(State(state): State<HttpState>) -> Result<Json<PdfSetupView>, HttpError> {
    state
        .store
        .pdf_setup()
        .await?
        .map(Json)
        .ok_or_else(|| HttpError::new(ErrorCode::NotFound))
}

async fn get_source(
    State(state): State<HttpState>,
    StrictPath(source_id): StrictPath<SourceId>,
) -> Result<Json<SourceView>, HttpError> {
    state
        .store
        .get_source(source_id)
        .await?
        .map(Json)
        .ok_or_else(|| HttpError::new(ErrorCode::NotFound))
}

async fn get_job(
    State(state): State<HttpState>,
    StrictPath(job_id): StrictPath<JobId>,
) -> Result<Json<JobView>, HttpError> {
    state
        .store
        .get_job(job_id)
        .await?
        .map(Json)
        .ok_or_else(|| HttpError::new(ErrorCode::NotFound))
}

async fn rerun_job(
    State(state): State<HttpState>,
    StrictPath(job_id): StrictPath<JobId>,
    StrictJson(request): StrictJson<RerunJobRequest>,
) -> Result<Json<CreatedJob>, HttpError> {
    let job_id = state
        .store
        .rerun_requested_job(&state.artifacts, job_id, &request, super::runner::now_ms()?)
        .await?;
    Ok(Json(CreatedJob { job_id }))
}

async fn cancel_job(
    State(state): State<HttpState>,
    StrictPath(job_id): StrictPath<JobId>,
) -> Result<StatusCode, HttpError> {
    state
        .store
        .cancel_job(&state.artifacts, job_id, super::runner::now_ms()?)
        .await?;
    Ok(StatusCode::NO_CONTENT)
}

async fn create_source(
    State(state): State<HttpState>,
    StrictJson(request): StrictJson<CreateRemoteSource>,
) -> Result<Json<CreatedSource>, HttpError> {
    let canonical_ref = canonical_ref(request.kind, &request.canonical_ref)?;
    let request_sha256 =
        digest(&serde_json::to_vec(&request).map_err(|_| HttpError::new(ErrorCode::Internal))?);
    let source_id = state
        .store
        .create_source(CreateSource {
            kind: request.kind,
            canonical_ref: &canonical_ref,
            title: request.title.as_deref(),
            domain_id: request.domain_id,
            collection_ids: &request.collection_ids,
            credential_id: request.credential_id,
            request_key: &request.request_key,
            request_sha256: request_sha256.as_str(),
            created_at_ms: super::runner::now_ms()?,
        })
        .await?;
    Ok(Json(CreatedSource { source_id }))
}

async fn create_job(
    State(state): State<HttpState>,
    StrictPath(source_id): StrictPath<SourceId>,
    StrictJson(request): StrictJson<CreateJobRequest>,
) -> Result<Json<CreatedJob>, HttpError> {
    let job_id = state
        .store
        .create_requested_job(source_id, &request, super::runner::now_ms()?)
        .await?;
    Ok(Json(CreatedJob { job_id }))
}

fn canonical_ref(kind: SourceKind, value: &str) -> Result<String, HttpError> {
    if value.contains('#') || value.trim() != value {
        return Err(HttpError::new(ErrorCode::InvalidRequest));
    }
    match kind {
        SourceKind::PdfUrl => {
            let uri = value
                .parse::<Uri>()
                .map_err(|_| HttpError::new(ErrorCode::InvalidRequest))?;
            let authority = uri
                .authority()
                .ok_or_else(|| HttpError::new(ErrorCode::InvalidRequest))?;
            if uri.scheme_str() != Some("https") || authority.as_str().contains('@') {
                return Err(HttpError::new(ErrorCode::InvalidRequest));
            }
            Ok(format!("url:{value}"))
        }
        SourceKind::Arxiv => arxiv_id(value)
            .map(|id| format!("arxiv:{id}"))
            .ok_or_else(|| HttpError::new(ErrorCode::InvalidRequest)),
        SourceKind::BilibiliVideo => bilibili_id(value)
            .map(|id| format!("bilibili:{id}"))
            .ok_or_else(|| HttpError::new(ErrorCode::InvalidRequest)),
        SourceKind::YoutubeVideo => youtube_id(value)
            .map(|id| format!("youtube:{id}"))
            .ok_or_else(|| HttpError::new(ErrorCode::InvalidRequest)),
        _ => Err(HttpError::new(ErrorCode::UnsupportedSource)),
    }
}

fn bilibili_id(value: &str) -> Option<&str> {
    let id = value
        .strip_prefix("bilibili:")
        .or_else(|| value.strip_prefix("https://www.bilibili.com/video/"))?;
    let id = id.strip_suffix('/').unwrap_or(id);
    valid_bilibili_id(id).then_some(id)
}

fn valid_bilibili_id(id: &str) -> bool {
    id.len() == 12
        && id.starts_with("BV")
        && id[2..].bytes().all(|byte| byte.is_ascii_alphanumeric())
}

fn youtube_id(value: &str) -> Option<&str> {
    let id = value
        .strip_prefix("youtube:")
        .or_else(|| value.strip_prefix("https://youtu.be/"))
        .or_else(|| value.strip_prefix("https://www.youtube.com/watch?v="))?;
    valid_youtube_id(id).then_some(id)
}

fn valid_youtube_id(id: &str) -> bool {
    id.len() == 11
        && id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

fn arxiv_id(value: &str) -> Option<&str> {
    let path = value
        .strip_prefix("https://arxiv.org/abs/")
        .or_else(|| value.strip_prefix("https://arxiv.org/pdf/"))?;
    let id = path.strip_suffix(".pdf").unwrap_or(path);
    let (prefix, version) = match id.split_once('v') {
        Some((prefix, version)) if !version.is_empty() => (prefix, Some(version)),
        Some(_) => return None,
        None => (id, None),
    };
    let (year_month, number) = prefix.split_once('.')?;
    (year_month.len() == 4
        && number.len() == 5
        && year_month.bytes().all(|byte| byte.is_ascii_digit())
        && number.bytes().all(|byte| byte.is_ascii_digit())
        && version.is_none_or(|value| value.bytes().all(|byte| byte.is_ascii_digit())))
    .then_some(id)
}

fn digest(bytes: &[u8]) -> Sha256Digest {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut output, "{byte:02x}").expect("writing to String cannot fail");
    }
    Sha256Digest::parse(output).expect("SHA-256 formatter is canonical")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn remote_pdf_references_are_strict_and_canonical() {
        let Ok(arxiv) = canonical_ref(SourceKind::Arxiv, "https://arxiv.org/abs/1706.03762") else {
            panic!("arXiv URL must be accepted");
        };
        assert_eq!(arxiv, "arxiv:1706.03762");
        assert!(canonical_ref(SourceKind::Arxiv, "https://arxiv.org/abs/1706.03762?x=1").is_err());
        assert!(canonical_ref(SourceKind::Arxiv, "https://arxiv.org/abs/1706.03762v").is_err());
        assert!(canonical_ref(SourceKind::PdfUrl, "https://user@example.com/a.pdf").is_err());
        assert!(canonical_ref(SourceKind::PdfUrl, "http://example.com/a.pdf").is_err());
    }

    #[test]
    fn platform_video_references_are_strict_and_canonical() {
        assert_eq!(
            canonical_ref(
                SourceKind::BilibiliVideo,
                "https://www.bilibili.com/video/BV1GJ411x7h7/"
            )
            .expect("Bilibili video"),
            "bilibili:BV1GJ411x7h7"
        );
        assert_eq!(
            canonical_ref(SourceKind::YoutubeVideo, "https://youtu.be/dQw4w9WgXcQ")
                .expect("YouTube video"),
            "youtube:dQw4w9WgXcQ"
        );
        assert!(canonical_ref(SourceKind::BilibiliVideo, "https://b23.tv/short").is_err());
        assert!(
            canonical_ref(
                SourceKind::BilibiliVideo,
                "https://www.bilibili.com/video/BV1GJ411x7h7?p=2"
            )
            .is_err()
        );
        assert!(
            canonical_ref(
                SourceKind::YoutubeVideo,
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123"
            )
            .is_err()
        );
        assert!(canonical_ref(SourceKind::YoutubeVideo, "https://evil.test/dQw4w9WgXcQ").is_err());
    }
}
