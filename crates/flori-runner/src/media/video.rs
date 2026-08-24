#[path = "video/frames.rs"]
mod frames;
#[path = "video/probe.rs"]
mod probe;
#[path = "video/process.rs"]
mod process;
#[path = "video/scene.rs"]
mod scene;
#[path = "video/similarity.rs"]
mod similarity;
#[path = "video/subtitle.rs"]
mod subtitle;
#[path = "video/transcribe.rs"]
pub(crate) mod transcribe;

pub(crate) use frames::extract_keyframes;
pub(crate) use probe::probe_video;
pub(crate) use subtitle::{mechanical_note_with_frames, normalize_srt};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum VideoMediaError {
    InvalidProbe,
    InvalidSubtitle,
    InvalidTranscriber,
    InvalidFrameRequest,
    ToolFailed,
    ToolTimedOut,
    OutputTooLarge,
}

impl VideoMediaError {
    pub(crate) fn code(self) -> flori_core::ErrorCode {
        match self {
            Self::InvalidProbe
            | Self::InvalidSubtitle
            | Self::InvalidTranscriber
            | Self::InvalidFrameRequest => flori_core::ErrorCode::ExecutorFailed,
            Self::ToolFailed => flori_core::ErrorCode::ExecutorFailed,
            Self::ToolTimedOut => flori_core::ErrorCode::AttemptTimeout,
            Self::OutputTooLarge => flori_core::ErrorCode::ArtifactTooLarge,
        }
    }
}
