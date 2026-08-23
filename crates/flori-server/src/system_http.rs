use std::{convert::Infallible, time::Duration};

use axum::{
    Json, Router,
    extract::{Query, State},
    http::{HeaderMap, header::HeaderName},
    response::{
        IntoResponse, Response,
        sse::{Event, KeepAlive, Sse},
    },
    routing::get,
};
use flori_core::{ErrorCode, JobEvent, JobEventPayload, JobId, SystemHealthEvent, SystemView};
use serde::Deserialize;
use tokio::sync::mpsc;
use tokio_stream::wrappers::ReceiverStream;

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
    let view = state
        .store
        .system_view(&state.artifacts, online_after)
        .await?;
    state
        .store
        .append_event(
            &JobEventPayload::SystemHealth(SystemHealthEvent {
                status: view.status,
                queue_depth: view.queue_depth,
                disk_free_bytes: view.disk_free_bytes,
            }),
            now_ms,
        )
        .await?;
    Ok(Json(view))
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
    let (sender, receiver) = mpsc::channel(64);
    tokio::spawn(pump(state, job_id, after, initial, sender));
    Ok(Sse::new(ReceiverStream::new(receiver))
        .keep_alive(
            KeepAlive::new()
                .interval(Duration::from_secs(15))
                .text("keep-alive"),
        )
        .into_response())
}

async fn pump(
    state: HttpState,
    job_id: Option<JobId>,
    mut cursor: u64,
    mut batch: Vec<JobEvent>,
    sender: mpsc::Sender<Result<Event, Infallible>>,
) {
    loop {
        let full = batch.len() == usize::from(BATCH_SIZE);
        for item in batch {
            cursor = item.id;
            if sender.send(Ok(sse_event(item))).await.is_err() {
                return;
            }
        }
        if !full {
            tokio::time::sleep(Duration::from_secs(1)).await;
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

fn sse_event(item: JobEvent) -> Event {
    let (kind, data) = match item.payload {
        JobEventPayload::SourceChanged(value) => ("source_changed", json(&value)),
        JobEventPayload::JobState(value) => ("job_state", json(&value)),
        JobEventPayload::TaskState(value) => ("task_state", json(&value)),
        JobEventPayload::ArtifactCommitted(value) => ("artifact_committed", json(&value)),
        JobEventPayload::LogCursor(value) => ("log_cursor", json(&value)),
        JobEventPayload::RunnerChanged(value) => ("runner_changed", json(&value)),
        JobEventPayload::SystemHealth(value) => ("system_health", json(&value)),
    };
    Event::default()
        .id(item.id.to_string())
        .event(kind)
        .data(data)
}

fn json(value: &impl serde::Serialize) -> String {
    serde_json::to_string(value).expect("closed event contract serializes")
}
