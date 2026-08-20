use utoipa::OpenApi;

use crate::{
    AiTool, AiUsageId, AiUsageState, ArtifactDeclaration, ArtifactId, ArtifactKind,
    ArtifactManifest, ArtifactManifestEntry, ArtifactManifestSchema, ArtifactOrigin,
    ArtifactRetention, ArtifactWhen, AttemptId, AttemptState, CollectionId, CollectionKind,
    ConceptOccurrenceId, CredentialId, CredentialKind, DomainId, ErrorCode, EvidenceId,
    EvidenceLocatorKind, Executor, GlossaryTermId, GlossaryTermState, JobEventKind, JobEventScope,
    JobId, JobState, JobTrigger, PipelineId, PipelineRevisionId, PromptSnapshotId, QrSessionId,
    RequestId, RerunMode, RunnerId, RunnerState, RunnerTool, SearchChunkId, Sha256Digest, SourceId,
    SourceInputId, SourceKind, SystemHealthStatus, TaskId, TaskLogLevel, TaskState, UploadId,
    UploadOwnerKind, UploadState, UsageOrigin,
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
        assert!(
            json.contains("^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        );
        assert!(json.contains("\n  \"openapi\""));
        assert!(json.contains("^[0-9a-f]{64}$"));
    }
}
