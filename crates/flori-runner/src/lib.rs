//! Flori Runner 的出站 HTTP 与有限本地恢复边界。

#![forbid(unsafe_code)]

#[cfg(any(feature = "codex", feature = "qoder"))]
mod ai;
mod attempt;
mod client;
mod content;
#[cfg(any(feature = "codex", feature = "qoder"))]
mod daemon;
mod digest;
#[cfg(feature = "media")]
mod media;
mod spool;
mod upload;

#[cfg(any(feature = "codex", feature = "qoder"))]
pub use ai::process::{
    AiProcessConfig, AiProcessError, AiProcessOutput, AiProcessTermination, run_ai_process,
};
#[cfg(feature = "qoder")]
pub use ai::qoder::{
    QODERCLI_PROGRAM, QODERCLI_VERSION, QoderCommand, QoderError, QoderResult,
    invocation_command as qoder_invocation_command, model_list_command as qoder_model_list_command,
    parse_result as qoder_parse_result, verify_model_allowlist as qoder_verify_model_allowlist,
    verify_version as qoder_verify_version, version_command as qoder_version_command,
};
#[cfg(feature = "codex")]
pub use ai::{
    CodexAdapterError, CodexCommand, CodexParsedOutput, CodexWebSearchObservation,
    build_codex_command, parse_codex_output,
};
pub use client::{ClientError, RunnerClient};
#[cfg(any(feature = "codex", feature = "qoder"))]
pub use daemon::{DaemonConfig, run as run_ai_daemon};
#[cfg(feature = "media")]
pub use media::pdf::{
    PdfAcquireConfig, PdfDaemonConfig, PdfExtractConfig, acquire_pdf, extract_pdf, run_pdf_daemon,
};
pub use reqwest::Url as ProxyUrl;
pub use spool::{Spool, SpoolError, SpoolUpload};
pub use upload::manifest_sha256;
