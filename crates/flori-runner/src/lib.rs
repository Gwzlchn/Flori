//! Flori Runner 的出站 HTTP 与有限本地恢复边界。

#![forbid(unsafe_code)]

mod attempt;
mod client;
mod digest;
mod spool;
mod upload;

pub use client::{ClientError, RunnerClient};
pub use spool::{Spool, SpoolError, SpoolUpload};
pub use upload::manifest_sha256;
