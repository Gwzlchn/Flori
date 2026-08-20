use std::collections::BTreeMap;

use utoipa::{OpenApi, PartialSchema, ToSchema};

use crate::{
    AiAudit, AiAuditSchema, AiModelCapability, AiResultEnvelope, AiResultSchema, AiRunnerSelection,
    AiTool, AiUsageId, AiUsageState, ArtifactDeclaration, ArtifactId, ArtifactKind,
    ArtifactManifest, ArtifactManifestEntry, ArtifactManifestSchema, ArtifactOrigin,
    ArtifactRetention, ArtifactView, ArtifactWhen, AttemptAck, AttemptId, AttemptState,
    CollectionId, CollectionKind, CompleteAttemptRequest, ConceptOccurrenceId, CreateJobRequest,
    CreateRemoteSource, CreateRunnerSlot, CreateRunnerSlotResponse, CreateUploadSource, CreatedJob,
    CreatedSource, CredentialId, CredentialKind, DocumentFigure, DocumentPage, DocumentSection,
    DocumentStructure, DocumentStructureSchema, DocumentTable, DocumentTextBlock, DomainId,
    ErrorBody, ErrorCode, ErrorResponse, EvidenceEntry, EvidenceId, EvidenceLocator,
    EvidenceLocatorKind, EvidenceManifest, EvidenceManifestSchema, EvidenceView, Executor,
    FailAttemptRequest, GlossaryTermId, GlossaryTermState, JobEventKind, JobEventScope, JobId,
    JobInputs, JobState, JobTrigger, LogCursor, LogFrame, PartsManifest, PartsManifestSchema,
    PdfRect, PendingSourceCommit, PipelineId, PipelineRevisionId, PromptSnapshotId, QrSessionId,
    RegisterRunnerRequest, RegisterRunnerResponse, RenewLeaseResponse, RequestId, RerunJobRequest,
    RerunMode, ResolvedArtifact, ResolvedProfile, ResolvedPrompt, ResolvedSource,
    ResolvedSourceInput, ResolvedTaskInputs, RunnerId, RunnerState, RunnerTool,
    RunnerToolCapability, SearchChunkId, SearchHit, SecretCredential, SecretInputs, Sha256Digest,
    SourceId, SourceInputId, SourceKind, StartUploadRequest, StartUploadResponse, SubscriptionItem,
    SubscriptionManifest, SubscriptionManifestSchema, SystemHealthStatus, TaskClaim, TaskId,
    TaskLogEvent, TaskLogLevel, TaskLogLine, TaskState, TermEntry, TermsManifest,
    TermsManifestSchema, TranscriptCue, TranscriptManifest, TranscriptSchema, UploadCursor,
    UploadId, UploadOwnerKind, UploadState, UsageAck, UsageOrigin, UsageUpdate,
    VerifyUploadRequest, VerifyUploadResponse, VideoKeyframe, VideoPart,
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
    PdfRect,
    VideoKeyframe,
    EvidenceLocator,
    EvidenceEntry,
    EvidenceManifestSchema,
    EvidenceManifest,
    DocumentStructureSchema,
    DocumentPage,
    DocumentSection,
    DocumentTextBlock,
    DocumentFigure,
    DocumentTable,
    DocumentStructure,
    TranscriptSchema,
    TranscriptCue,
    TranscriptManifest,
    PartsManifestSchema,
    VideoPart,
    PartsManifest,
    SubscriptionManifestSchema,
    SubscriptionItem,
    SubscriptionManifest,
    RunnerToolCapability,
    AiModelCapability,
    ResolvedArtifact,
    ResolvedSourceInput,
    ResolvedSource,
    ResolvedPrompt,
    ResolvedProfile,
    ResolvedTaskInputs,
    TermsManifestSchema,
    TermEntry,
    TermsManifest,
    AiAuditSchema,
    AiAudit,
    AiResultSchema,
    AiResultEnvelope,
    SecretCredential,
    SecretInputs,
    TaskClaim,
    RegisterRunnerRequest,
    RegisterRunnerResponse,
    CreateRunnerSlot,
    CreateRunnerSlotResponse,
    RenewLeaseResponse,
    LogFrame,
    TaskLogLine,
    LogCursor,
    TaskLogEvent,
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
    CreateUploadSource,
    PendingSourceCommit,
    ArtifactView,
    SearchHit,
    EvidenceView,
)))]
struct ApiDoc;

pub fn openapi_json() -> Result<String, serde_json::Error> {
    let mut document = ApiDoc::openapi();
    document.info.title = "Flori API".to_owned();
    document.info.version = crate::CONTRACT_REVISION.to_owned();
    serde_json::to_string_pretty(&document)
}

pub fn ai_result_schema_json() -> Result<String, serde_json::Error> {
    let root = serde_json::to_string(&AiResultEnvelope::schema())?;
    let mut dependencies = Vec::new();
    AiResultEnvelope::schemas(&mut dependencies);
    let definitions = serde_json::to_string(
        &dependencies
            .into_iter()
            .collect::<BTreeMap<String, utoipa::openapi::RefOr<utoipa::openapi::Schema>>>(),
    )?;
    Ok(format!(
        r#"{{"$schema":"https://json-schema.org/draft/2020-12/schema","allOf":[{}],"$defs":{}}}"#,
        rewrite_schema_refs(root),
        rewrite_schema_refs(definitions),
    ))
}

fn rewrite_schema_refs(json: String) -> String {
    json.replace("#/components/schemas/", "#/$defs/")
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
        assert!(schemas.contains_key("EvidenceLocator"));
        assert!(schemas.contains_key("Sha256Digest"));
        assert!(schemas.contains_key("TaskClaim"));
        assert!(schemas.contains_key("CompleteAttemptRequest"));
        assert!(
            json.contains("^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        );
        assert!(json.contains("\n  \"openapi\""));
        assert!(json.contains("^[0-9a-f]{64}$"));
        assert!(json.contains(r#""core.validate""#));
        assert!(!json.contains(r#""core_validate""#));
        let locator =
            serde_json::to_string(&schemas["EvidenceLocator"]).expect("serialize locator schema");
        assert_eq!(
            locator.matches(r#""additionalProperties":false"#).count(),
            6
        );
    }

    #[test]
    fn exports_ai_result_schema_from_the_same_rust_types() {
        let schema = ai_result_schema_json().expect("AI result schema");
        assert!(schema.contains(r#""$schema":"https://json-schema.org/draft/2020-12/schema""#));
        assert!(schema.contains(r#""TermsManifest":{"#));
        assert!(!schema.contains("#/components/schemas/"));
    }
}
