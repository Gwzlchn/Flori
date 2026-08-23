use axum::{Json, Router, extract::State, routing::get};
use flori_core::RunnerView;

use crate::{error::HttpError, runner::HttpState};

pub(super) fn routes() -> Router<HttpState> {
    Router::new().route("/api/v1/runners", get(list_runners))
}

async fn list_runners(State(state): State<HttpState>) -> Result<Json<Vec<RunnerView>>, HttpError> {
    let runners = state
        .store
        .list_runners(super::runner::now_ms()?, state.lease_ms)
        .await?;
    Ok(Json(runners))
}
