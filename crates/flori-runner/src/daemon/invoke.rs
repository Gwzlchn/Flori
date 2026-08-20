#[cfg(feature = "qoder")]
use std::ffi::OsString;
use std::{path::Path, time::Duration};

use flori_core::{
    AiResultEnvelope, AiTool, ErrorCode, TaskClaim, UsageOrigin, UsageUpdate, ai_result_schema_json,
};
use sha2::{Digest, Sha256};
use tokio::sync::watch;
#[cfg(feature = "codex")]
use tokio::{fs, io::AsyncReadExt};

#[cfg(feature = "qoder")]
use crate::ai::process::qoder_process;
use crate::{AiProcessConfig, AiProcessTermination};
#[cfg(feature = "codex")]
use crate::{CodexWebSearchObservation, build_codex_command, run_ai_process};
#[cfg(feature = "qoder")]
use crate::{QoderError, qoder_invocation_command, qoder_parse_result};

use super::DaemonConfig;

pub(super) struct InvocationOutcome {
    pub result: Result<AiResultEnvelope, ErrorCode>,
    pub usage: Option<UsageUpdate>,
    pub redacted_arguments: Vec<String>,
    pub websearch_urls: Vec<String>,
    pub exit_code: Option<i32>,
    pub timed_out: bool,
    pub output_sha256: flori_core::Sha256Digest,
}

pub(super) fn not_invoked(error: ErrorCode) -> Result<InvocationOutcome, ErrorCode> {
    Ok(InvocationOutcome {
        result: Err(error),
        usage: None,
        redacted_arguments: Vec::new(),
        websearch_urls: Vec::new(),
        exit_code: None,
        timed_out: false,
        output_sha256: digest(&[])?,
    })
}

