//! HTTP request and response types and the OpenAPI document (contract 3 in docs/contracts.md).
//! docs/openapi.json is generated from here; `openapi_is_current` keeps it in sync.

use std::collections::BTreeMap;

use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use serde::{Deserialize, Serialize};
use utoipa::{IntoParams, ToSchema};

use crate::pipeline::{
    AiSpec, FeedKind, MetaValue, Pipeline, Platform, PromptName, StepName, TranslateMode,
};

/// Only the drift test builds the document; the server never serves it.
#[cfg(test)]
#[derive(utoipa::OpenApi)]
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
        crate::http::list_prompts,
        crate::http::set_prompt,
        crate::http::reset_prompt,
        crate::http::list_domain_prompts,
        crate::http::set_domain_prompt,
        crate::http::health,
        crate::system::get_system,
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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema, sqlx::FromRow)]
pub struct Domain {
    pub id: i64,
    /// 1-32 characters in NFC without whitespace, control or invisible characters or any of
    /// `/\:*?"<>|#^[],;`, not starting with `.` or `-`, not ending with `.`; unique ignoring
    /// case (contract 3). Also the vault folder name and, for a domain, a tag. Immutable.
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
    /// Same rule as `Domain.name`, unique within the domain; `unsorted` is reserved.
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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema, sqlx::FromRow)]
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

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema, sqlx::FromRow)]
pub struct JobSummary {
    /// Integer primary key: the `{id}` of every job endpoint and the name of `jobs/<id>/`.
    pub id: i64,
    /// String id written to the note's frontmatter as `job_id`:
    /// `<bili|yt|arxiv>-<source id>-<6 hex>`. Derived from platform, source_id and created, not
    /// stored; no endpoint takes it.
    #[sqlx(skip)]
    pub job_key: String,
    pub pipeline: Pipeline,
    pub platform: Platform,
    pub source_id: String,
    pub url: String,
    pub domain_id: i64,
    pub collection_id: Option<i64>,
    pub translate: TranslateMode,
    pub status: JobStatus,
    pub title: Option<String>,
    /// Uploader of a video, or a paper's authors joined with ", ". This and the next three are
    /// set when publish succeeds; null before that, when the source did not say, and on jobs
    /// published before these fields existed.
    pub author: Option<String>,
    /// Date the source was published, `YYYY-MM-DD`.
    pub published: Option<String>,
    /// Video length.
    pub duration_ms: Option<i64>,
    /// The note's one-line summary, as in its frontmatter.
    pub description: Option<String>,
    /// Step running now, or the step that failed or was canceled.
    pub current_step: Option<StepName>,
    /// Exit code of `current_step`; 30 means credentials expired.
    pub current_exit_code: Option<i32>,
    /// translate adds punctuation to a Chinese transcript instead of translating it, so pages
    /// name that step 加标点. Set by the job detail, and by the job list where `current_step` is
    /// translate, the one step name the list shows; false elsewhere.
    #[sqlx(skip)]
    pub punctuates: bool,
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
    /// job.json's `ai`: the tool, the models for note and translate, and the CLI version.
    pub ai: Option<AiSpec>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema, sqlx::FromRow)]
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
    #[sqlx(skip)]
    pub skipped_reason: Option<String>,
    /// translate on a Chinese video, which gets punctuation instead of a translation.
    #[sqlx(skip)]
    pub punctuation: Option<Punctuation>,
    /// `manifest.meta` of a succeeded step as written; empty before it succeeds.
    #[sqlx(skip)]
    pub meta: BTreeMap<String, MetaValue>,
    /// The step's own `manifest.files`, before the merge into `JobDetail.files` where later
    /// steps win.
    #[sqlx(skip)]
    pub files: Vec<JobFile>,
    /// The step's AI CLI calls over all attempts, oldest first, from `ai/` (contract 1).
    #[sqlx(skip)]
    pub ai_calls: Vec<AiCall>,
    pub started: Option<String>,
    pub finished: Option<String>,
}

/// From translate's `meta.punctuated`, `meta.sections`, `meta.kept_original`, `meta.corrected`
/// and `meta.reverted`. Jobs punctuated before corrections existed report 0 for the last two.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct Punctuation {
    /// Sections with text sent to the AI.
    pub sections: u32,
    /// Sections without the AI's punctuation; they keep the transcribed text.
    pub kept_original: u32,
    /// Same-sounding ideographs the AI put in for misheard ones, counted as runs.
    pub corrected: u32,
    /// Other changes of the AI, put back as transcribed, counted as runs.
    pub reverted: u32,
}

/// The record of one AI CLI call. Its files hold untrusted transcript and model text.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct AiCall {
    /// `<step>-a<attempt>-<k>`.
    pub name: String,
    pub attempt: u32,
    /// Counts the attempt's calls from 1.
    pub index: u32,
    /// Job-relative paths, in this order where present: `.prompt.md`, `.call.json`,
    /// `.out.json` or `.out.txt`, `.err.txt`.
    pub files: Vec<String>,
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

/// A prompt AI steps send and the text a step that starts now uses for it.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct PromptInfo {
    pub name: PromptName,
    /// What pages call it, e.g. 视频笔记.
    pub label: String,
    /// The built-in text from prompts/.
    pub default_text: String,
    /// The override when there is one, else the built-in text.
    pub text: String,
    /// Whether an override is stored.
    pub customized: bool,
}

/// One domain's requirement for a prompt, sent after the prompt's instructions under the
/// heading `## 领域要求(<domain>)` for the domain's jobs.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct DomainPrompt {
    pub name: PromptName,
    pub label: String,
    /// Empty when the domain has none.
    pub text: String,
    /// The prompt it goes after: `PromptInfo.text`.
    pub base: String,
}

/// Plain text. An override has 1-20000 characters; text equal to the built-in one removes the
/// override. A domain requirement has at most 4000 characters; empty text removes it. CRLF
/// becomes LF, and blank lines before and whitespace after the text are dropped.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
#[serde(deny_unknown_fields)]
pub struct SetPrompt {
    pub text: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct Health {
    pub version: String,
}

/// The running instance, read-only. Secrets are reported only as configured or not.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct SystemInfo {
    pub version: String,
    /// Short commit the binary was built from; `unknown` when the build did not say.
    pub commit: String,
    /// What new jobs get: the configured tool and models and the CLI version probed at start
    /// (`unknown` when the CLI did not answer).
    pub ai: AiSpec,
    /// Whether a Bilibili SESSDATA is configured. Says nothing about it still being valid.
    pub bili_login: bool,
    pub proxy: bool,
    /// Probed at start; `unknown` when yt-dlp did not answer.
    pub ytdlp_version: String,
    /// Last path component of the whisper model directory, e.g. `whisper-base`.
    pub whisper_model: String,
    pub data_dir: String,
    pub vault_name: String,
    /// Space left for the server's user on the filesystem of `data_dir`; null when unknown.
    pub disk_free_bytes: Option<u64>,
    pub disk_total_bytes: Option<u64>,
    pub domains: u32,
    pub collections: u32,
    /// Jobs that succeeded, i.e. published notes.
    pub notes: u32,
    /// Submissions a week has to reach.
    pub weekly_goal: u32,
    /// The current week and the three before it, newest first.
    pub weeks: Vec<WeekUse>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize, ToSchema)]
pub struct WeekUse {
    /// Monday of the week, `YYYY-MM-DD` in +08:00.
    pub start: String,
    /// Jobs created in the week, whatever became of them.
    pub submitted: u32,
    /// `submitted >= weekly_goal`. The current week may still get there.
    pub met: bool,
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
