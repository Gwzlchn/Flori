//! HTTP request and response types and the OpenAPI document (contract 3 in docs/contracts.md).
//! docs/openapi.json is generated from here; `openapi_is_current` keeps it in sync.

use std::collections::BTreeMap;

use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde::{Deserialize, Serialize};
use utoipa::{IntoParams, OpenApi, ToSchema};

use crate::pipeline::{FeedKind, Pipeline, Platform, StepName, TranslateMode};

#[derive(OpenApi)]
#[openapi(
    info(
        title = "Flori",
        version = "1",
        description = "Contract 3 in docs/contracts.md."
    ),
    paths(
        crate::http::list_domains,
        crate::http::create_domain,
        crate::http::delete_domain,
        crate::http::list_collections,
        crate::http::create_collection,
        crate::http::delete_collection,
        crate::http::set_feed,
        crate::http::list_feed,
        crate::http::submit_feed,
        crate::http::submit_job,
        crate::http::list_jobs,
        crate::http::get_job,
        crate::http::get_logs,
        crate::http::retry_job,
        crate::http::cancel_job,
        crate::http::get_file,
        crate::http::health,
    )
)]
pub struct ApiDoc;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    InvalidUrl,
    InvalidName,
    InvalidRequest,
    NotFound,
    Conflict,
    Upstream,
}

impl ErrorCode {
    pub fn status(self) -> StatusCode {
        match self {
            ErrorCode::InvalidUrl | ErrorCode::InvalidName | ErrorCode::InvalidRequest => {
                StatusCode::BAD_REQUEST
            }
            ErrorCode::NotFound => StatusCode::NOT_FOUND,
            ErrorCode::Conflict => StatusCode::CONFLICT,
            ErrorCode::Upstream => StatusCode::BAD_GATEWAY,
        }
    }
}

/// Every non-2xx JSON body.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct ApiError {
    pub code: ErrorCode,
    pub message: String,
    /// Only on `conflict` when an existing job caused it.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub job_id: Option<i64>,
}

impl ApiError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> Self {
        ApiError {
            code,
            message: message.into(),
            job_id: None,
        }
    }

    pub fn duplicate_job(job_id: i64) -> Self {
        ApiError {
            code: ErrorCode::Conflict,
            message: "job already exists".into(),
            job_id: Some(job_id),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.code.status(), Json(self)).into_response()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema, sqlx::Type)]
#[serde(rename_all = "lowercase")]
#[sqlx(rename_all = "lowercase")]
pub enum JobStatus {
    Queued,
    Running,
    Succeeded,
    Failed,
    Canceled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema, sqlx::Type)]
