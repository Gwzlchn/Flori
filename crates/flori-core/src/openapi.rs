use std::collections::BTreeMap;

use utoipa::{OpenApi, PartialSchema, ToSchema};

use crate::{
    AiAudit, AiAuditSchema, AiModelCapability, AiResultEnvelope, AiResultSchema, AiRunnerSelection,
    AiTool, AiUsageId, AiUsageState, AiUsageView, ArtifactCommittedEvent, ArtifactDeclaration,
    ArtifactId, ArtifactKind, ArtifactManifest, ArtifactManifestEntry, ArtifactManifestSchema,
    ArtifactOrigin, ArtifactRetention, ArtifactView, ArtifactWhen, AttemptAck, AttemptId,
    AttemptState, AttemptView, CollectionId, CollectionKind, CollectionView,
    CompleteAttemptRequest, ConceptOccurrenceId, CreateJobRequest, CreateRemoteSource,
    CreateRunnerSlot, CreateRunnerSlotResponse, CreateUploadSource, CreatedJob, CreatedSource,
    CredentialId, CredentialKind, DocumentFigure, DocumentMetadataView, DocumentPage,
    DocumentRepresentationView, DocumentSection, DocumentStructure, DocumentStructureSchema,
    DocumentTable, DocumentTextBlock, DomainId, DomainView, ErrorBody, ErrorCode, ErrorResponse,
    EvidenceEntry, EvidenceId, EvidenceLocator, EvidenceLocatorKind, EvidenceManifest,
    EvidenceManifestSchema, EvidenceView, Executor, FailAttemptRequest, GlossaryTermId,
    GlossaryTermState, HtmlPdfCrosswalk, HtmlPdfCrosswalkStatus, HtmlVisualProjection,
    HtmlVisualStatus, JobEvent, JobEventKind, JobEventPayload, JobEventScope, JobId, JobInputs,
    JobState, JobStateEvent, JobTrigger, JobView, LogCursor, LogFrame, PartsManifest,
    PartsManifestSchema, PdfRect, PdfSetupView, PendingSourceCommit, PipelineId,
    PipelineRevisionId, PromptSnapshotId, QrSessionId, RegisterRunnerRequest,
    RegisterRunnerResponse, RenewLeaseResponse, RequestId, RerunJobRequest, RerunMode,
    ResolvedArtifact, ResolvedProfile, ResolvedPrompt, ResolvedSource, ResolvedSourceInput,
    ResolvedTaskInputs, RunnerChangedEvent, RunnerId, RunnerState, RunnerTool,
    RunnerToolCapability, RunnerView, ScholarlyFile, ScholarlyHtmlSnapshot,
    ScholarlyHtmlSnapshotSchema, ScholarlyProvider, SearchChunkId, SearchHit, SecretCredential,
    SecretInputs, Sha256Digest, SourceChangedEvent, SourceId, SourceInputId, SourceKind,
    SourceView, StartUploadRequest, StartUploadResponse, SubscriptionItem, SubscriptionManifest,
    SubscriptionManifestSchema, SystemHealthEvent, SystemHealthStatus, SystemView, TaskClaim,
    TaskId, TaskLogEvent, TaskLogLevel, TaskLogLine, TaskState, TaskStateEvent, TaskView,
    TermEntry, TermsManifest, TermsManifestSchema, TranscriptCue, TranscriptManifest,
    TranscriptSchema, UploadCursor, UploadId, UploadOwnerKind, UploadState, UsageAck, UsageOrigin,
    UsageUpdate, VerifyUploadRequest, VerifyUploadResponse, VideoKeyframe, VideoPart,
};

