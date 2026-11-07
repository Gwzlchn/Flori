//! Routes, Basic Auth, static files with SPA fallback, job file preview with Range, feed listing.

use std::sync::Arc;

use axum::{
    Json, Router,
    http::StatusCode,
    routing::{get, post, put},
};
use sqlx::SqlitePool;

use crate::{
    Config,
    api::{
        ApiError, Collection, Domain, FeedList, FeedQuery, FeedSubmit, FeedSubmitResult, Health,
        JobDetail, JobSummary, JobsQuery, LogChunk, LogQuery, NewCollection, NewDomain, SetFeed,
        SubmitJob,
    },
    exec::ExecHandle,
};

#[derive(Clone)]
pub struct AppState {
    pub db: SqlitePool,
    pub exec: ExecHandle,
    pub config: Arc<Config>,
}

pub fn router(state: AppState) -> Router {
    Router::new()
        .route("/api/domains", get(list_domains).post(create_domain))
        .route("/api/domains/{id}", axum::routing::delete(delete_domain))
        .route(
            "/api/collections",
            get(list_collections).post(create_collection),
        )
        .route(
            "/api/collections/{id}",
            axum::routing::delete(delete_collection),
        )
        .route("/api/collections/{id}/feed", put(set_feed).get(list_feed))
        .route("/api/collections/{id}/feed/jobs", post(submit_feed))
        .route("/api/jobs", get(list_jobs).post(submit_job))
        .route("/api/jobs/{id}", get(get_job))
        .route("/api/jobs/{id}/logs", get(get_logs))
        .route("/api/jobs/{id}/retry", post(retry_job))
        .route("/api/jobs/{id}/cancel", post(cancel_job))
        .route("/api/jobs/{id}/files/{*path}", get(get_file))
        .route("/api/health", get(health))
        .with_state(state)
}

// Handler bodies below are skeleton stubs; the annotations are the frozen contract.

#[utoipa::path(get, path = "/api/domains", responses((status = 200, body = [Domain])))]
pub async fn list_domains() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    post, path = "/api/domains", request_body = NewDomain,
    responses((status = 201, body = Domain), (status = 400, body = ApiError), (status = 409, body = ApiError))
)]
pub async fn create_domain() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    delete, path = "/api/domains/{id}", params(("id" = i64, Path)),
    responses((status = 204), (status = 404, body = ApiError), (status = 409, body = ApiError))
)]
pub async fn delete_domain() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(get, path = "/api/collections", responses((status = 200, body = [Collection])))]
pub async fn list_collections() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    post, path = "/api/collections", request_body = NewCollection,
    responses(
        (status = 201, body = Collection), (status = 400, body = ApiError),
        (status = 404, body = ApiError), (status = 409, body = ApiError)
    )
)]
pub async fn create_collection() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    delete, path = "/api/collections/{id}", params(("id" = i64, Path)),
    responses((status = 204), (status = 404, body = ApiError), (status = 409, body = ApiError))
)]
pub async fn delete_collection() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    put, path = "/api/collections/{id}/feed", params(("id" = i64, Path)), request_body = SetFeed,
    responses((status = 200, body = Collection), (status = 400, body = ApiError), (status = 404, body = ApiError))
)]
pub async fn set_feed() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    get, path = "/api/collections/{id}/feed", params(("id" = i64, Path), FeedQuery),
    responses(
        (status = 200, body = FeedList), (status = 400, body = ApiError), (status = 404, body = ApiError),
        (status = 409, body = ApiError), (status = 502, body = ApiError)
    )
)]
pub async fn list_feed() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    post, path = "/api/collections/{id}/feed/jobs", params(("id" = i64, Path)), request_body = FeedSubmit,
    responses((status = 200, body = FeedSubmitResult), (status = 400, body = ApiError), (status = 404, body = ApiError))
)]
pub async fn submit_feed() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    post, path = "/api/jobs", request_body = SubmitJob,
    responses(
        (status = 201, body = JobSummary), (status = 400, body = ApiError),
        (status = 404, body = ApiError), (status = 409, body = ApiError)
    )
)]
pub async fn submit_job() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(get, path = "/api/jobs", params(JobsQuery), responses((status = 200, body = [JobSummary])))]
pub async fn list_jobs() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    get, path = "/api/jobs/{id}", params(("id" = i64, Path)),
    responses((status = 200, body = JobDetail), (status = 404, body = ApiError))
)]
pub async fn get_job() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    get, path = "/api/jobs/{id}/logs", params(("id" = i64, Path), LogQuery),
    responses((status = 200, body = LogChunk), (status = 404, body = ApiError))
)]
pub async fn get_logs() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    post, path = "/api/jobs/{id}/retry", params(("id" = i64, Path)),
    responses((status = 200, body = JobSummary), (status = 404, body = ApiError), (status = 409, body = ApiError))
)]
pub async fn retry_job() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(
    post, path = "/api/jobs/{id}/cancel", params(("id" = i64, Path)),
    responses((status = 200, body = JobSummary), (status = 404, body = ApiError), (status = 409, body = ApiError))
)]
pub async fn cancel_job() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

/// Raw bytes of a file inside the job directory. Supports `Range` for video playback.
#[utoipa::path(
    get, path = "/api/jobs/{id}/files/{path}",
    params(("id" = i64, Path), ("path" = String, Path, description = "Job-relative path")),
    responses((status = 200, description = "File bytes"), (status = 206, description = "Partial content"), (status = 404, body = ApiError))
)]
pub async fn get_file() -> StatusCode {
    StatusCode::NOT_IMPLEMENTED
}

#[utoipa::path(get, path = "/api/health", responses((status = 200, body = Health)))]
pub async fn health() -> Json<Health> {
    Json(Health {
        version: env!("CARGO_PKG_VERSION").to_owned(),
    })
}