#[serde(rename_all = "lowercase")]
#[sqlx(rename_all = "lowercase")]
pub enum StepStatus {
    Pending,
    Running,
    Succeeded,
    Failed,
    Canceled,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct Domain {
    pub id: i64,
    /// Slug `^[a-z0-9][a-z0-9-]{0,47}$`; also the vault folder name. Immutable.
    pub name: String,
    pub created: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct NewDomain {
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct Collection {
    pub id: i64,
    pub domain_id: i64,
    pub name: String,
    pub feed: Option<Feed>,
    pub created: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct Feed {
    pub kind: FeedKind,
    /// Normalized URL.
    pub url: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct NewCollection {
    pub domain_id: i64,
    pub name: String,
}

/// `url: null` unbinds the feed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SetFeed {
    pub url: Option<String>,
}

#[derive(Debug, Clone, Deserialize, IntoParams)]
#[serde(deny_unknown_fields)]
#[into_params(parameter_in = Query)]
pub struct FeedQuery {
    /// 1-100, default 30.
    pub limit: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct FeedList {
    pub title: String,
    pub items: Vec<FeedItem>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct FeedItem {
    pub platform: Platform,
    pub source_id: String,
    pub title: String,
    /// `YYYY-MM-DD`.
    pub published: Option<String>,
    pub duration_s: Option<i64>,
    /// Existing job for this item in this collection.
    pub job: Option<JobRef>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct JobRef {
    pub id: i64,
    pub status: JobStatus,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct FeedSubmit {
    /// 1-20 items.
    pub items: Vec<FeedPick>,
    pub translate: Option<TranslateMode>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct FeedPick {
    pub platform: Platform,
    pub source_id: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct FeedSubmitResult {
    pub created: Vec<FeedPicked>,
    /// Items that already had a job in this collection.
    pub skipped: Vec<FeedPicked>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct FeedPicked {
    pub platform: Platform,
    pub source_id: String,
    pub job: JobRef,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SubmitJob {
    pub url: String,
    pub domain_id: i64,
    pub collection_id: Option<i64>,
    /// Default `auto`.
    pub translate: Option<TranslateMode>,
}

#[derive(Debug, Clone, Deserialize, IntoParams)]
#[serde(deny_unknown_fields)]
#[into_params(parameter_in = Query)]
pub struct JobsQuery {
    /// Default 200.
    pub limit: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct JobSummary {
    pub id: i64,
    pub pipeline: Pipeline,
    pub platform: Platform,
    pub source_id: String,
    pub url: String,
    pub domain_id: i64,
    pub collection_id: Option<i64>,
    pub translate: TranslateMode,
    pub status: JobStatus,
    pub title: Option<String>,
    /// Step running now, or the step that failed or was canceled.
    pub current_step: Option<StepName>,
    pub created: String,
    pub updated: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct JobDetail {
    pub job: JobSummary,
    pub steps: Vec<StepInfo>,
    /// Merged `manifest.files` of finished steps; later steps win, as in publish.
    pub files: Vec<JobFile>,
    /// Basename -> job-relative path, for rewriting `assets/<basename>` in previews.
    pub assets: BTreeMap<String, String>,
    /// Prefix for job-relative paths, `/api/jobs/{id}/files/`.
    pub preview_base: String,
    /// Path inside the vault after publish, e.g. `flori/ml/unsorted/2026-11-08-yt-abc/`.
    pub vault_path: Option<String>,
    /// `obsidian://open?vault=...&file=...` for the main note, after publish.
    pub obsidian_uri: Option<String>,
    pub job_url: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct StepInfo {
    pub seq: u32,
    pub name: StepName,
    pub status: StepStatus,
    pub attempts: u32,
    /// 30 means credentials expired.
    pub exit_code: Option<i32>,
    /// Last stderr line of the failed attempt.
    pub error: Option<String>,
    /// `meta.skipped_reason` from the step manifest.
    pub skipped_reason: Option<String>,
    pub started: Option<String>,
    pub finished: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct JobFile {
    /// Logical name from the manifest, e.g. `note`, `transcript`, `video`.
    pub name: String,
    /// Path relative to the job directory.
    pub path: String,
}

#[derive(Debug, Clone, Deserialize, IntoParams)]
#[serde(deny_unknown_fields)]
#[into_params(parameter_in = Query)]
pub struct LogQuery {
    /// Byte offset into job.log. Default 0.
    pub offset: Option<u64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct LogChunk {
    /// Whole UTF-8 lines starting at `offset`.
    pub text: String,
    pub next_offset: u64,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct Health {
    pub version: String,
}

#[cfg(test)]
mod tests {
    use utoipa::OpenApi;

    use super::ApiDoc;

    #[test]
    fn openapi_is_current() {
        let path = concat!(env!("CARGO_MANIFEST_DIR"), "/docs/openapi.json");
        let generated = ApiDoc::openapi()
            .to_pretty_json()
            .expect("serialize openapi")
            + "\n";
        if std::env::var_os("FLORI_UPDATE_OPENAPI").is_some() {
            std::fs::write(path, &generated).expect("write docs/openapi.json");
            return;
        }
        let committed = std::fs::read_to_string(path).unwrap_or_default();
        assert!(
            committed == generated,
            "docs/openapi.json is stale; run `just openapi`"
        );
    }
}
