//! Flori Runner 的出站 HTTP 与有限本地恢复边界。

#![forbid(unsafe_code)]

mod ai;
mod attempt;
mod client;
mod digest;
mod spool;
mod upload;

pub use ai::qoder::{
    QODERCLI_PROGRAM, QODERCLI_VERSION, QoderCommand, QoderError, QoderResult, invocation_command,
    model_list_command, parse_result, verify_model_allowlist, verify_version, version_command,
};
pub use client::{ClientError, RunnerClient};
pub use spool::{Spool, SpoolError, SpoolUpload};
pub use upload::manifest_sha256;
