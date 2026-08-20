use std::fmt::Write as _;

use axum::{Json, Router, extract::State, http::Uri, routing::post};
use flori_core::{
    CreateJobRequest, CreateRemoteSource, CreatedJob, CreatedSource, ErrorCode, Sha256Digest,
    SourceId, SourceKind,
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
        .route("/api/v1/sources", post(create_source))
        .route("/api/v1/sources/{source_id}/jobs", post(create_job))
}

async fn create_source(
    State(state): State<HttpState>,
    StrictJson(request): StrictJson<CreateRemoteSource>,
) -> Result<Json<CreatedSource>, HttpError> {
    let canonical_ref = canonical_ref(request.kind, &request.canonical_ref)?;
    if request.credential_id.is_some() {
        return Err(HttpError::new(ErrorCode::InvalidRequest));
    }
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
        _ => Err(HttpError::new(ErrorCode::UnsupportedSource)),
    }
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
}
