use std::{fs, path::Path};

use super::{EXPORT, command, repository_files};

const POLICY_FILES: &str = ".sqlx/query-129b64010ee7cddac8b3c36e19e4a31971abab8fb27ce4f580c592c93ded4f66.json\n.sqlx/query-4f1c1f5e91d92b41bfd1c11a7807c9537aa69b42cea2c5d59487df7d55a96da8.json\n.sqlx/query-52200a51171bab442d1b755c7f1ee306d8f1bf1915b8bed0a02b6b7db7d72638.json\n.sqlx/query-7ba858139d4c260f2daf33a15450464f66e1fe77cf4418acd350ad46a502534d.json\n.sqlx/query-beaee9edbaadcefc2febc643ffffad4aa8d7e7cfd5445018f675c00331f58e49.json\n.sqlx/query-ce416528782adb45ebaaa34e448b0588615ae6afa683e8bc7285af1b1504718d.json\n.sqlx/query-e019b4d2e713d2806484a4f5f45a44f1267f4ebe6c73131eb8892f8343354d3a.json\n.sqlx/query-f4648a6d7a1cc285b1d4c5ff83bcef075dee50918656c76b22623029f3600d2d.json\n.sqlx/query-feec2dcc6fd9d9f3aa8f79a59c4ab6c6f0d74fcb1dce380462d54fcc4711e88d.json\nCargo.toml\ncompose.dev.yml\ncompose.prod.yml\ncompose.test.yml\ncrates/flori-core/Cargo.toml\ncrates/flori-core/src/api_paths.rs\ncrates/flori-core/src/artifact.rs\ncrates/flori-core/src/document.rs\ncrates/flori-core/src/enums.rs\ncrates/flori-core/src/evidence.rs\ncrates/flori-core/src/ids.rs\ncrates/flori-core/src/job.rs\ncrates/flori-core/src/lib.rs\ncrates/flori-core/src/materialize.rs\ncrates/flori-core/src/openapi.rs\ncrates/flori-core/src/pdf_evidence.rs\ncrates/flori-core/src/runner_claim.rs\ncrates/flori-core/src/runner_protocol.rs\ncrates/flori-core/src/video.rs\ncrates/flori-core/src/video_evidence.rs\ncrates/flori-core/tests/golden_contracts.rs\ncrates/flori-pipeline/Cargo.toml\ncrates/flori-runner/Cargo.toml\ncrates/flori-server/Cargo.toml\ncrates/flori-store/Cargo.toml\ncrates/flori-store/migrations/0001_v1.sql\ndocker/runner.Dockerfile\ndocker/server.Dockerfile\nfrontend/Dockerfile\nxtask/Cargo.toml";
const RUST_FORBIDDEN: &str =
    "serde_json::Value\0serde_yaml_ng::Value\0serde(alias\0serde(untagged\0serde(flatten\0Unknown(";
const CORE_TYPES: &str = "PipelineId\0PipelineRevisionId\0SourceId\0SourceInputId\0JobId\0TaskId\0AttemptId\0ArtifactId\0RunnerId\0PromptSnapshotId\0UploadId\0CredentialId\0AiUsageId\0DomainId\0CollectionId\0GlossaryTermId\0ConceptOccurrenceId\0EvidenceId\0SearchChunkId\0QrSessionId\0RequestId\0SourceKind\0JobTrigger\0JobState\0TaskState\0AttemptState\0RunnerState\0CredentialKind\0AiTool\0UsageOrigin\0ArtifactKind\0UploadOwnerKind\0UploadState\0ArtifactOrigin\0ArtifactRetention\0AiUsageState\0JobEventScope\0CollectionKind\0GlossaryTermState\0EvidenceLocatorKind\0Executor\0RunnerTool\0RerunMode\0ArtifactWhen\0TaskLogLevel\0SystemHealthStatus\0JobEventKind\0ErrorCode\0ArtifactDeclaration\0ArtifactManifestSchema\0ArtifactManifest\0ArtifactManifestEntry\0Sha256Digest\0DocumentStructureSchema\0DocumentPage\0DocumentSection\0DocumentTextBlock\0DocumentFigure\0DocumentTable\0DocumentStructure\0EvidenceManifestSchema\0PdfRect\0EvidenceLocator\0EvidenceEntry\0EvidenceManifest\0VideoKeyframe\0TranscriptSchema\0TranscriptCue\0TranscriptManifest\0PartsManifestSchema\0VideoPart\0PartsManifest\0SubscriptionManifestSchema\0SubscriptionItem\0SubscriptionManifest\0PromptSnapshotPrompt\0PromptSnapshotProfile\0PromptSnapshot\0CompiledTaskSpec\0TaskInputReference\0TaskInputBindings\0JobInputs\0CreateRemoteSource\0CreateJobRequest\0AiRunnerSelection\0RerunJobRequest\0CreatedSource\0CreatedJob\0PendingTaskCommit\0PendingAttemptUpload\0PendingMaterializedArtifact\0PendingMaterializeCommit\0RunnerToolCapability\0AiModelCapability\0RunnerTags\0RunnerTools\0AiModels\0ResolvedArtifact\0ResolvedSourceInput\0ResolvedSource\0ResolvedPrompt\0ResolvedProfile\0ResolvedTaskInputs\0TermsManifestSchema\0TermEntry\0TermsManifest\0AiAuditSchema\0AiAudit\0AiResultSchema\0AiResultEnvelope\0SecretCredential\0SecretInputs\0TaskClaim\0RegisterRunnerRequest\0RegisterRunnerResponse\0CreateRunnerSlot\0CreateRunnerSlotResponse\0RenewLeaseResponse\0LogFrame\0TaskLogLine\0LogCursor\0TaskLogEvent\0UsageUpdate\0UsageAck\0StartUploadRequest\0StartUploadResponse\0UploadCursor\0VerifyUploadRequest\0VerifyUploadResponse\0CompleteAttemptRequest\0FailAttemptRequest\0AttemptAck\0ErrorResponse\0ErrorBody";
const KNOWLEDGE_TYPES: &str =
    "PdfSetupView\0SourceView\0JobView\0TaskView\0AttemptView\0ArtifactView\0SearchHit\0EvidenceView";
