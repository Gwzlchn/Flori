use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

macro_rules! string_enums {
    ($($name:ident { $($variant:ident $(=> $wire:literal)?),+ $(,)? })+) => {
        $(
            #[derive(Clone, Copy, Debug, Deserialize, Eq, Hash, PartialEq, Serialize, ToSchema)]
            #[serde(rename_all = "snake_case")]
            pub enum $name {
                $($(#[serde(rename = $wire)])? $variant),+
            }
        )+
    };
}

#[rustfmt::skip]
string_enums! {
    SourceKind { Arxiv, PdfUrl, PdfUpload, BilibiliVideo, BilibiliChannel, YoutubeVideo, YoutubeChannel, LocalVideo }
    JobTrigger { Initial, PipelineRerun, TaskRerun, Subscription }
    JobState { Queued, Running, Succeeded, Failed, Canceled }
    TaskState { Pending, Ready, Leased, Succeeded, Failed, Canceled, Skipped }
    AttemptState { Leased, Succeeded, Failed, Expired, Canceled }
    RunnerState { Enabled, Disabled }
    CredentialKind { BilibiliCookie, YoutubeCookie }
    AiTool { QoderCli, CodexCli }
    UsageOrigin { Observed, Estimated, Unavailable }
    ArtifactKind {
        SourceOriginal, DocumentStructure, Figure, TableRegion, Translation, Subtitle, Transcript,
        Keyframe, Danmaku, PartsManifest, SubscriptionManifest, MechanicalNote, SmartNote, Summary,
        Terms, Evidence, TaskLog, AiAudit
    }
    UploadOwnerKind { Source, Attempt, Materialize }
    UploadState { Receiving, Verified, Moved }
    ArtifactOrigin { Produced, Materialized }
    ArtifactRetention { Source, Published, FailedAudit }
    AiUsageState { Started, Final }
    JobEventScope { System, Source, Job, Runner }
    CollectionKind { Manual, Subscription }
    GlossaryTermState { Active, Hidden }
    EvidenceLocatorKind { Pdf, Video }
    Executor {
        DocumentAcquire => "document.acquire", DocumentExtract => "document.extract",
        AiDocumentTranslate => "ai.document_translate", AiDocumentNote => "ai.document_note",
        VideoAcquire => "video.acquire", VideoSubscription => "video.subscription",
        VideoTranscribe => "video.transcribe", VideoFrames => "video.frames",
        VideoMechanicalNote => "video.mechanical_note", AiVideoNote => "ai.video_note",
        CoreValidate => "core.validate", CorePublish => "core.publish"
    }
    RunnerTool { PdfExtractor, YtDlp, Yutto, Ffmpeg, Ffprobe, WhisperCpp, FasterWhisper, QoderCli, CodexCli }
    RerunMode { Pipeline, FromTask }
    ArtifactWhen { OnSuccess, Always }
    TaskLogLevel { Debug, Info, Warn, Error }
    SystemHealthStatus { Healthy, Degraded }
    JobEventKind { SourceChanged, JobState, TaskState, ArtifactCommitted, LogCursor, RunnerChanged, SystemHealth }
    ErrorCode {
        InvalidRequest, ProtocolMismatch, NotFound, Conflict, IdempotencyConflict, SourceBusy,
        UnsupportedSource, RerunBoundaryInvalid, UnsupportedScannedPdf, PipelineInvalid,
        PipelineCycle, RunnerUnavailable, RunnerDisabled, CapabilityMismatch, LeaseExpired,
        StaleAttempt, TaskCanceled, AttemptTimeout, RunnerLost, NetworkTemporary,
        UpstreamRateLimited, ToolTemporarilyUnavailable, ExecutorFailed, ArtifactUndeclared,
        ArtifactInvalidPath, ArtifactTooLarge, DigestMismatch, LogSequenceGap,
        LogSequenceConflict, UsageConflict, EvidenceInvalid, CredentialUnavailable,
        StorageUnavailable, EventCursorExpired, CorruptState, SchemaMismatch, Internal
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rejects_unknown_enum_values() {
        let error = serde_json::from_str::<JobState>("\"recovering\"")
            .expect_err("unknown state must be rejected");

        assert!(error.to_string().contains("unknown variant"));
    }

    #[test]
    fn executor_uses_frozen_wire_name() {
        assert_eq!(
            serde_json::to_string(&Executor::AiVideoNote).expect("serialize executor"),
            "\"ai.video_note\""
        );
    }
}
