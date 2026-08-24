#[path = "video/frames.rs"]
mod frames;
#[path = "video/probe.rs"]
mod probe;
#[path = "video/process.rs"]
mod process;
#[path = "video/subtitle.rs"]
mod subtitle;
#[path = "video/transcribe.rs"]
pub(crate) mod transcribe;

pub(crate) use frames::extract_keyframes;
pub(crate) use probe::probe_video;
pub(crate) use subtitle::{mechanical_note, normalize_srt};

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
