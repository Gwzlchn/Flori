use flori_core::{ErrorCode, LogFrame, SecretInputs};

use super::SpoolError;
use crate::digest;

pub(super) fn redact_frame(
    frame: &LogFrame,
    secrets: &SecretInputs,
) -> Result<LogFrame, SpoolError> {
    let mut line = frame.line.clone();
    if let Some(credential) = &secrets.credential
        && !credential.value.is_empty()
    {
        line = line.replace(&credential.value, "[REDACTED]");
        let escaped = serde_json::to_string(&credential.value)
            .map_err(|_| SpoolError::rejected(ErrorCode::Internal))?;
        line = line.replace(&escaped[1..escaped.len() - 1], "[REDACTED]");
    }
    let sha256 =
        digest::sha256(line.as_bytes()).map_err(|_| SpoolError::rejected(ErrorCode::Internal))?;
    Ok(LogFrame {
        sequence: frame.sequence,
        sha256,
        line,
    })
}
