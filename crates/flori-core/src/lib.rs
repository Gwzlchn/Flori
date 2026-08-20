//! Flori vNext 的唯一领域与传输类型。

#![forbid(unsafe_code)]

mod artifact;
mod enums;
mod ids;
mod job;
mod materialize;
mod openapi;
mod runner_claim;
mod runner_protocol;

pub use artifact::*;
pub use enums::*;
pub use ids::*;
pub use job::*;
pub use materialize::*;
pub use openapi::openapi_json;
pub use runner_claim::*;
pub use runner_protocol::*;

pub const CONTRACT_REVISION: &str = "flori.v1";
pub const PIPELINE_COMPILER_VERSION: u8 = 1;
pub const PROTOCOL_VERSION: &str = "1";