pub(super) async fn run(
    config: &DaemonConfig,
    claim: &TaskClaim,
    invocation_key: &str,
    prompt: String,
    workspace: &Path,
    cancel: &mut watch::Receiver<bool>,
) -> Result<InvocationOutcome, ErrorCode> {
    let model = claim.model.as_deref().ok_or(ErrorCode::CorruptState)?;
    let effort = claim.effort.as_deref().ok_or(ErrorCode::CorruptState)?;
    let result_schema = ai_result_schema_json().map_err(|_| ErrorCode::Internal)?;
    let (arguments, stdin, redacted) = match config.tool {
        #[cfg(feature = "qoder")]
        AiTool::QoderCli => {
            let cwd = workspace.to_str().ok_or(ErrorCode::InvalidRequest)?;
            let mut prompt = prompt;
            append_section(&mut prompt, "AI RESULT JSON SCHEMA", &result_schema);
            let command = qoder_invocation_command(claim.executor, model, effort, cwd, prompt)
                .map_err(qoder_error)?;
            let stdin = command
                .standard_input()
                .ok_or(ErrorCode::CorruptState)?
                .as_bytes()
                .to_vec();
            let arguments = command.arguments().iter().map(OsString::from).collect();
            (arguments, stdin, command.redacted_arguments())
        }
        #[cfg(feature = "codex")]
        AiTool::CodexCli => {
            let schema = workspace.join("ai-result.schema.json");
            let result = workspace.join("ai-result.json");
            fs::write(&schema, result_schema)
                .await
                .map_err(|_| ErrorCode::StorageUnavailable)?;
            let command = build_codex_command(
                claim.executor,
                model,
                effort,
                workspace,
                &schema,
                &result,
                &prompt,
            )
            .map_err(|_| ErrorCode::ExecutorFailed)?;
            let redacted = command
                .args
                .iter()
                .map(|value| {
                    value
                        .to_str()
                        .map(str::to_owned)
                        .ok_or(ErrorCode::InvalidRequest)
                })
                .collect::<Result<Vec<_>, _>>()?;
            (command.args, command.stdin.into_bytes(), redacted)
        }
        #[allow(unreachable_patterns)]
        _ => return Err(ErrorCode::InvalidRequest),
    };
    let process_config = AiProcessConfig {
        tool: config.tool,
        executable: config.executable.clone(),
        arguments,
        home: config.home.clone(),
        tool_config_home: config.tool_config_home.clone(),
        working_directory: workspace.to_owned(),
        timeout: Duration::from_millis(claim.timeout_ms),
        max_output_bytes: config.max_output_bytes,
        proxy_url: config.proxy_url.clone(),
    };
    let result = match config.tool {
        #[cfg(feature = "qoder")]
        AiTool::QoderCli => qoder_process::run(process_config, &stdin, cancel).await,
        #[cfg(feature = "codex")]
        AiTool::CodexCli => run_ai_process(&process_config, &stdin, cancel).await,
        #[allow(unreachable_patterns)]
        _ => return Err(ErrorCode::InvalidRequest),
    };
    let process = match result {
        Ok(process) => process,
        Err(error) if error.code() == ErrorCode::ArtifactTooLarge => {
            return Ok(InvocationOutcome {
                result: Err(ErrorCode::ArtifactTooLarge),
                usage: Some(unavailable(invocation_key)),
                redacted_arguments: redacted,
                websearch_urls: Vec::new(),
                exit_code: None,
                timed_out: false,
                output_sha256: digest(&[])?,
            });
        }
        Err(error) => return Err(error.code()),
    };
    let output_sha256 = digest(&process.stdout)?;
    let timed_out = process.termination == AiProcessTermination::TimedOut;
    let result = if timed_out {
        Err((ErrorCode::AttemptTimeout, unavailable(invocation_key)))
    } else if process.termination == AiProcessTermination::Canceled {
        Err((ErrorCode::TaskCanceled, unavailable(invocation_key)))
    } else {
        match config.tool {
            #[cfg(feature = "qoder")]
            AiTool::QoderCli => qoder_parse_result(
                process.exit_code,
                &process.stdout,
                config.max_output_bytes,
                claim.executor,
                invocation_key.to_owned(),
            )
            .map(|parsed| (parsed.envelope, parsed.usage, Vec::new()))
            .map_err(|error| {
                let code = qoder_error(error);
                let usage = crate::ai::qoder::parse_usage(
                    process.exit_code,
                    &process.stdout,
                    config.max_output_bytes,
                    invocation_key.to_owned(),
                )
                .unwrap_or_else(|_| unavailable(invocation_key));
                (code, usage)
            }),
            #[cfg(feature = "codex")]
            AiTool::CodexCli => {
                let stdout =
                    String::from_utf8(process.stdout).map_err(|_| ErrorCode::ExecutorFailed)?;
                let result =
                    read_result(&workspace.join("ai-result.json"), config.max_output_bytes).await?;
                crate::parse_codex_output(
                    claim.executor,
                    invocation_key,
                    process.exit_code,
                    &stdout,
                    &result,
                )
                .map(|parsed| (parsed.result, parsed.usage, urls(parsed.websearch)))
                .map_err(|_| (ErrorCode::ExecutorFailed, unavailable(invocation_key)))
            }
            #[allow(unreachable_patterns)]
            _ => return Err(ErrorCode::InvalidRequest),
        }
    };
    let (result, usage, websearch_urls) = match result {
        Ok((result, usage, urls)) => (Ok(result), Some(usage), urls),
        Err((error, usage)) => (Err(error), Some(usage), Vec::new()),
    };
    Ok(InvocationOutcome {
        result,
        usage,
        redacted_arguments: redacted,
        websearch_urls,
        exit_code: process.exit_code,
        timed_out,
        output_sha256,
    })
}

#[cfg(feature = "qoder")]
fn append_section(target: &mut String, name: &str, content: &str) {
    if !target.ends_with('\n') {
        target.push('\n');
    }
    target.push_str(name);
    target.push(' ');
    target.push_str(&content.len().to_string());
    target.push('\n');
    target.push_str(content);
    target.push('\n');
}

