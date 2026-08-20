use std::{
    fmt::Write as _,
    net::IpAddr,
    sync::Arc,
    time::{SystemTime, UNIX_EPOCH},
};

use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, State},
    http::{HeaderMap, StatusCode, Uri, header::CONTENT_TYPE},
    middleware,
    response::{IntoResponse, Response},
    routing::post,
};
use flori_core::{
    ErrorCode, LogCursor, LogFrame, RegisterRunnerRequest, RegisterRunnerResponse,
    RenewLeaseResponse, RequestId, RunnerId, Sha256Digest, TaskClaim, UsageAck, UsageUpdate,
};
use flori_store::Store;
use sha2::{Digest, Sha256};

use crate::{
    error::HttpError,
    protocol::{BearerToken, StrictBytes, StrictJson, StrictPath, require_v1},
};

#[derive(Clone)]
struct HttpState {
    store: Arc<Store>,
    artifact_download_base: Arc<str>,
    lease_ms: u64,
}

pub(super) fn routes(
    store: Arc<Store>,
    artifact_download_base: String,
    lease_ms: u64,
) -> Result<Router, ErrorCode> {
    if lease_ms == 0 || lease_ms > 86_400_000 || !valid_download_base(&artifact_download_base) {
        return Err(ErrorCode::InvalidRequest);
    }
    let state = HttpState {
        store,
        artifact_download_base: artifact_download_base.into(),
        lease_ms,
    };
    Ok(Router::new()
        .route("/runner/v1/register", post(register))
        .route(
            "/runner/v1/poll",
            post(poll).layer(DefaultBodyLimit::max(1)),
        )
        .route(
            "/runner/v1/attempts/{exec_id}/renew",
            post(renew).layer(DefaultBodyLimit::max(1)),
        )
        .route(
            "/runner/v1/attempts/{exec_id}/logs",
            post(logs).layer(DefaultBodyLimit::max(10 * 1024 * 1024)),
        )
        .route("/runner/v1/attempts/{exec_id}/usage", post(usage))
        .fallback(not_found)
        .method_not_allowed_fallback(method_not_allowed)
        .layer(middleware::from_fn(require_v1))
        .with_state(state))
}

