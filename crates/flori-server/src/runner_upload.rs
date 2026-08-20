use axum::{Json, extract::State, http::HeaderMap};
use flori_core::{
    AttemptAck, AttemptId, CompleteAttemptRequest, ErrorCode, FailAttemptRequest, Sha256Digest,
    StartUploadRequest, StartUploadResponse, UploadCursor, UploadId, VerifyUploadRequest,
    VerifyUploadResponse,
};

use crate::{
    error::HttpError,
    protocol::{BearerToken, StrictBytes, StrictJson, StrictPath},
    runner::{HttpState, authenticate, now_ms},
};

pub(super) const MAX_CHUNK_BYTES: usize = 8 * 1024 * 1024;

pub(super) async fn start(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictPath(exec_id): StrictPath<AttemptId>,
    StrictJson(request): StrictJson<StartUploadRequest>,
) -> Result<Json<StartUploadResponse>, HttpError> {
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    Ok(Json(
        state
            .store
            .start_attempt_upload(&state.artifacts, runner_id, exec_id, &request, now_ms)
            .await?,
    ))
}

pub(super) async fn append(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictPath(upload_id): StrictPath<UploadId>,
    headers: HeaderMap,
    StrictBytes(body): StrictBytes,
) -> Result<Json<UploadCursor>, HttpError> {
    require_content_type(&headers, "application/octet-stream")?;
    let offset = parse_offset(single_header(&headers, "Upload-Offset")?)?;
    let digest = Sha256Digest::parse(single_header(&headers, "X-Flori-Chunk-SHA256")?.to_owned())
        .map_err(|_| HttpError::new(ErrorCode::InvalidRequest))?;
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    Ok(Json(
        state
            .store
            .append_attempt_upload(
                &state.artifacts,
                runner_id,
                upload_id,
                offset,
                &digest,
                &body,
                now_ms,
            )
            .await?,
    ))
}

pub(super) async fn verify(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictPath(upload_id): StrictPath<UploadId>,
    StrictJson(request): StrictJson<VerifyUploadRequest>,
) -> Result<Json<VerifyUploadResponse>, HttpError> {
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    Ok(Json(
        state
            .store
            .verify_attempt_upload(&state.artifacts, runner_id, upload_id, &request, now_ms)
            .await?,
    ))
}

pub(super) async fn complete(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictPath(exec_id): StrictPath<AttemptId>,
    StrictJson(request): StrictJson<CompleteAttemptRequest>,
) -> Result<Json<AttemptAck>, HttpError> {
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    Ok(Json(
        state
            .store
            .complete_authenticated_attempt(&state.artifacts, runner_id, exec_id, &request, now_ms)
            .await?,
    ))
}

pub(super) async fn fail(
    State(state): State<HttpState>,
    token: BearerToken,
    StrictPath(exec_id): StrictPath<AttemptId>,
    StrictJson(request): StrictJson<FailAttemptRequest>,
) -> Result<Json<AttemptAck>, HttpError> {
    let now_ms = now_ms()?;
    let runner_id = authenticate(&state, &token, now_ms).await?;
    Ok(Json(
        state
            .store
            .fail_authenticated_attempt(&state.artifacts, runner_id, exec_id, &request, now_ms)
            .await?,
    ))
}

fn require_content_type(headers: &HeaderMap, expected: &str) -> Result<(), HttpError> {
    if single_header(headers, "Content-Type")? == expected {
        Ok(())
    } else {
        Err(HttpError::new(ErrorCode::InvalidRequest))
    }
}

fn single_header<'a>(headers: &'a HeaderMap, name: &str) -> Result<&'a str, HttpError> {
    let mut values = headers.get_all(name).iter();
    let value = values
        .next()
        .and_then(|value| value.to_str().ok())
        .ok_or_else(|| HttpError::new(ErrorCode::InvalidRequest))?;
    if values.next().is_some() {
        return Err(HttpError::new(ErrorCode::InvalidRequest));
    }
    Ok(value)
}

fn parse_offset(value: &str) -> Result<u64, HttpError> {
    if value.is_empty()
        || value.len() > 1 && value.starts_with('0')
        || !value.bytes().all(|byte| byte.is_ascii_digit())
    {
        return Err(HttpError::new(ErrorCode::InvalidRequest));
    }
    value
        .parse()
        .map_err(|_| HttpError::new(ErrorCode::InvalidRequest))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn offset_is_canonical_decimal() {
        assert_eq!(parse_offset("0").ok(), Some(0));
        assert_eq!(parse_offset("42").ok(), Some(42));
        for invalid in ["", "00", "01", "+1", "-1", " 1", "18446744073709551616"] {
            assert!(parse_offset(invalid).is_err());
        }
    }
}