pub(super) fn unavailable(invocation_key: &str) -> UsageUpdate {
    UsageUpdate::Final {
        invocation_key: invocation_key.to_owned(),
        origin: UsageOrigin::Unavailable,
        input_tokens: None,
        output_tokens: None,
        cost_micros: None,
        credits_micros: None,
    }
}

#[cfg(feature = "codex")]
async fn read_result(path: &Path, max: usize) -> Result<String, ErrorCode> {
    let limit = u64::try_from(max)
        .ok()
        .and_then(|value| value.checked_add(1))
        .ok_or(ErrorCode::ArtifactTooLarge)?;
    let file = fs::File::open(path)
        .await
        .map_err(|_| ErrorCode::ExecutorFailed)?;
    let mut bytes = Vec::with_capacity(max.min(64 * 1024));
    file.take(limit)
        .read_to_end(&mut bytes)
        .await
        .map_err(|_| ErrorCode::ExecutorFailed)?;
    if bytes.len() > max {
        return Err(ErrorCode::ArtifactTooLarge);
    }
    String::from_utf8(bytes).map_err(|_| ErrorCode::ExecutorFailed)
}

#[cfg(feature = "codex")]
fn urls(observations: Vec<CodexWebSearchObservation>) -> Vec<String> {
    observations
        .into_iter()
        .filter_map(|observation| observation.url)
        .collect()
}

#[cfg(feature = "qoder")]
fn qoder_error(error: QoderError) -> ErrorCode {
    match error {
        QoderError::OutputTooLarge => ErrorCode::ArtifactTooLarge,
        _ => ErrorCode::ExecutorFailed,
    }
}