async fn register(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictJson(request): StrictJson<RegisterRunnerRequest>,
) -> Result<Json<RegisterRunnerResponse>, HttpError> {
    let now_ms = now_ms()?;
    let registration_digest = token_digest(token.expose())?;
    let long_token = new_token();
    let long_digest = token_digest(&long_token)?;
    let runner_id = state
        .store
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

async fn poll(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictBytes(body): StrictBytes,
) -> Result<Response, HttpError> {
    require_empty(&body)?;
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    let lease_expires_at_ms = lease_expiry(now_ms, state.lease_ms)?;
    match state
        .store
        .poll_and_claim(
            runner_id,
            now_ms,
            lease_expires_at_ms,
            &state.artifact_download_base,
        )
        .await?
    {
        Some(claim) => Ok(Json::<TaskClaim>(claim).into_response()),
        None => Ok(StatusCode::NO_CONTENT.into_response()),
    }
}

async fn renew(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictPath(exec_id): StrictPath<flori_core::AttemptId>,
    StrictBytes(body): StrictBytes,
) -> Result<Json<RenewLeaseResponse>, HttpError> {
    require_empty(&body)?;
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    let lease_expires_at_ms = lease_expiry(now_ms, state.lease_ms)?;
    let lease = state
        .store
        .renew_lease(exec_id, runner_id, now_ms, lease_expires_at_ms)
        .await?;
    Ok(Json(RenewLeaseResponse {
        lease_expires_at_ms: lease.lease_expires_at_ms,
    }))
}

async fn logs(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictPath(exec_id): StrictPath<flori_core::AttemptId>,
    headers: HeaderMap,
    StrictBytes(body): StrictBytes,
) -> Result<Json<LogCursor>, HttpError> {
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    let frames = parse_ndjson(&headers, &body)?;
    Ok(Json(
        state
            .store
            .append_log_frames(runner_id, exec_id, &frames, now_ms)
            .await?,
    ))
}

async fn usage(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictPath(exec_id): StrictPath<flori_core::AttemptId>,
    StrictJson(update): StrictJson<UsageUpdate>,
) -> Result<Json<UsageAck>, HttpError> {
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    Ok(Json(
        state
            .store
            .apply_usage_update(runner_id, exec_id, &update, now_ms)
            .await?,
    ))
}

async fn authenticate(
    state: &HttpState,
    token: &BearerToken,
    now_ms: i64,
) -> Result<RunnerId, HttpError> {
    let digest = token_digest(token.expose())?;
    state
        .store
        .authenticate_runner(&digest, now_ms)
        .await
        .map_err(|error| match error.code() {
            ErrorCode::CredentialUnavailable => HttpError::unauthorized(),
            _ => error.into(),
        })
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

fn lease_expiry(now_ms: i64, lease_ms: u64) -> Result<i64, HttpError> {
    let lease_ms = i64::try_from(lease_ms).map_err(|_| HttpError::new(ErrorCode::Internal))?;
    now_ms
        .checked_add(lease_ms)
        .filter(|expires| *expires > now_ms)
        .ok_or_else(|| HttpError::new(ErrorCode::Internal))
}

fn valid_download_base(value: &str) -> bool {
    let Ok(uri) = value.parse::<Uri>() else {
        return false;
    };
    let Some(authority) = uri.authority() else {
        return false;
    };
    if value.ends_with('/') || uri.query().is_some() {
        return false;
    }
    uri.scheme_str() == Some("https")
        || uri.scheme_str() == Some("http")
            && (authority.host() == "localhost"
                || authority
                    .host()
                    .parse::<IpAddr>()
                    .is_ok_and(|address| address.is_loopback()))
}

fn parse_ndjson(headers: &HeaderMap, body: &[u8]) -> Result<Vec<LogFrame>, HttpError> {
    if headers
        .get(CONTENT_TYPE)
        .is_none_or(|value| value.as_bytes() != b"application/x-ndjson")
        || body.is_empty()
        || !body.ends_with(b"\n")
    {
        return Err(HttpError::new(ErrorCode::InvalidRequest));
    }
    let text = std::str::from_utf8(body).map_err(|_| HttpError::new(ErrorCode::InvalidRequest))?;
    text.strip_suffix('\n')
        .expect("body ending checked")
        .split('\n')
        .map(|line| {
            serde_json::from_str(line).map_err(|_| HttpError::new(ErrorCode::InvalidRequest))
        })
        .collect()
}

fn require_empty(body: &[u8]) -> Result<(), HttpError> {
    if body.is_empty() {
        Ok(())
    } else {
        Err(HttpError::new(ErrorCode::InvalidRequest))
    }
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

    #[test]
    fn remote_download_base_requires_https() {
        assert!(valid_download_base("https://flori.example/api/artifacts"));
        assert!(valid_download_base("http://localhost/artifacts"));
        assert!(!valid_download_base("http://flori.example/artifacts"));
        assert!(!valid_download_base("https://flori.example/artifacts/"));
    }

    #[test]
    fn ndjson_requires_content_type_final_newline_and_strict_frames() {
        let mut headers = HeaderMap::new();
        headers.insert(
            CONTENT_TYPE,
            "application/x-ndjson".parse().expect("header"),
        );
        let line = concat!(
            r#"{"sequence":1,"sha256":"2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b","line":"secret"}"#,
            "\n"
        );
        assert_eq!(
            parse_ndjson(&headers, line.as_bytes()).map_or(0, |v| v.len()),
            1
        );
        assert!(parse_ndjson(&headers, line.trim_end().as_bytes()).is_err());
        assert!(parse_ndjson(&HeaderMap::new(), line.as_bytes()).is_err());
        assert!(parse_ndjson(&headers, line.replace('}', ",\"extra\":1}").as_bytes()).is_err());
    }
}
