use flori_core::{
    AttemptAck, AttemptId, FailAttemptRequest, LogCursor, LogFrame, RenewLeaseResponse, UsageAck,
    UsageUpdate,
};
use reqwest::Method;

use crate::{ClientError, RunnerClient, digest};

const MAX_LOG_LINE_BYTES: usize = 64 * 1024;

impl RunnerClient {
    pub async fn renew(&self, exec_id: AttemptId) -> Result<RenewLeaseResponse, ClientError> {
        self.send_json(self.request(Method::POST, &format!("runner/v1/attempts/{exec_id}/renew"))?)
            .await
    }

    pub async fn append_logs(
        &self,
        exec_id: AttemptId,
        frames: &[LogFrame],
    ) -> Result<LogCursor, ClientError> {
        if frames.is_empty() {
            return Err(ClientError::local(flori_core::ErrorCode::InvalidRequest));
        }
        for (index, frame) in frames.iter().enumerate() {
            if frame.sequence == 0 || index > 0 && frame.sequence != frames[index - 1].sequence + 1
            {
                return Err(ClientError::local(flori_core::ErrorCode::LogSequenceGap));
            }
            if frame.line.len() > MAX_LOG_LINE_BYTES {
                return Err(ClientError::local(flori_core::ErrorCode::ArtifactTooLarge));
            }
            if digest::sha256(frame.line.as_bytes()).ok().as_ref() != Some(&frame.sha256) {
                return Err(ClientError::local(flori_core::ErrorCode::DigestMismatch));
            }
        }
        let mut body = Vec::new();
        for frame in frames {
            serde_json::to_writer(&mut body, frame)
                .map_err(|_| ClientError::local(flori_core::ErrorCode::InvalidRequest))?;
            body.push(b'\n');
        }
        self.send_json(
            self.request(Method::POST, &format!("runner/v1/attempts/{exec_id}/logs"))?
                .header("Content-Type", "application/x-ndjson")
                .body(body),
        )
        .await
    }

    pub async fn update_usage(
        &self,
        exec_id: AttemptId,
        update: &UsageUpdate,
    ) -> Result<UsageAck, ClientError> {
        self.send_json_body(
            self.request(Method::POST, &format!("runner/v1/attempts/{exec_id}/usage"))?,
            update,
        )
        .await
    }

    pub async fn complete(
        &self,
        exec_id: AttemptId,
        manifest_sha256: flori_core::Sha256Digest,
    ) -> Result<AttemptAck, ClientError> {
        self.send_json_body(
            self.request(
                Method::POST,
                &format!("runner/v1/attempts/{exec_id}/complete"),
            )?,
            &flori_core::CompleteAttemptRequest { manifest_sha256 },
        )
        .await
    }

    pub async fn fail(
        &self,
        exec_id: AttemptId,
        request: &FailAttemptRequest,
    ) -> Result<AttemptAck, ClientError> {
        self.send_json_body(
            self.request(Method::POST, &format!("runner/v1/attempts/{exec_id}/fail"))?,
            request,
        )
        .await
    }
}
