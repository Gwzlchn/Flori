use flori_core::{ArtifactKind, ErrorCode, LogFrame, TaskClaim, TaskLogLevel, TaskLogLine};
use sha2::{Digest, Sha256};

use crate::RunnerClient;

pub(super) async fn started(client: &RunnerClient, claim: &TaskClaim) -> Result<(), ErrorCode> {
    if !claim
        .output_declarations
        .iter()
        .any(|declaration| declaration.kind == ArtifactKind::TaskLog)
    {
        return Ok(());
    }
    let line = serde_json::to_string(&TaskLogLine {
        timestamp_ms: unix_ms()?,
        level: TaskLogLevel::Info,
        message: "AI task started".to_owned(),
    })
    .map_err(|_| ErrorCode::Internal)?;
    client
        .append_logs(
            claim.exec_id,
            &[LogFrame {
                sequence: 1,
                sha256: digest(line.as_bytes())?,
                line,
            }],
        )
        .await
        .map_err(|error| error.code())?;
    Ok(())
}

fn unix_ms() -> Result<u64, ErrorCode> {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|_| ErrorCode::Internal)?
        .as_millis()
        .try_into()
        .map_err(|_| ErrorCode::Internal)
}

fn digest(bytes: &[u8]) -> Result<flori_core::Sha256Digest, ErrorCode> {
    flori_core::Sha256Digest::parse(
        Sha256::digest(bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .map_err(|_| ErrorCode::Internal)
}