const SOURCE_TYPES: &str = "CreateUploadSource\0CreateUploadSourceForm\0PendingSourceCommit";
const TS_FORBIDDEN: &str =
    "as unknown as\0@ts-ignore\0@ts-nocheck\0as any\0: any\0any[]\0fetch(\0XMLHttpRequest\0axios";
const MAX_MODULE_LINES: usize = 300;

pub(super) fn check(root: &Path) -> Result<(), String> {
    command(root, "cargo", EXPORT)?;
    command(root, "sha256sum", "--check --quiet xtask/policy.sha256")?;
    let mut inventory = repository_files(
        root,
        &[
            ".sqlx",
            "*Cargo.toml",
            "compose*.yml",
            "*Dockerfile",
            "crates/flori-core",
            "crates/flori-store/migrations",
            "frontend/.generated",
        ],
    )?;
    inventory.sort();
    let mut expected = POLICY_FILES
        .lines()
        .chain([
            "crates/flori-core/src/knowledge.rs",
            "crates/flori-core/src/source.rs",
        ])
        .collect::<Vec<_>>();
    expected.sort_unstable();
    if inventory != expected {
        return Err(format!("architecture inventory changed: {inventory:?}"));
    }
    let compose =
        fs::read_to_string(root.join("compose.prod.yml")).map_err(|error| error.to_string())?;
    if !isolated_runner_auth_defaults(&compose) {
        return Err("AI Runner auth defaults overlap the Server data root".into());
    }
    scan(root, "crates", RUST_FORBIDDEN)?;
    scan(root, "frontend/src", TS_FORBIDDEN)
}

pub(super) fn isolated_runner_auth_defaults(compose: &str) -> bool {
    compose.contains("${FLORI_QODER_AUTH_DIR:-./runner-auth/qoder}")
        && compose.contains("${FLORI_CODEX_AUTH_FILE:-./runner-auth/codex/auth.json}")
        && !compose.contains("./data/runner-auth")
}

fn scan(root: &Path, directory: &str, patterns: &str) -> Result<(), String> {
    for relative in repository_files(root, &[directory])? {
        if patterns == TS_FORBIDDEN && !(relative.ends_with(".ts") || relative.ends_with(".vue")) {
            continue;
        }
        let text = fs::read_to_string(root.join(&relative)).map_err(|error| error.to_string())?;
        if is_product_module(&relative) && !module_sections_within_budget(&text) {
            return Err(format!(
                "product or inline test module exceeds 300 lines: {relative}"
            ));
        }
        if let Some(pattern) = patterns.split('\0').find(|pattern| text.contains(pattern)) {
            return Err(format!("forbidden pattern {pattern:?} in {relative}"));
        }
        if relative.starts_with("crates/")
            && !relative.starts_with("crates/flori-core/")
            && CORE_TYPES
                .split('\0')
                .chain(KNOWLEDGE_TYPES.split('\0'))
                .chain(SOURCE_TYPES.split('\0'))
                .any(|name| {
                    ["enum", "struct", "type"]
                        .iter()
                        .any(|kind| text.contains(&format!("{kind} {name}")))
                })
        {
            return Err(format!("core type redeclared in {relative}"));
        }
        if relative != "frontend/src/api/client.ts" && text.contains(".generated") {
            return Err(format!("generated API import outside client: {relative}"));
        }
    }
    Ok(())
}

fn is_product_module(relative: &str) -> bool {
    relative.starts_with("crates/flori-") && relative.contains("/src/") && relative.ends_with(".rs")
        || relative.starts_with("frontend/src/")
            && (relative.ends_with(".ts") || relative.ends_with(".vue"))
}

pub(super) fn nonempty_lines(text: &str) -> usize {
    text.lines().filter(|line| !line.trim().is_empty()).count()
}

pub(super) fn module_sections_within_budget(text: &str) -> bool {
    text.split("\n#[cfg(test)]")
        .all(|section| nonempty_lines(section) <= MAX_MODULE_LINES)
}
