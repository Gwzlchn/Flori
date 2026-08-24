mod daemon;
pub(crate) mod pdf;
pub(crate) mod video;
#[path = "video/acquire.rs"]
mod video_acquire;
#[path = "video/claim.rs"]
mod video_claim;
#[path = "video/daemon.rs"]
mod video_daemon;

pub use video::transcribe::FasterWhisperConfig;
pub use video_daemon::VideoDaemonConfig;

pub async fn run_media_daemon(
    client: &crate::RunnerClient,
    pdf: &pdf::PdfDaemonConfig,
    video: &VideoDaemonConfig,
    cancel: &mut tokio::sync::watch::Receiver<bool>,
) -> Result<(), flori_core::ErrorCode> {
    daemon::run_media(client, pdf, video, cancel).await
}
