use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, Query, State, rejection::QueryRejection},
    routing::get,
};
use flori_core::{ErrorCode, EvidenceId, EvidenceView, SearchHit};
use serde::Deserialize;

use crate::{
    error::HttpError,
    protocol::{StrictBytes, StrictPath},
    runner::HttpState,
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SearchQuery {
    q: String,
    limit: u32,
}

pub(super) fn routes() -> Router<HttpState> {
    Router::new()
        .route(
            "/api/v1/search",
            get(search).layer(DefaultBodyLimit::max(1)),
        )
        .route(
            "/api/v1/evidence/{id}",
            get(evidence).layer(DefaultBodyLimit::max(1)),
        )
}

async fn search(
    State(state): State<HttpState>,
    query: Result<Query<SearchQuery>, QueryRejection>,
    StrictBytes(body): StrictBytes,
) -> Result<Json<Vec<SearchHit>>, HttpError> {
    require_empty(&body)?;
    let Query(query) = query.map_err(|_| HttpError::new(ErrorCode::InvalidRequest))?;
    Ok(Json(
        state.store.search_current(&query.q, query.limit).await?,
    ))
}

async fn evidence(
    State(state): State<HttpState>,
    StrictPath(id): StrictPath<EvidenceId>,
    StrictBytes(body): StrictBytes,
) -> Result<Json<EvidenceView>, HttpError> {
    require_empty(&body)?;
    state
        .store
        .get_current_evidence(id)
        .await?
        .map(Json)
        .ok_or_else(|| HttpError::new(ErrorCode::NotFound))
}

fn require_empty(body: &[u8]) -> Result<(), HttpError> {
    body.is_empty()
        .then_some(())
        .ok_or_else(|| HttpError::new(ErrorCode::InvalidRequest))
}
