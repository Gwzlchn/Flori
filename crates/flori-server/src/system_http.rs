use std::{fmt::Write as _, time::Duration};

use axum::{
    Json, Router,
    body::Body,
    extract::{Query, State},
    http::{
        HeaderMap,
        header::{CACHE_CONTROL, CONTENT_TYPE, HeaderName},
    },
    response::Response,
    routing::get,
};
use flori_core::{ErrorCode, JobEvent, JobEventPayload, JobId, SystemView};
use serde::Deserialize;
use tokio::io::AsyncWriteExt;
use tokio_util::io::ReaderStream;

use crate::{error::HttpError, protocol::StrictPath, runner::HttpState};

const LAST_EVENT_ID: HeaderName = HeaderName::from_static("last-event-id");
const BATCH_SIZE: u16 = 256;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct EventQuery {
    after: Option<u64>,
}

pub(super) fn routes() -> Router<HttpState> {
    Router::new()
        .route("/api/v1/system", get(system))
        .route("/api/v1/events", get(events))
        .route("/api/v1/jobs/{job_id}/events", get(job_events))
}

async fn system(State(state): State<HttpState>) -> Result<Json<SystemView>, HttpError> {
    let now_ms = super::runner::now_ms()?;
    let online_after =
        now_ms.saturating_sub(i64::try_from(state.lease_ms.saturating_mul(2)).unwrap_or(i64::MAX));
    state
        .store
        .system_view(&state.artifacts, online_after)
        .await
        .map(Json)
        .map_err(Into::into)
}

async fn events(
    State(state): State<HttpState>,
    headers: HeaderMap,
    Query(query): Query<EventQuery>,
) -> Result<Response, HttpError> {
    event_stream(state, None, cursor(&headers, query.after)?).await
}

async fn job_events(
    State(state): State<HttpState>,
    StrictPath(job_id): StrictPath<JobId>,
    headers: HeaderMap,
    Query(query): Query<EventQuery>,
) -> Result<Response, HttpError> {
    if state.store.get_job(job_id).await?.is_none() {
        return Err(HttpError::new(ErrorCode::NotFound));
    }
    event_stream(state, Some(job_id), cursor(&headers, query.after)?).await
}

async fn event_stream(
    state: HttpState,
    job_id: Option<JobId>,
    after: u64,
) -> Result<Response, HttpError> {
    let initial = state.store.read_events(job_id, after, BATCH_SIZE).await?;
    let (output, input) = tokio::io::duplex(64 * 1024);
    tokio::spawn(pump(state, job_id, after, initial, output));
    Ok(Response::builder()
        .header(CONTENT_TYPE, "text/event-stream")
        .header(CACHE_CONTROL, "no-cache")
        .body(Body::from_stream(ReaderStream::new(input)))
        .expect("static SSE response is valid"))
}

async fn pump(
    state: HttpState,
    job_id: Option<JobId>,
    mut cursor: u64,
    mut batch: Vec<JobEvent>,
    mut output: tokio::io::DuplexStream,
) {
    let mut idle_seconds = 0_u8;
    loop {
        let full = batch.len() == usize::from(BATCH_SIZE);
        if !batch.is_empty() {
            idle_seconds = 0;
        }
        for item in batch {
            cursor = item.id;
            if output.write_all(&sse_event(item)).await.is_err() {
                return;
            }
        }
        if !full {
            tokio::time::sleep(Duration::from_secs(1)).await;
            idle_seconds += 1;
            if idle_seconds == 15 {
                if output.write_all(b": keep-alive\n\n").await.is_err() {
                    return;
                }
                idle_seconds = 0;
            }
        }
        batch = match state.store.read_events(job_id, cursor, BATCH_SIZE).await {
            Ok(value) => value,
            Err(_) => return,
        };
    }
}

fn cursor(headers: &HeaderMap, query: Option<u64>) -> Result<u64, HttpError> {
    let mut values = headers.get_all(&LAST_EVENT_ID).iter();
    let header = values.next();
    if values.next().is_some() {
        return Err(HttpError::new(ErrorCode::InvalidRequest));
    }
    let header = header
        .map(|value| {
            let text = value
                .to_str()
                .map_err(|_| HttpError::new(ErrorCode::InvalidRequest))?;
            let parsed = text
                .parse::<u64>()
                .map_err(|_| HttpError::new(ErrorCode::InvalidRequest))?;
            if text != parsed.to_string() {
                return Err(HttpError::new(ErrorCode::InvalidRequest));
            }
            Ok(parsed)
        })
        .transpose()?;
    match (query, header) {
        (Some(left), Some(right)) if left != right => {
            Err(HttpError::new(ErrorCode::InvalidRequest))
        }
        (Some(value), _) | (_, Some(value)) => Ok(value),
        (None, None) => Ok(0),
    }
}

fn sse_event(item: JobEvent) -> Vec<u8> {
    let created_at_ms = item.created_at_ms;
    let (kind, data) = match item.payload {
        JobEventPayload::SourceChanged(value) => ("source_changed", json(&value)),
        JobEventPayload::JobState(value) => ("job_state", json(&value)),
        JobEventPayload::TaskState(value) => ("task_state", json(&value)),
        JobEventPayload::ArtifactCommitted(value) => ("artifact_committed", json(&value)),
        JobEventPayload::LogCursor(value) => ("log_cursor", json(&value)),
        JobEventPayload::RunnerChanged(value) => ("runner_changed", json(&value)),
        JobEventPayload::SystemHealth(value) => ("system_health", json(&value)),
    };
    let mut output = String::new();
    write!(
        output,
        "id: {}\nevent: {kind}\nevent-time-ms: {created_at_ms}\ndata: {data}\n\n",
        item.id
    )
    .expect("writing to String cannot fail");
    output.into_bytes()
}

fn json(value: &impl serde::Serialize) -> String {
    serde_json::to_string(value).expect("closed event contract serializes")
}
