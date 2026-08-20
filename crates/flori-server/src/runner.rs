use std::{
    fmt::Write as _,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

use axum::{Json, Router, extract::State, middleware, routing::post};
use flori_core::{
    ErrorCode, RegisterRunnerRequest, RegisterRunnerResponse, RequestId, Sha256Digest,
};
use flori_store::Store;
use sha2::{Digest, Sha256};

use crate::{
    error::HttpError,
    protocol::{BearerToken, StrictJson, require_v1},
};

pub(super) fn routes(store: Arc<Store>) -> Router {
    Router::new()
        .route("/runner/v1/register", post(register))
        .fallback(not_found)
        .method_not_allowed_fallback(method_not_allowed)
        .layer(middleware::from_fn(require_v1))
        .with_state(store)
}

async fn register(
    State(store): State<Arc<Store>>,
    token: BearerToken,
    StrictJson(request): StrictJson<RegisterRunnerRequest>,
) -> Result<Json<RegisterRunnerResponse>, HttpError> {
    let now_ms = now_ms()?;
    let registration_digest = token_digest(token.expose())?;
    let long_token = new_token();
    let long_digest = token_digest(&long_token)?;
    let runner_id = store
        .register_runner(&registration_digest, &long_digest, &request, now_ms)
        .await
        .map_err(|error| {
            if error.code() == ErrorCode::CredentialUnavailable {
                HttpError::unauthorized()
            } else {
                error.into()
            }
        })?;
    Ok(Json(RegisterRunnerResponse {
        runner_id,
        token: long_token,
    }))
}

async fn not_found() -> HttpError {
    HttpError::new(ErrorCode::NotFound)
}

async fn method_not_allowed() -> HttpError {
    HttpError::method_not_allowed()
}

fn now_ms() -> Result<i64, HttpError> {
    let milliseconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| HttpError::new(ErrorCode::Internal))?
        .as_millis();
    i64::try_from(milliseconds).map_err(|_| HttpError::new(ErrorCode::Internal))
}

fn new_token() -> String {
    format!("{}{}", RequestId::generate(), RequestId::generate())
}

fn token_digest(token: &str) -> Result<Sha256Digest, HttpError> {
    let digest = Sha256::digest(token.as_bytes());
    let mut encoded = String::with_capacity(64);
    for byte in digest {
        write!(encoded, "{byte:02x}").map_err(|_| HttpError::new(ErrorCode::Internal))?;
    }
    Sha256Digest::parse(encoded).map_err(|_| HttpError::new(ErrorCode::Internal))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_digest_is_canonical_and_stable() {
        let Ok(digest) = token_digest("secret") else {
            panic!("valid digest");
        };
        assert_eq!(
            digest.as_str(),
            "2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b"
        );
    }
}
