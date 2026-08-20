use std::fmt;
use std::time::Duration;

use flori_core::{
    ErrorBody, ErrorCode, ErrorResponse, PROTOCOL_VERSION, RegisterRunnerRequest,
    RegisterRunnerResponse, TaskClaim,
};
use reqwest::{Method, RequestBuilder, Response, StatusCode, Url};
use serde::{Serialize, de::DeserializeOwned};

const PROTOCOL_HEADER: &str = "X-Flori-Protocol";

pub struct RunnerClient {
    http: reqwest::Client,
    base_url: Url,
    bearer_token: String,
}

#[derive(Debug)]
pub struct ClientError {
    code: ErrorCode,
    remote: Option<ErrorBody>,
}

impl ClientError {
    pub(crate) const fn local(code: ErrorCode) -> Self {
        Self { code, remote: None }
    }

    #[must_use]
    pub const fn code(&self) -> ErrorCode {
        self.code
    }

    #[must_use]
    pub const fn remote(&self) -> Option<&ErrorBody> {
        self.remote.as_ref()
    }
}

impl fmt::Display for ClientError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match &self.remote {
            Some(remote) => write!(formatter, "Runner request rejected: {:?}", remote.code),
            None => write!(formatter, "Runner request failed: {:?}", self.code),
        }
    }
}

impl std::error::Error for ClientError {}

impl RunnerClient {
    pub fn new(base_url: &str, bearer_token: impl Into<String>) -> Result<Self, ClientError> {
        let mut base_url =
            Url::parse(base_url).map_err(|_| ClientError::local(ErrorCode::InvalidRequest))?;
        if !matches!(base_url.scheme(), "http" | "https")
            || base_url.cannot_be_a_base()
            || base_url.host().is_none()
            || !base_url.username().is_empty()
            || base_url.password().is_some()
            || base_url.query().is_some()
            || base_url.fragment().is_some()
        {
            return Err(ClientError::local(ErrorCode::InvalidRequest));
        }
        if !base_url.path().ends_with('/') {
            let path = format!("{}/", base_url.path());
            base_url.set_path(&path);
        }
        let bearer_token = bearer_token.into();
        if bearer_token.is_empty() {
            return Err(ClientError::local(ErrorCode::InvalidRequest));
        }
        let http = reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(45))
            .build()
            .map_err(|_| ClientError::local(ErrorCode::Internal))?;
        Ok(Self {
            http,
            base_url,
            bearer_token,
        })
    }

    pub async fn register(
        base_url: &str,
        registration_token: impl Into<String>,
        request: &RegisterRunnerRequest,
    ) -> Result<RegisterRunnerResponse, ClientError> {
        let client = Self::new(base_url, registration_token)?;
        client
            .send_json(
                client
                    .request(Method::POST, "runner/v1/register")?
                    .json(request),
            )
            .await
    }

    pub async fn poll(&self) -> Result<Option<TaskClaim>, ClientError> {
        let response = self
            .send(self.request(Method::POST, "runner/v1/poll")?)
            .await?;
        if response.status() == StatusCode::NO_CONTENT {
            return Ok(None);
        }
        self.decode(response).await.map(Some)
    }

    pub(crate) fn request(
        &self,
        method: Method,
        path: &str,
    ) -> Result<RequestBuilder, ClientError> {
        let url = self
            .base_url
            .join(path)
            .map_err(|_| ClientError::local(ErrorCode::InvalidRequest))?;
        Ok(self
            .http
            .request(method, url)
            .header(PROTOCOL_HEADER, PROTOCOL_VERSION)
            .bearer_auth(&self.bearer_token))
    }

    pub(crate) async fn send_json<T>(&self, request: RequestBuilder) -> Result<T, ClientError>
    where
        T: DeserializeOwned,
    {
        let response = self.send(request).await?;
        self.decode(response).await
    }

    pub(crate) async fn send_json_body<I, O>(
        &self,
        request: RequestBuilder,
        body: &I,
    ) -> Result<O, ClientError>
    where
        I: Serialize + ?Sized,
        O: DeserializeOwned,
    {
        self.send_json(request.json(body)).await
    }

    async fn send(&self, request: RequestBuilder) -> Result<Response, ClientError> {
        let response = request
            .send()
            .await
            .map_err(|_| ClientError::local(ErrorCode::NetworkTemporary))?;
        if response.status().is_success() {
            return Ok(response);
        }
        let remote = response
            .json::<ErrorResponse>()
            .await
            .map_err(|_| ClientError::local(ErrorCode::CorruptState))?
            .error;
        Err(ClientError {
            code: remote.code,
            remote: Some(remote),
        })
    }

    async fn decode<T: DeserializeOwned>(&self, response: Response) -> Result<T, ClientError> {
        response
            .json()
            .await
            .map_err(|_| ClientError::local(ErrorCode::CorruptState))
    }
}
