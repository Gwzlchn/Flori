use flori_core::{ErrorCode, Executor, JobTrigger, SourceKind};

pub(super) const fn transient(code: ErrorCode) -> bool {
    matches!(
        code,
        ErrorCode::AttemptTimeout
            | ErrorCode::RunnerLost
            | ErrorCode::NetworkTemporary
            | ErrorCode::UpstreamRateLimited
            | ErrorCode::ToolTemporarilyUnavailable
    )
}

pub(super) const fn error_code(code: ErrorCode) -> &'static str {
    match code {
        ErrorCode::InvalidRequest => "invalid_request",
        ErrorCode::ProtocolMismatch => "protocol_mismatch",
        ErrorCode::NotFound => "not_found",
        ErrorCode::Conflict => "conflict",
        ErrorCode::IdempotencyConflict => "idempotency_conflict",
        ErrorCode::SourceBusy => "source_busy",
        ErrorCode::UnsupportedSource => "unsupported_source",
        ErrorCode::RerunBoundaryInvalid => "rerun_boundary_invalid",
        ErrorCode::UnsupportedScannedPdf => "unsupported_scanned_pdf",
        ErrorCode::PipelineInvalid => "pipeline_invalid",
        ErrorCode::PipelineCycle => "pipeline_cycle",
        ErrorCode::RunnerUnavailable => "runner_unavailable",
        ErrorCode::RunnerDisabled => "runner_disabled",
        ErrorCode::CapabilityMismatch => "capability_mismatch",
        ErrorCode::LeaseExpired => "lease_expired",
        ErrorCode::StaleAttempt => "stale_attempt",
        ErrorCode::TaskCanceled => "task_canceled",
        ErrorCode::AttemptTimeout => "attempt_timeout",
        ErrorCode::RunnerLost => "runner_lost",
        ErrorCode::NetworkTemporary => "network_temporary",
        ErrorCode::UpstreamRateLimited => "upstream_rate_limited",
        ErrorCode::ToolTemporarilyUnavailable => "tool_temporarily_unavailable",
        ErrorCode::ExecutorFailed => "executor_failed",
        ErrorCode::ArtifactUndeclared => "artifact_undeclared",
        ErrorCode::ArtifactInvalidPath => "artifact_invalid_path",
        ErrorCode::ArtifactTooLarge => "artifact_too_large",
        ErrorCode::DigestMismatch => "digest_mismatch",
        ErrorCode::LogSequenceGap => "log_sequence_gap",
        ErrorCode::LogSequenceConflict => "log_sequence_conflict",
        ErrorCode::UsageConflict => "usage_conflict",
        ErrorCode::EvidenceInvalid => "evidence_invalid",
        ErrorCode::CredentialUnavailable => "credential_unavailable",
        ErrorCode::StorageUnavailable => "storage_unavailable",
        ErrorCode::EventCursorExpired => "event_cursor_expired",
        ErrorCode::CorruptState => "corrupt_state",
        ErrorCode::SchemaMismatch => "schema_mismatch",
        ErrorCode::Internal => "internal",
    }
}

pub(super) const fn executor(value: Executor) -> &'static str {
    match value {
        Executor::DocumentAcquire => "document.acquire",
        Executor::DocumentExtract => "document.extract",
        Executor::AiDocumentTranslate => "ai.document_translate",
        Executor::AiDocumentNote => "ai.document_note",
        Executor::VideoAcquire => "video.acquire",
        Executor::VideoSubscription => "video.subscription",
        Executor::VideoTranscribe => "video.transcribe",
        Executor::VideoFrames => "video.frames",
        Executor::VideoMechanicalNote => "video.mechanical_note",
        Executor::AiVideoNote => "ai.video_note",
        Executor::CoreValidate => "core.validate",
        Executor::CorePublish => "core.publish",
    }
}

pub(super) const fn job_trigger(value: JobTrigger) -> &'static str {
    match value {
        JobTrigger::Initial => "initial",
        JobTrigger::PipelineRerun => "pipeline_rerun",
        JobTrigger::TaskRerun => "task_rerun",
        JobTrigger::Subscription => "subscription",
    }
}

pub(crate) const fn source_kind(value: SourceKind) -> &'static str {
    match value {
        SourceKind::Arxiv => "arxiv",
        SourceKind::PdfUrl => "pdf_url",
        SourceKind::PdfUpload => "pdf_upload",
        SourceKind::BilibiliVideo => "bilibili_video",
        SourceKind::BilibiliChannel => "bilibili_channel",
        SourceKind::YoutubeVideo => "youtube_video",
        SourceKind::YoutubeChannel => "youtube_channel",
        SourceKind::LocalVideo => "local_video",
    }
}
