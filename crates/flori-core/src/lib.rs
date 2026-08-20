//! Flori vNext 的唯一领域与传输类型。

#![forbid(unsafe_code)]

mod artifact;
mod document;
mod enums;
mod evidence;
mod ids;
mod job;
mod knowledge;
mod materialize;
mod openapi;
mod pdf_evidence;
mod runner_claim;
mod runner_protocol;
mod video;
mod video_evidence;

pub use artifact::*;
pub use document::*;
pub use enums::*;
pub use evidence::*;
pub use ids::*;
pub use job::*;
pub use knowledge::*;
pub use materialize::*;
pub use openapi::{ai_result_schema_json, openapi_json};
pub use pdf_evidence::*;
pub use runner_claim::*;
pub use runner_protocol::*;
pub use video::*;
pub use video_evidence::*;

pub const CONTRACT_REVISION: &str = "flori.v1";
pub const PIPELINE_COMPILER_VERSION: u8 = 1;
pub const PROTOCOL_VERSION: &str = "1";
