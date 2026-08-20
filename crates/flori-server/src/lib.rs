//! Home Core HTTP 边界。

#![forbid(unsafe_code)]

mod error;
mod protocol;
mod runner;

use std::sync::Arc;

use axum::Router;
use flori_core::ErrorCode;
use flori_store::Store;

pub fn app(
    store: Arc<Store>,
    artifact_download_base: String,
    lease_ms: u64,
) -> Result<Router, ErrorCode> {
    runner::routes(store, artifact_download_base, lease_ms)
}
