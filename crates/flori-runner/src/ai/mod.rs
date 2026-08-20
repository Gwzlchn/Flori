#[cfg(feature = "codex")]
mod codex;
pub(crate) mod process;
#[cfg(feature = "qoder")]
pub(crate) mod qoder;

#[cfg(feature = "codex")]
pub use codex::{
    CodexAdapterError, CodexCommand, CodexParsedOutput, CodexWebSearchObservation,
    build_codex_command, parse_codex_output,
};
