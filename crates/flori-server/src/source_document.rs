use std::{collections::BTreeSet, io::Read};

use axum::{
    Json, Router,
    body::Body,
    extract::{DefaultBodyLimit, State},
    http::{HeaderValue, Response, header},
    routing::get,
};
use flori_core::{
    ArtifactKind, ArtifactView, DocumentRepresentationView, ErrorCode, ScholarlyHtmlSnapshot,
    SourceId,
};

use crate::{
    error::HttpError,
    protocol::{StrictBytes, StrictPath},
    runner::HttpState,
    source_document_html,
};

struct DocumentBundle {
    pdf: ArtifactView,
    html: Option<HtmlBundle>,
}

struct HtmlBundle {
    html: String,
    snapshot: ScholarlyHtmlSnapshot,
    html_artifact: ArtifactView,
    snapshot_artifact: ArtifactView,
    resources: Vec<ArtifactView>,
}

pub(super) fn routes() -> Router<HttpState> {
    Router::new()
        .route("/api/v1/sources/{id}/document", get(document))
        .route("/api/v1/sources/{id}/document/content", get(content))
        .layer(DefaultBodyLimit::max(1))
}

async fn document(
    State(state): State<HttpState>,
    StrictPath(id): StrictPath<SourceId>,
    StrictBytes(body): StrictBytes,
) -> Result<Json<DocumentRepresentationView>, HttpError> {
    empty(&body)?;
    let bundle = load(&state, id).await?;
    let pdf_url = format!("/api/v1/artifacts/{}/content", bundle.pdf.artifact_id);
    let view = if let Some(html) = bundle.html {
        DocumentRepresentationView::ScholarlyHtml {
            source_id: id,
            job_id: bundle.pdf.job_id,
            provider: html.snapshot.provider,
            html_artifact_id: html.html_artifact.artifact_id,
            snapshot_artifact_id: html.snapshot_artifact.artifact_id,
            resources: html.resources,
            content_url: format!("/api/v1/sources/{id}/document/content"),
            fallback_pdf_artifact_id: bundle.pdf.artifact_id,
            fallback_pdf_url: pdf_url,
            crosswalk: None,
        }
    } else {
        DocumentRepresentationView::Pdf {
            source_id: id,
            job_id: bundle.pdf.job_id,
            pdf_artifact_id: bundle.pdf.artifact_id,
            content_url: pdf_url,
        }
    };
    Ok(Json(view))
}

