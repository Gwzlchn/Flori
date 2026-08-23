mod acquire;
mod claim;
mod daemon;
mod extract;
mod log;
mod network;
mod process;
mod scan;
mod scholarly;
mod scholarly_fetch;
mod scholarly_html;
mod upload;

pub use acquire::{PdfAcquireConfig, acquire_pdf};
pub use daemon::{PdfDaemonConfig, run_pdf_daemon};
pub use extract::{PdfExtractConfig, extract_pdf};

#[cfg(test)]
mod daemon_tests;
