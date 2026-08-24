mod acquire;
pub(super) mod claim;
pub(super) mod daemon;
mod extract;
pub(super) mod log;
mod network;
mod process;
mod scan;
mod scholarly;
mod scholarly_fetch;
mod scholarly_html;
pub(super) mod upload;

pub use acquire::{PdfAcquireConfig, acquire_pdf};
pub use daemon::{PdfDaemonConfig, run_pdf_daemon};
pub use extract::{PdfExtractConfig, extract_pdf};

#[cfg(test)]
mod daemon_tests;
