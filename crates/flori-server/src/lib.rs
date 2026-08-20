//! Home Core HTTP 边界。

#![forbid(unsafe_code)]

mod error;
mod protocol;
mod runner;

use std::sync::Arc;

use axum::Router;
use flori_store::Store;

pub fn app(store: Arc<Store>) -> Router {
    runner::routes(store)
}
