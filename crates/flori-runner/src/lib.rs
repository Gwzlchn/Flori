//! Flori Runner 的出站 HTTP 与有限本地恢复边界。

#![forbid(unsafe_code)]

#[cfg(any(feature = "codex", feature = "qoder"))]
mod ai;
mod attempt;
#[cfg(any(feature = "codex", feature = "download", feature = "qoder"))]
mod child_process;
mod client;
mod content;
#[cfg(any(feature = "codex", feature = "qoder"))]
mod daemon;
mod digest;
#[cfg(feature = "download")]
mod download;
#[cfg(feature = "media")]
mod media;
mod spool;
#[cfg(any(feature = "download", feature = "media"))]
mod task_log;
#[cfg(any(feature = "download", feature = "media"))]
mod task_upload;
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
#[cfg(feature = "download")]
pub use download::{DownloadDaemonConfig, run_download_daemon};
#[cfg(feature = "media")]
pub use media::pdf::{
    PdfAcquireConfig, PdfDaemonConfig, PdfExtractConfig, acquire_pdf, extract_pdf, run_pdf_daemon,
};
#[cfg(feature = "media")]
pub use media::{FasterWhisperConfig, VideoDaemonConfig, run_media_daemon};
pub use reqwest::Url as ProxyUrl;
pub use spool::{Spool, SpoolError, SpoolUpload};
pub use upload::manifest_sha256;
