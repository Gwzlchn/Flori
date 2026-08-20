//! Flori Runner 的出站 HTTP 与有限本地恢复边界。

#![forbid(unsafe_code)]

mod ai;
mod attempt;
mod client;
mod content;
mod daemon;
mod digest;
mod media;
mod spool;
mod upload;

pub use ai::process::{
    AiProcessConfig, AiProcessError, AiProcessOutput, AiProcessTermination, run_ai_process,
};
pub use ai::qoder::{
    QODERCLI_PROGRAM, QODERCLI_VERSION, QoderCommand, QoderError, QoderResult,
    invocation_command as qoder_invocation_command, model_list_command as qoder_model_list_command,
    parse_result as qoder_parse_result, verify_model_allowlist as qoder_verify_model_allowlist,
    verify_version as qoder_verify_version, version_command as qoder_version_command,
};
pub use ai::{
    CodexAdapterError, CodexCommand, CodexParsedOutput, CodexWebSearchObservation,
    build_codex_command, parse_codex_output,
};
pub use client::{ClientError, RunnerClient};
pub use daemon::{DaemonConfig, run as run_ai_daemon};
pub use spool::{Spool, SpoolError, SpoolUpload};
pub use upload::manifest_sha256;
