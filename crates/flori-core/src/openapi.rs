use utoipa::OpenApi;

use crate::{
    AiModelCapability, AiRunnerSelection, AiTool, AiUsageId, AiUsageState, ArtifactDeclaration,
    ArtifactId, ArtifactKind, ArtifactManifest, ArtifactManifestEntry, ArtifactManifestSchema,
    ArtifactOrigin, ArtifactRetention, ArtifactWhen, AttemptAck, AttemptId, AttemptState,
    CollectionId, CollectionKind, CompleteAttemptRequest, ConceptOccurrenceId, CreateJobRequest,
    CreateRemoteSource, CreatedJob, CreatedSource, CredentialId, CredentialKind, DomainId,
    ErrorBody, ErrorCode, ErrorResponse, EvidenceId, EvidenceLocatorKind, Executor,
    FailAttemptRequest, GlossaryTermId, GlossaryTermState, JobEventKind, JobEventScope, JobId,
    JobInputs, JobState, JobTrigger, LogCursor, LogFrame, PipelineId, PipelineRevisionId,
    PromptSnapshotId, QrSessionId, RegisterRunnerRequest, RegisterRunnerResponse,
    RenewLeaseResponse, RequestId, RerunJobRequest, RerunMode, ResolvedArtifact, ResolvedProfile,
    ResolvedPrompt, ResolvedSource, ResolvedTaskInputs, RunnerId, RunnerState, RunnerTool,
    RunnerToolCapability, SearchChunkId, SecretCredential, SecretInputs, Sha256Digest, SourceId,
    SourceInputId, SourceKind, StartUploadRequest, StartUploadResponse, SystemHealthStatus,
    TaskClaim, TaskId, TaskLogLevel, TaskState, UploadCursor, UploadId, UploadOwnerKind,
    UploadState, UsageAck, UsageOrigin, UsageUpdate, VerifyUploadRequest, VerifyUploadResponse,
};

#[derive(OpenApi)]
#[openapi(components(schemas(
    PipelineId,
    PipelineRevisionId,
    SourceId,
    SourceInputId,
    JobId,
    TaskId,
    AttemptId,
    ArtifactId,
    RunnerId,
    PromptSnapshotId,
    UploadId,
    CredentialId,
    AiUsageId,
    DomainId,
    CollectionId,
    GlossaryTermId,
    ConceptOccurrenceId,
    EvidenceId,
    SearchChunkId,
    QrSessionId,
    RequestId,
    SourceKind,
    JobTrigger,
    JobState,
    TaskState,
    AttemptState,
    RunnerState,
    CredentialKind,
    AiTool,
    UsageOrigin,
    ArtifactKind,
    UploadOwnerKind,
    UploadState,
    ArtifactOrigin,
    ArtifactRetention,
    AiUsageState,
    JobEventScope,
    CollectionKind,
    GlossaryTermState,
    EvidenceLocatorKind,
    Executor,
    RunnerTool,
    RerunMode,
    ArtifactWhen,
    TaskLogLevel,
    SystemHealthStatus,
    JobEventKind,
    ErrorCode,
    ArtifactDeclaration,
    ArtifactManifestSchema,
    ArtifactManifest,
    ArtifactManifestEntry,
    Sha256Digest,
    RunnerToolCapability,
    AiModelCapability,
    ResolvedArtifact,
    ResolvedSource,
    ResolvedPrompt,
    ResolvedProfile,
    ResolvedTaskInputs,
    SecretCredential,
    SecretInputs,
    TaskClaim,
    RegisterRunnerRequest,
    RegisterRunnerResponse,
    RenewLeaseResponse,
    LogFrame,
    LogCursor,
    UsageUpdate,
    UsageAck,
    StartUploadRequest,
    StartUploadResponse,
    UploadCursor,
    VerifyUploadRequest,
    VerifyUploadResponse,
    CompleteAttemptRequest,
    FailAttemptRequest,
    AttemptAck,
    ErrorResponse,
    ErrorBody,
    JobInputs,
    CreateRemoteSource,
    CreateJobRequest,
    AiRunnerSelection,
    RerunJobRequest,
    CreatedSource,
    CreatedJob,
)))]
struct ApiDoc;

pub fn openapi_json() -> Result<String, serde_json::Error> {
    let mut document = ApiDoc::openapi();
    document.info.title = "Flori API".to_owned();
    document.info.version = crate::CONTRACT_REVISION.to_owned();
    serde_json::to_string_pretty(&document)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn exports_parseable_components_without_paths() {
        let json = openapi_json().expect("serialize OpenAPI");
        let document: utoipa::openapi::OpenApi =
            serde_json::from_str(&json).expect("parse OpenAPI");
        let schemas = &document.components.expect("components").schemas;

        assert_eq!(document.info.title, "Flori API");
        assert_eq!(document.info.version, "flori.v1");
        assert!(document.paths.paths.is_empty());
        assert!(schemas.contains_key("SourceId"));
        assert!(schemas.contains_key("SourceKind"));
        assert!(schemas.contains_key("Executor"));
        assert!(schemas.contains_key("ErrorCode"));
        assert!(schemas.contains_key("ArtifactDeclaration"));
        assert!(schemas.contains_key("ArtifactManifestSchema"));
        assert!(schemas.contains_key("ArtifactManifest"));
        assert!(schemas.contains_key("ArtifactManifestEntry"));
        assert!(schemas.contains_key("Sha256Digest"));
        assert!(schemas.contains_key("TaskClaim"));
        assert!(schemas.contains_key("CompleteAttemptRequest"));
        assert!(
            json.contains("^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        );
        assert!(json.contains("\n  \"openapi\""));
        assert!(json.contains("^[0-9a-f]{64}$"));
    }
}
