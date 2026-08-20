use axum::{
    Json,
    http::StatusCode,
    response::{IntoResponse, Response},
};
use flori_core::{ErrorBody, ErrorCode, ErrorResponse, RequestId};
use flori_store::StoreError;

pub(crate) struct HttpError {
    status: StatusCode,
    body: ErrorResponse,
}

impl HttpError {
    pub(crate) fn new(code: ErrorCode) -> Self {
        Self {
            status: status(code),
            body: ErrorResponse {
                error: ErrorBody {
                    code,
                    message: wire_name(code),
                    request_id: RequestId::generate(),
                    field: None,
                    retry_after_ms: None,
                },
            },
        }
    }

    pub(crate) fn unauthorized() -> Self {
        let mut error = Self::new(ErrorCode::InvalidRequest);
        error.status = StatusCode::UNAUTHORIZED;
        error.body.error.message = "missing or invalid bearer token".to_owned();
        error
    }

    pub(crate) fn method_not_allowed() -> Self {
        let mut error = Self::new(ErrorCode::InvalidRequest);
        error.status = StatusCode::METHOD_NOT_ALLOWED;
        error.body.error.message = "HTTP method is not allowed".to_owned();
        error
    }

    pub(crate) fn payload_too_large() -> Self {
        let mut error = Self::new(ErrorCode::ArtifactTooLarge);
        error.status = StatusCode::PAYLOAD_TOO_LARGE;
        error
    }
}

impl IntoResponse for HttpError {
    fn into_response(self) -> Response {
        (self.status, Json(self.body)).into_response()
    }
}

impl From<StoreError> for HttpError {
    fn from(error: StoreError) -> Self {
        Self::new(error.code())
    }
}

const fn status(code: ErrorCode) -> StatusCode {
    match code {
        ErrorCode::NotFound => StatusCode::NOT_FOUND,
        ErrorCode::Conflict
        | ErrorCode::IdempotencyConflict
        | ErrorCode::SourceBusy
        | ErrorCode::RerunBoundaryInvalid
        | ErrorCode::StaleAttempt
        | ErrorCode::LogSequenceGap
        | ErrorCode::LogSequenceConflict
        | ErrorCode::UsageConflict => StatusCode::CONFLICT,
        ErrorCode::UpstreamRateLimited => StatusCode::TOO_MANY_REQUESTS,
        ErrorCode::RunnerUnavailable
        | ErrorCode::ToolTemporarilyUnavailable
        | ErrorCode::StorageUnavailable => StatusCode::SERVICE_UNAVAILABLE,
        ErrorCode::CorruptState | ErrorCode::SchemaMismatch | ErrorCode::Internal => {
            StatusCode::INTERNAL_SERVER_ERROR
        }
        ErrorCode::InvalidRequest
        | ErrorCode::ProtocolMismatch
        | ErrorCode::UnsupportedSource
        | ErrorCode::UnsupportedScannedPdf
        | ErrorCode::PipelineInvalid
        | ErrorCode::PipelineCycle
        | ErrorCode::RunnerDisabled
        | ErrorCode::CapabilityMismatch
        | ErrorCode::LeaseExpired
        | ErrorCode::TaskCanceled
        | ErrorCode::AttemptTimeout
        | ErrorCode::RunnerLost
        | ErrorCode::NetworkTemporary
        | ErrorCode::ExecutorFailed
        | ErrorCode::ArtifactUndeclared
        | ErrorCode::ArtifactInvalidPath
        | ErrorCode::ArtifactTooLarge
        | ErrorCode::DigestMismatch
        | ErrorCode::EvidenceInvalid
        | ErrorCode::CredentialUnavailable
        | ErrorCode::EventCursorExpired => StatusCode::BAD_REQUEST,
    }
}

fn wire_name(code: ErrorCode) -> String {
    serde_json::to_string(&code)
        .expect("serializing a closed enum cannot fail")
        .trim_matches('"')
        .to_owned()
}
