use axum::{
    Json,
    body::Bytes,
    extract::{FromRequest, FromRequestParts, Path, Request},
    http::{HeaderMap, header::AUTHORIZATION, request::Parts},
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

    pub(crate) fn optional(headers: &HeaderMap) -> Result<Option<Self>, HttpError> {
        let mut values = headers.get_all(AUTHORIZATION).iter();
        let Some(value) = values.next() else {
            return Ok(None);
        };
        if values.next().is_some() {
            return Err(HttpError::unauthorized());
        }
        let value = value.to_str().map_err(|_| HttpError::unauthorized())?;
        let Some(token) = value.strip_prefix("Bearer ") else {
            return Ok(None);
        };
        if !valid_token(token) {
            return Err(HttpError::unauthorized());
        }
        Ok(Some(Self(token.to_owned())))
    }
}

impl<S> FromRequestParts<S> for BearerToken
where
    S: Send + Sync,
{
    type Rejection = HttpError;

    async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
        Self::optional(&parts.headers)?.ok_or_else(HttpError::unauthorized)
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

pub(crate) struct StrictBytes(pub(crate) Bytes);

impl<S> FromRequest<S> for StrictBytes
where
    S: Send + Sync,
{
    type Rejection = HttpError;

    async fn from_request(request: Request, state: &S) -> Result<Self, Self::Rejection> {
        Bytes::from_request(request, state)
            .await
            .map(Self)
            .map_err(|rejection| {
                if rejection.into_response().status() == axum::http::StatusCode::PAYLOAD_TOO_LARGE {
                    HttpError::payload_too_large()
                } else {
                    HttpError::new(ErrorCode::InvalidRequest)
                }
            })
    }
}

pub(crate) struct StrictPath<T>(pub(crate) T);

impl<T, S> FromRequestParts<S> for StrictPath<T>
where
    T: DeserializeOwned + Send,
    S: Send + Sync,
{
    type Rejection = HttpError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        Path::<T>::from_request_parts(parts, state)
            .await
            .map(|Path(value)| Self(value))
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

    #[test]
    fn optional_bearer_leaves_edge_basic_auth_to_the_public_route() {
        let mut headers = HeaderMap::new();
        headers.insert(AUTHORIZATION, "Basic Zmxvcmk6cGFzcw==".parse().unwrap());
        assert!(matches!(BearerToken::optional(&headers), Ok(None)));
        headers.insert(AUTHORIZATION, "Bearer runner-token".parse().unwrap());
        let Ok(Some(token)) = BearerToken::optional(&headers) else {
            panic!("valid bearer token");
        };
        assert_eq!(token.expose(), "runner-token");
        headers.append(AUTHORIZATION, "Bearer duplicate".parse().unwrap());
        assert!(BearerToken::optional(&headers).is_err());
    }
}
