// utoipa consumes these marker functions while deriving the single HTTP contract.
#![allow(dead_code)]

use crate::{
    ArtifactId, ArtifactView, CollectionView, CreateJobRequest, CreateUploadSourceForm, CreatedJob,
    CreatedSource, DocumentRepresentationView, DomainView, ErrorResponse, EvidenceId, EvidenceView,
    JobId, JobView, PdfSetupView, RerunJobRequest, RunnerView, SearchHit, SourceId, SourceView,
    SystemView,
};

#[utoipa::path(
    get,
    path = "/api/v1/system",
    responses((status = 200, body = SystemView), (status = 500, body = ErrorResponse))
)]
pub(crate) fn system() {}

#[utoipa::path(
    get,
    path = "/api/v1/events",
    params(
        ("after" = Option<u64>, Query),
        ("Last-Event-ID" = Option<u64>, Header)
    ),
    responses(
        (status = 200, body = String, content_type = "text/event-stream"),
        (status = 400, body = ErrorResponse),
    )
)]
pub(crate) fn events() {}

#[utoipa::path(
    get,
    path = "/api/v1/jobs/{job_id}/events",
    params(
        ("job_id" = JobId, Path),
        ("after" = Option<u64>, Query),
        ("Last-Event-ID" = Option<u64>, Header)
    ),
    responses(
        (status = 200, body = String, content_type = "text/event-stream"),
        (status = 400, body = ErrorResponse),
        (status = 404, body = ErrorResponse),
    )
)]
pub(crate) fn job_events() {}

#[utoipa::path(
    get,
    path = "/api/v1/pdf/setup",
    responses(
        (status = 200, body = PdfSetupView),
        (status = 404, body = ErrorResponse),
        (status = 500, body = ErrorResponse),
    )
)]
pub(crate) fn pdf_setup() {}

#[utoipa::path(
    post,
    path = "/api/v1/sources/uploads",
    request_body(content = CreateUploadSourceForm, content_type = "multipart/form-data"),
    responses(
        (status = 200, body = CreatedSource),
        (status = 400, body = ErrorResponse),
        (status = 409, body = ErrorResponse),
    )
)]
pub(crate) fn upload_source() {}

#[utoipa::path(
    get,
    path = "/api/v1/domains",
    responses((status = 200, body = Vec<DomainView>), (status = 500, body = ErrorResponse))
)]
pub(crate) fn domains() {}

#[utoipa::path(
    get,
    path = "/api/v1/collections",
    responses((status = 200, body = Vec<CollectionView>), (status = 500, body = ErrorResponse))
)]
pub(crate) fn collections() {}

#[utoipa::path(
    get,
    path = "/api/v1/sources",
    responses((status = 200, body = Vec<SourceView>), (status = 500, body = ErrorResponse))
)]
pub(crate) fn sources() {}

#[utoipa::path(
    get,
    path = "/api/v1/sources/{source_id}",
    params(("source_id" = SourceId, Path)),
    responses((status = 200, body = SourceView), (status = 404, body = ErrorResponse))
)]
pub(crate) fn source_detail() {}

#[utoipa::path(
    get,
    path = "/api/v1/sources/{source_id}/document",
    params(("source_id" = SourceId, Path), ("evidence_id" = Option<EvidenceId>, Query)),
    responses(
        (status = 200, body = DocumentRepresentationView),
        (status = 400, body = ErrorResponse),
        (status = 404, body = ErrorResponse),
        (status = 500, body = ErrorResponse),
    )
)]
pub(crate) fn source_document() {}

#[utoipa::path(
    get,
    path = "/api/v1/sources/{source_id}/document/content",
    params(("source_id" = SourceId, Path)),
    responses(
        (status = 200, body = String, content_type = "text/html"),
        (status = 404, body = ErrorResponse),
        (status = 500, body = ErrorResponse),
    )
)]
pub(crate) fn source_document_content() {}

#[utoipa::path(
    delete,
    path = "/api/v1/sources/{source_id}",
    params(("source_id" = SourceId, Path)),
    responses(
        (status = 204),
        (status = 400, body = ErrorResponse),
        (status = 409, body = ErrorResponse),
        (status = 500, body = ErrorResponse),
    )
)]
pub(crate) fn delete_source() {}

#[utoipa::path(
    post,
    path = "/api/v1/sources/{source_id}/jobs",
    params(("source_id" = SourceId, Path)),
    request_body = CreateJobRequest,
    responses(
        (status = 200, body = CreatedJob),
        (status = 400, body = ErrorResponse),
        (status = 404, body = ErrorResponse),
        (status = 409, body = ErrorResponse),
    )
)]
pub(crate) fn create_job() {}

#[utoipa::path(
    get,
    path = "/api/v1/jobs/{job_id}",
    params(("job_id" = JobId, Path)),
    responses((status = 200, body = JobView), (status = 404, body = ErrorResponse))
)]
pub(crate) fn job_detail() {}

#[utoipa::path(
    post,
    path = "/api/v1/jobs/{job_id}/cancel",
    params(("job_id" = JobId, Path)),
    responses(
        (status = 204),
        (status = 404, body = ErrorResponse),
        (status = 409, body = ErrorResponse),
    )
)]
pub(crate) fn cancel_job() {}

#[utoipa::path(
    post,
    path = "/api/v1/jobs/{job_id}/rerun",
    params(("job_id" = JobId, Path)),
    request_body = RerunJobRequest,
    responses(
        (status = 200, body = CreatedJob),
        (status = 400, body = ErrorResponse),
        (status = 404, body = ErrorResponse),
        (status = 409, body = ErrorResponse),
    )
)]
pub(crate) fn rerun_job() {}

#[utoipa::path(
    get,
    path = "/api/v1/runners",
    responses((status = 200, body = Vec<RunnerView>), (status = 500, body = ErrorResponse))
)]
pub(crate) fn runners() {}

#[utoipa::path(
    get,
    path = "/api/v1/artifacts/{artifact_id}",
    params(("artifact_id" = ArtifactId, Path)),
    responses((status = 200, body = ArtifactView), (status = 404, body = ErrorResponse))
)]
pub(crate) fn artifact_detail() {}

#[utoipa::path(
    get,
    path = "/api/v1/artifacts/{artifact_id}/content",
    params(("artifact_id" = ArtifactId, Path)),
    responses(
        (status = 200, body = String, content_type = "application/octet-stream"),
        (status = 206, body = String, content_type = "application/octet-stream"),
        (status = 404, body = ErrorResponse),
    )
)]
pub(crate) fn artifact_content() {}

#[utoipa::path(
    get,
    path = "/api/v1/search",
    params(("q" = String, Query), ("limit" = u32, Query)),
    responses((status = 200, body = Vec<SearchHit>), (status = 400, body = ErrorResponse))
)]
pub(crate) fn search() {}

#[utoipa::path(
    get,
    path = "/api/v1/evidence/{evidence_id}",
    params(("evidence_id" = EvidenceId, Path)),
    responses((status = 200, body = EvidenceView), (status = 404, body = ErrorResponse))
)]
pub(crate) fn evidence() {}
