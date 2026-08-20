use flori_core::{ErrorCode, LogFrame, SecretInputs, TaskLogLine};

use super::SpoolError;
use crate::digest;

pub(super) fn redact_frame(
    frame: &LogFrame,
    secrets: &SecretInputs,
) -> Result<LogFrame, SpoolError> {
    let mut decoded = serde_json::from_str::<TaskLogLine>(&frame.line)
        .map_err(|_| SpoolError::rejected(ErrorCode::InvalidRequest))?;
    if let Some(credential) = &secrets.credential
        && !credential.value.is_empty()
    {
        decoded.message = decoded.message.replace(&credential.value, "[REDACTED]");
    }
    let line =
        serde_json::to_string(&decoded).map_err(|_| SpoolError::rejected(ErrorCode::Internal))?;
    let sha256 =
        digest::sha256(line.as_bytes()).map_err(|_| SpoolError::rejected(ErrorCode::Internal))?;
    Ok(LogFrame {
        sequence: frame.sequence,
        sha256,
        line,
    })
}