fn digest(bytes: &[u8]) -> Result<flori_core::Sha256Digest, ErrorCode> {
    flori_core::Sha256Digest::parse(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .map_err(|_| ErrorCode::Internal)
}

#[cfg(test)]
mod tests {
    use std::{
        fs as std_fs,
        os::unix::fs::PermissionsExt,
        path::{Path, PathBuf},
    };

    use flori_core::{
        AiResultSchema, ArtifactId, ArtifactKind, AttemptId, Executor, JobId, RequestId,
        ResolvedArtifact, ResolvedPrompt, ResolvedTaskInputs, SecretInputs, TaskId, TermsManifest,
        TermsManifestSchema,
    };

    use super::*;

    struct TestDir(PathBuf);

    impl TestDir {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!("flori-daemon-{}", RequestId::generate()));
            for child in ["home", "config", "work"] {
                std_fs::create_dir_all(root.join(child)).expect("test directory");
            }
            Self(root)
        }

        fn script(&self, name: &str, body: &str) -> PathBuf {
            let path = self.0.join(name);
            std_fs::write(&path, format!("#!/bin/sh\n{body}")).expect("write fake executable");
            let mut permissions = std_fs::metadata(&path).expect("metadata").permissions();
            permissions.set_mode(0o700);
            std_fs::set_permissions(&path, permissions).expect("chmod");
            path
        }
    }

    impl Drop for TestDir {
        fn drop(&mut self) {
            std_fs::remove_dir_all(&self.0).expect("cleanup");
        }
    }

    #[tokio::test]
    async fn qoder_and_codex_keep_usage_units_distinct() {
        let root = TestDir::new();
        let envelope = note();
        let result = serde_json::to_string(&envelope).expect("result JSON");
        let qoder_outer = format!(
            r#"{{"type":"result","subtype":"success","duration_ms":1,"duration_api_ms":1,"is_error":false,"num_turns":1,"result":{},"stop_reason":"end_turn","total_cost_usd":0,"total_credits":2.5,"usage":{{}},"modelUsage":{{}},"permission_denials":[],"fast_mode_state":"off","uuid":"fake","session_id":"fake"}}"#,
            serde_json::to_string(&result).expect("nested result")
        );
        let qoder = root.script(
            "qoder",
            &format!("cat > captured-prompt\nprintf '%s' '{qoder_outer}'\n"),
        );
        let outcome = invoke(
            &root,
            AiTool::QoderCli,
            qoder,
            Executor::AiDocumentNote,
            "framed prompt".into(),
            2_000,
        )
        .await;
        assert_eq!(outcome.result, Ok(envelope.clone()));
        assert!(matches!(
            outcome.usage,
            Some(UsageUpdate::Final {
                input_tokens: None,
                output_tokens: None,
                credits_micros: Some(2_500_000),
                ..
            })
        ));
        let prompt =
            std_fs::read_to_string(root.0.join("work/captured-prompt")).expect("captured prompt");
        assert!(prompt.starts_with("framed prompt\nAI RESULT JSON SCHEMA "));
        assert!(prompt.contains("flori.ai_result.v1"));

        let agent = format!(
            r#"{{"type":"item.completed","item":{{"id":"item","type":"agent_message","text":{}}}}}"#,
            serde_json::to_string(&result).expect("agent result")
        );
        let events = [
            r#"{"type":"thread.started","thread_id":"thread"}"#,
            r#"{"type":"turn.started"}"#,
            agent.as_str(),
            r#"{"type":"turn.completed","usage":{"input_tokens":12,"cached_input_tokens":0,"cache_write_input_tokens":0,"output_tokens":3,"reasoning_output_tokens":1}}"#,
        ]
        .join("\\n");
        let codex = root.script(
            "codex",
            &format!(
                "result=''\nwhile [ \"$#\" -gt 0 ]; do\n  if [ \"$1\" = '--output-last-message' ]; then shift; result=$1; fi\n  shift\ndone\ncat >/dev/null\nprintf '%s' '{result}' > \"$result\"\nprintf '%b' '{events}\\n'\n"
            ),
        );
        let outcome = invoke(
            &root,
            AiTool::CodexCli,
            codex,
            Executor::AiDocumentNote,
            "framed prompt".into(),
            2_000,
        )
        .await;
        assert_eq!(outcome.result, Ok(envelope));
        assert!(matches!(
            outcome.usage,
            Some(UsageUpdate::Final {
                input_tokens: Some(12),
                output_tokens: Some(3),
                credits_micros: None,
                ..
            })
        ));
    }

    #[tokio::test]
    async fn failure_invalid_output_and_timeout_never_fabricate_usage() {
        let root = TestDir::new();
        for (name, body, timeout, expected) in [
            (
                "nonzero",
                "cat >/dev/null\nexit 7\n",
                2_000,
                ErrorCode::ExecutorFailed,
            ),
            (
                "invalid",
                "cat >/dev/null\nprintf nope\n",
                2_000,
                ErrorCode::ExecutorFailed,
            ),
            (
                "timeout",
                "cat >/dev/null\nwhile :; do :; done\n",
                20,
                ErrorCode::AttemptTimeout,
            ),
        ] {
            let executable = root.script(name, body);
            let outcome = invoke(
                &root,
                AiTool::QoderCli,
                executable,
                Executor::AiDocumentNote,
                "prompt".into(),
                timeout,
            )
            .await;
            assert_eq!(outcome.result, Err(expected));
            assert!(matches!(
                outcome.usage,
                Some(UsageUpdate::Final {
                    origin: UsageOrigin::Unavailable,
                    input_tokens: None,
                    output_tokens: None,
                    credits_micros: None,
                    ..
                })
            ));
        }

        let outer = r#"{"type":"result","subtype":"success","duration_ms":1,"duration_api_ms":1,"is_error":false,"num_turns":1,"result":"not a Flori envelope","stop_reason":"end_turn","total_cost_usd":0,"total_credits":1.25,"usage":{},"modelUsage":{},"permission_denials":[],"fast_mode_state":"off","origin":"headless","uuid":"fake","session_id":"fake"}"#;
        let executable = root.script(
            "invalid-envelope",
            &format!("cat >/dev/null\nprintf '%s' '{outer}'\n"),
        );
        let outcome = invoke(
            &root,
            AiTool::QoderCli,
            executable,
            Executor::AiDocumentNote,
            "prompt".into(),
            2_000,
        )
        .await;
        assert_eq!(outcome.result, Err(ErrorCode::ExecutorFailed));
        assert!(matches!(
            outcome.usage,
            Some(UsageUpdate::Final {
                origin: UsageOrigin::Observed,
                credits_micros: Some(1_250_000),
                ..
            })
        ));
    }

    #[tokio::test]
    async fn codex_result_file_is_read_with_a_hard_byte_limit() {
        let root = TestDir::new();
        let path = root.0.join("oversize-result");
        let file = std_fs::File::create(&path).expect("result file");
        file.set_len(8 * 1024 * 1024).expect("sparse result");
        assert_eq!(
            read_result(&path, 64).await,
            Err(ErrorCode::ArtifactTooLarge)
        );
    }

    async fn invoke(
        root: &TestDir,
        tool: AiTool,
        executable: PathBuf,
        executor: Executor,
        prompt: String,
        timeout_ms: u64,
    ) -> InvocationOutcome {
        let config = DaemonConfig {
            tool,
            executable,
            home: root.0.join("home"),
            tool_config_home: root.0.join("config"),
            work_root: root.0.clone(),
            model: "model-1".into(),
            effort: "high".into(),
            renew_interval: Duration::from_secs(1),
            max_output_bytes: 1024 * 1024,
            proxy_url: (tool == AiTool::QoderCli)
                .then(|| reqwest::Url::parse("http://proxy.invalid:10809").expect("proxy")),
        };
        let (_keep, mut cancel) = watch::channel(false);
        let claim = claim(executor, timeout_ms);
        run(
            &config,
            &claim,
            "primary",
            prompt,
            Path::new(&root.0.join("work")),
            &mut cancel,
        )
        .await
        .expect("invocation outcome")
    }

    fn claim(executor: Executor, timeout_ms: u64) -> TaskClaim {
        let empty = digest(&[]).expect("digest");
        TaskClaim {
            job_id: JobId::generate(),
            task_id: TaskId::generate(),
            task_key: "note".into(),
            exec_id: AttemptId::generate(),
            attempt_no: 1,
            executor,
            timeout_ms,
            lease_expires_at_ms: i64::MAX,
            prompt_snapshot_sha256: empty.clone(),
            resolved_inputs: ResolvedTaskInputs::AiDocumentNote {
                document: ResolvedArtifact {
                    artifact_id: ArtifactId::generate(),
                    name: "document".into(),
                    kind: ArtifactKind::DocumentStructure,
                    media_type: "application/json".into(),
                    size_bytes: 0,
                    sha256: empty.clone(),
                    download_url: "https://example.invalid/document".into(),
                },
                prompt: ResolvedPrompt {
                    key: "document_note".into(),
                    content: "prompt".into(),
                    sha256: empty,
                },
                profile: None,
            },
            output_declarations: Vec::new(),
            model: Some("model-1".into()),
            effort: Some("high".into()),
            runner_config_revision: 1,
            secret_inputs: SecretInputs::default(),
        }
    }

    fn note() -> AiResultEnvelope {
        AiResultEnvelope::DocumentNote {
            schema: AiResultSchema::V1,
            smart_note_markdown: "note".into(),
            summary_markdown: "summary".into(),
            terms: TermsManifest {
                schema: TermsManifestSchema::V1,
                terms: Vec::new(),
                evidence_candidates: Vec::new(),
            },
        }
    }
}
