use axum::{
    Router,
    body::Body,
    extract::{DefaultBodyLimit, State},
    http::{
        HeaderMap, HeaderValue, Response, StatusCode,
        header::{ACCEPT_RANGES, CONTENT_LENGTH, CONTENT_RANGE, CONTENT_TYPE, ETAG, RANGE},
    },
    response::IntoResponse,
    routing::get,
};
use flori_core::{ArtifactId, ErrorCode, SourceInputId};
use tokio::io::AsyncReadExt;
use tokio_util::io::ReaderStream;

use crate::{
    error::HttpError,
    protocol::{BearerToken, StrictBytes, StrictPath},
    runner::{HttpState, authenticate, now_ms},
};

pub(super) fn routes() -> Router<HttpState> {
    Router::new()
        .route(
            "/api/v1/artifacts/{id}/content",
            get(artifact).layer(DefaultBodyLimit::max(1)),
        )
        .route(
            "/api/v1/source-inputs/{id}/content",
            get(source_input).layer(DefaultBodyLimit::max(1)),
        )
}

async fn artifact(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictPath(id): StrictPath<ArtifactId>,
    headers: HeaderMap,
    StrictBytes(body): StrictBytes,
) -> Result<Response<Body>, HttpError> {
    if !body.is_empty() {
        return Err(HttpError::new(ErrorCode::InvalidRequest));
    }
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    let metadata = state
        .store
        .authorize_artifact_content(runner_id, id, now_ms)
        .await?;
    content(state, headers, metadata).await
}

async fn source_input(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictPath(id): StrictPath<SourceInputId>,
    headers: HeaderMap,
    StrictBytes(body): StrictBytes,
) -> Result<Response<Body>, HttpError> {
    if !body.is_empty() {
        return Err(HttpError::new(ErrorCode::InvalidRequest));
    }
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    let metadata = state
        .store
        .authorize_source_input_content(runner_id, id, now_ms)
        .await?;
    content(state, headers, metadata).await
}

async fn content(
    state: HttpState,
    headers: HeaderMap,
    (relative_path, media_type, size_bytes, sha256): (
        String,
        String,
        u64,
        flori_core::Sha256Digest,
    ),
) -> Result<Response<Body>, HttpError> {
    let (start, end_exclusive, partial) = match parse_range(&headers, size_bytes) {
        Ok(range) => range,
        Err(()) => return Ok(range_not_satisfiable(size_bytes)),
    };
    let artifacts = state.artifacts.clone();
    let digest = sha256.clone();
    let file = tokio::task::spawn_blocking(move || {
        artifacts.open_verified_range(&relative_path, size_bytes, &digest, start, end_exclusive)
    })
    .await
    .map_err(|_| HttpError::new(ErrorCode::Internal))?
    .map_err(|error| HttpError::new(error.code()))?;

    let remaining = end_exclusive - start;
    let file = tokio::fs::File::from_std(file).take(remaining);
    let body = ReaderStream::with_capacity(file, 64 * 1024);
    let mut response = Response::new(Body::from_stream(body));
    *response.status_mut() = if partial {
        StatusCode::PARTIAL_CONTENT
    } else {
        StatusCode::OK
    };
    response
        .headers_mut()
        .insert(ACCEPT_RANGES, HeaderValue::from_static("bytes"));
    insert_header(response.headers_mut(), CONTENT_TYPE, &media_type)?;
    insert_header(
        response.headers_mut(),
        CONTENT_LENGTH,
        &(end_exclusive - start).to_string(),
    )?;
    insert_header(
        response.headers_mut(),
        ETAG,
        &format!("\"{}\"", sha256.as_str()),
    )?;
    if partial {
        insert_header(
            response.headers_mut(),
            CONTENT_RANGE,
            &format!("bytes {start}-{}/{size_bytes}", end_exclusive - 1),
        )?;
    }
    Ok(response)
}

fn parse_range(headers: &HeaderMap, size: u64) -> Result<(u64, u64, bool), ()> {
    let mut values = headers.get_all(RANGE).iter();
    let Some(value) = values.next() else {
        return Ok((0, size, false));
    };
    if values.next().is_some() || size == 0 {
        return Err(());
    }
    let value = value.to_str().map_err(|_| ())?;
    let range = value.strip_prefix("bytes=").ok_or(())?;
    if range.contains(',') || range.contains(char::is_whitespace) {
        return Err(());
    }
    let (start, end) = range.split_once('-').ok_or(())?;
    let (start, end_exclusive) = if start.is_empty() {
        let suffix = parse_u64(end)?;
        if suffix == 0 {
            return Err(());
        }
        (size.saturating_sub(suffix), size)
    } else {
        let start = parse_u64(start)?;
        if start >= size {
            return Err(());
        }
        let end_exclusive = if end.is_empty() {
            size
        } else {
            parse_u64(end)?.checked_add(1).ok_or(())?.min(size)
        };
        if end_exclusive <= start {
            return Err(());
        }
        (start, end_exclusive)
    };
    Ok((start, end_exclusive, true))
}

fn parse_u64(value: &str) -> Result<u64, ()> {
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err(());
    }
    value.parse().map_err(|_| ())
}

fn range_not_satisfiable(size: u64) -> Response<Body> {
    let mut response = HttpError::new(ErrorCode::InvalidRequest).into_response();
    *response.status_mut() = StatusCode::RANGE_NOT_SATISFIABLE;
    if let Ok(value) = HeaderValue::from_str(&format!("bytes */{size}")) {
        response.headers_mut().insert(CONTENT_RANGE, value);
    }
    response
}

fn insert_header(
    headers: &mut HeaderMap,
    name: axum::http::header::HeaderName,
    value: &str,
) -> Result<(), HttpError> {
    let value =
        HeaderValue::from_str(value).map_err(|_| HttpError::new(ErrorCode::CorruptState))?;
    headers.insert(name, value);
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn range_is_single_and_bounded() {
        for (value, expected) in [
            ("bytes=1-3", (1, 4, true)),
            ("bytes=7-", (7, 10, true)),
            ("bytes=-3", (7, 10, true)),
            ("bytes=-20", (0, 10, true)),
        ] {
            let mut headers = HeaderMap::new();
            headers.insert(RANGE, value.parse().expect("header"));
            assert_eq!(parse_range(&headers, 10), Ok(expected));
        }
        for value in ["bytes=10-", "bytes=4-3", "bytes=0-1,3-4", "items=0-1"] {
            let mut headers = HeaderMap::new();
            headers.insert(RANGE, value.parse().expect("header"));
            assert_eq!(parse_range(&headers, 10), Err(()));
        }
    }
}