#[derive(OpenApi)]
#[openapi(
    paths(
        crate::api_paths::system,
        crate::api_paths::events,
        crate::api_paths::job_events,
        crate::api_paths::pdf_setup,
        crate::api_paths::upload_source,
        crate::api_paths::domains,
        crate::api_paths::collections,
        crate::api_paths::sources,
        crate::api_paths::create_source,
        crate::api_paths::source_detail,
        crate::api_paths::source_document,
        crate::api_paths::source_document_content,
        crate::api_paths::delete_source,
        crate::api_paths::create_job,
        crate::api_paths::job_detail,
        crate::api_paths::cancel_job,
        crate::api_paths::rerun_job,
        crate::api_paths::runners,
        crate::api_paths::artifact_detail,
        crate::api_paths::artifact_content,
        crate::api_paths::search,
        crate::api_paths::evidence,
    ),
    components(schemas(
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
        ScholarlyHtmlSnapshotSchema,
        ScholarlyProvider,
        ScholarlyFile,
        ScholarlyHtmlSnapshot,
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
        PdfSetupView,
        DomainView,
        CollectionView,
        SourceView,
        JobView,
        TaskView,
        AttemptView,
        AiUsageView,
        ArtifactView,
        SearchHit,
        EvidenceView,
        HtmlPdfCrosswalkStatus,
        HtmlPdfCrosswalk,
        HtmlVisualStatus,
        HtmlVisualProjection,
        DocumentRepresentationView,
        DocumentMetadataView,
        RunnerView,
        SourceChangedEvent,
        JobStateEvent,
        TaskStateEvent,
        ArtifactCommittedEvent,
        RunnerChangedEvent,
        SystemHealthEvent,
        JobEventPayload,
        JobEvent,
        SystemView,
    ))
)]
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
    fn exports_parseable_ui_contract() {
        let json = openapi_json().expect("serialize OpenAPI");
        let document: utoipa::openapi::OpenApi =
            serde_json::from_str(&json).expect("parse OpenAPI");
        let schemas = &document.components.expect("components").schemas;

        assert_eq!(document.info.title, "Flori API");
        assert_eq!(document.info.version, "flori.v1");
        assert_eq!(document.paths.paths.len(), 20);
        assert!(document.paths.paths.contains_key("/api/v1/system"));
        assert!(document.paths.paths.contains_key("/api/v1/events"));
        assert!(
            document
                .paths
                .paths
                .contains_key("/api/v1/jobs/{job_id}/events")
        );
        assert!(document.paths.paths.contains_key("/api/v1/pdf/setup"));
        assert!(document.paths.paths.contains_key("/api/v1/domains"));
        assert!(document.paths.paths.contains_key("/api/v1/collections"));
        let sources = &document.paths.paths["/api/v1/sources"];
        assert!(sources.get.is_some());
        assert!(sources.post.is_some());
        assert!(
            document
                .paths
                .paths
                .contains_key("/api/v1/sources/{source_id}/document")
        );
        assert!(
            document
                .paths
                .paths
                .contains_key("/api/v1/sources/{source_id}/document/content")
        );
        assert!(document.paths.paths.contains_key("/api/v1/jobs/{job_id}"));
        assert!(
            document
                .paths
                .paths
                .contains_key("/api/v1/jobs/{job_id}/cancel")
        );
        assert!(
            document
                .paths
                .paths
                .contains_key("/api/v1/jobs/{job_id}/rerun")
        );
        assert!(document.paths.paths.contains_key("/api/v1/runners"));
        assert!(schemas.contains_key("SourceId"));
        assert!(schemas.contains_key("SourceKind"));
        assert!(schemas.contains_key("Executor"));
        assert!(schemas.contains_key("ErrorCode"));
        assert!(schemas.contains_key("ArtifactDeclaration"));
        assert!(schemas.contains_key("ArtifactManifestSchema"));
        assert!(schemas.contains_key("ArtifactManifest"));
        assert!(schemas.contains_key("ArtifactManifestEntry"));
        assert!(schemas.contains_key("SourceView"));
        assert!(schemas.contains_key("DomainView"));
        assert!(schemas.contains_key("CollectionView"));
        assert!(schemas.contains_key("JobView"));
        assert!(schemas.contains_key("TaskView"));
        assert!(schemas.contains_key("AttemptView"));
        assert!(schemas.contains_key("RunnerView"));
        assert!(schemas.contains_key("EvidenceLocator"));
        assert!(schemas.contains_key("DocumentRepresentationView"));
        assert!(schemas.contains_key("HtmlVisualProjection"));
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
        assert_eq!(json.matches("Last-Event-ID").count(), 2);
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