async fn content(
    State(state): State<HttpState>,
    StrictPath(id): StrictPath<SourceId>,
    StrictBytes(body): StrictBytes,
) -> Result<Response<Body>, HttpError> {
    empty(&body)?;
    let html = load(&state, id)
        .await?
        .html
        .ok_or_else(|| HttpError::new(ErrorCode::NotFound))?;
    let names = html
        .resources
        .iter()
        .map(|resource| resource.name.as_str())
        .collect::<BTreeSet<_>>();
    let body = source_document_html::render(&html.html, &names).map_err(HttpError::new)?;
    let mut response = Response::new(Body::from(body));
    let headers = response.headers_mut();
    for (name, value) in [
        (header::CONTENT_TYPE, "text/html; charset=utf-8"),
        (
            header::CONTENT_SECURITY_POLICY,
            "default-src 'none'; img-src blob:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-same-origin",
        ),
        (header::REFERRER_POLICY, "no-referrer"),
        (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        (
            header::HeaderName::from_static("cross-origin-resource-policy"),
            "same-origin",
        ),
    ] {
        headers.insert(name, HeaderValue::from_static(value));
    }
    Ok(response)
}

async fn load(state: &HttpState, source_id: SourceId) -> Result<DocumentBundle, HttpError> {
    let source = state
        .store
        .get_source(source_id)
        .await?
        .ok_or_else(|| HttpError::new(ErrorCode::NotFound))?;
    let job_id = source
        .current_job_id
        .ok_or_else(|| HttpError::new(ErrorCode::NotFound))?;
    let job = state
        .store
        .get_job(job_id)
        .await?
        .filter(|job| job.source_id == source_id)
        .ok_or_else(|| HttpError::new(ErrorCode::CorruptState))?;
    let pdf = exact(&job.artifacts, ArtifactKind::SourceOriginal)?
        .ok_or_else(|| HttpError::new(ErrorCode::CorruptState))?;
    let html_artifact = exact(&job.artifacts, ArtifactKind::ScholarlyHtml)?;
    let snapshot_artifact = exact(&job.artifacts, ArtifactKind::ScholarlyHtmlSnapshot)?;
    let html = match (html_artifact, snapshot_artifact) {
        (None, None) => None,
        (Some(html_artifact), Some(snapshot_artifact)) => {
            let snapshot_bytes = read(state, &snapshot_artifact, 1024 * 1024).await?;
            let snapshot: ScholarlyHtmlSnapshot = serde_json::from_slice(&snapshot_bytes)
                .map_err(|_| HttpError::new(ErrorCode::CorruptState))?;
            snapshot
                .validate()
                .map_err(|_| HttpError::new(ErrorCode::CorruptState))?;
            if snapshot.job_id != job_id || !matches_file(&html_artifact, &snapshot.html) {
                return Err(HttpError::new(ErrorCode::CorruptState));
            }
            let resources = snapshot
                .resources
                .iter()
                .map(|frozen| {
                    job.artifacts
                        .iter()
                        .find(|item| matches_resource(item, frozen))
                        .cloned()
                        .ok_or_else(|| HttpError::new(ErrorCode::CorruptState))
                })
                .collect::<Result<Vec<_>, _>>()?;
            if resources.len()
                != job
                    .artifacts
                    .iter()
                    .filter(|item| item.kind == ArtifactKind::ScholarlyResource)
                    .count()
            {
                return Err(HttpError::new(ErrorCode::CorruptState));
            }
            let html_bytes =
                read(state, &html_artifact, flori_core::SCHOLARLY_HTML_MAX_BYTES).await?;
            let html_text = String::from_utf8(html_bytes)
                .map_err(|_| HttpError::new(ErrorCode::CorruptState))?;
            Some(HtmlBundle {
                html: html_text,
                snapshot,
                html_artifact,
                snapshot_artifact,
                resources,
            })
        }
        _ => return Err(HttpError::new(ErrorCode::CorruptState)),
    };
    Ok(DocumentBundle { pdf, html })
}

async fn read(state: &HttpState, artifact: &ArtifactView, max: u64) -> Result<Vec<u8>, HttpError> {
    if artifact.size_bytes == 0 || artifact.size_bytes > max {
        return Err(HttpError::new(ErrorCode::CorruptState));
    }
    let (view, path) = state
        .store
        .get_current_artifact(artifact.artifact_id)
        .await?
        .filter(|(view, _)| view == artifact)
        .ok_or_else(|| HttpError::new(ErrorCode::CorruptState))?;
    let artifacts = state.artifacts.clone();
    tokio::task::spawn_blocking(move || {
        let mut file = artifacts
            .open_verified_range(&path, view.size_bytes, &view.sha256, 0, view.size_bytes)
            .map_err(|error| HttpError::new(error.code()))?;
        let mut bytes = Vec::with_capacity(usize::try_from(view.size_bytes).unwrap_or(0));
        file.read_to_end(&mut bytes)
            .map_err(|_| HttpError::new(ErrorCode::StorageUnavailable))?;
        Ok(bytes)
    })
    .await
    .map_err(|_| HttpError::new(ErrorCode::Internal))?
}

fn exact(items: &[ArtifactView], kind: ArtifactKind) -> Result<Option<ArtifactView>, HttpError> {
    let mut found = items.iter().filter(|item| item.kind == kind);
    let first = found.next().cloned();
    if found.next().is_some() {
        return Err(HttpError::new(ErrorCode::CorruptState));
    }
    Ok(first)
}

fn matches_file(item: &ArtifactView, frozen: &flori_core::ScholarlyFile) -> bool {
    item.name == frozen.artifact_name
        && item.media_type == frozen.media_type
        && item.size_bytes == frozen.size_bytes
        && item.sha256 == frozen.sha256
}

fn matches_resource(item: &ArtifactView, frozen: &flori_core::ScholarlyResource) -> bool {
    item.name == frozen.artifact_name
        && item.media_type == frozen.media_type
        && item.size_bytes == frozen.size_bytes
        && item.sha256 == frozen.sha256
}

fn empty(body: &[u8]) -> Result<(), HttpError> {
    body.is_empty()
        .then_some(())
        .ok_or_else(|| HttpError::new(ErrorCode::InvalidRequest))
}
