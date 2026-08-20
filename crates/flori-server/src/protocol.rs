use axum::{
    Json,
    extract::{FromRequest, FromRequestParts, Request},
    http::{header::AUTHORIZATION, request::Parts},
    middleware::Next,
    response::{IntoResponse, Response},
};
use flori_core::ErrorCode;
use serde::de::DeserializeOwned;

use crate::error::HttpError;

pub(crate) async fn require_v1(request: Request, next: Next) -> Response {
    let mut versions = request.headers().get_all("X-Flori-Protocol").iter();
    if versions.next().is_none_or(|value| value.as_bytes() != b"1") || versions.next().is_some() {
        return HttpError::new(ErrorCode::ProtocolMismatch).into_response();
    }
    next.run(request).await
}

pub(crate) struct BearerToken(String);

impl BearerToken {
    pub(crate) fn expose(&self) -> &str {
        &self.0
    }
}

impl<S> FromRequestParts<S> for BearerToken
where
    S: Send + Sync,
{
    type Rejection = HttpError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        let mut headers = parts.headers.get_all(AUTHORIZATION).iter();
        let value = headers
            .next()
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.strip_prefix("Bearer "))
            .filter(|token| valid_token(token))
            .ok_or_else(HttpError::unauthorized)?;
        if headers.next().is_some() {
            return Err(HttpError::unauthorized());
        }
        Ok(Self(value.to_owned()))
    }
}

pub(crate) struct StrictJson<T>(pub(crate) T);

impl<T, S> FromRequest<S> for StrictJson<T>
where
    T: DeserializeOwned,
    S: Send + Sync,
{
    type Rejection = HttpError;

    async fn from_request(request: Request, state: &S) -> Result<Self, Self::Rejection> {
        Json::<T>::from_request(request, state)
            .await
            .map(|Json(value)| Self(value))
            .map_err(|_| HttpError::new(ErrorCode::InvalidRequest))
    }
}

fn valid_token(token: &str) -> bool {
    !token.is_empty()
        && token.len() <= 512
        && token
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'.' | b'_' | b'~'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bearer_token_rejects_whitespace_and_empty_values() {
        assert!(valid_token("token-._~09AZaz"));
        assert!(!valid_token("token with spaces"));
    }
}
