use flori_core::{
    ArtifactManifest, ArtifactManifestEntry, AttemptId, ErrorCode, JobId, Sha256Digest,
    StartUploadRequest, StartUploadResponse, TaskId, UploadCursor, UploadId, VerifyUploadRequest,
    VerifyUploadResponse,
};
use reqwest::Method;

use crate::{ClientError, RunnerClient, digest};

impl RunnerClient {
    pub async fn start_upload(
        &self,
        exec_id: AttemptId,
        request: &StartUploadRequest,
    ) -> Result<StartUploadResponse, ClientError> {
        self.send_json_body(
            self.request(
                Method::POST,
                &format!("runner/v1/attempts/{exec_id}/uploads"),
            )?,
            request,
        )
        .await
    }

    pub async fn append_upload_chunk(
        &self,
        upload_id: UploadId,
        offset: u64,
        chunk: Vec<u8>,
    ) -> Result<UploadCursor, ClientError> {
        if chunk.is_empty() {
            return Err(ClientError::local(ErrorCode::InvalidRequest));
        }
        let sha256 = digest::sha256(&chunk).map_err(|_| ClientError::local(ErrorCode::Internal))?;
        self.send_json(
            self.request(Method::PUT, &format!("runner/v1/uploads/{upload_id}"))?
                .header("Upload-Offset", offset)
                .header("X-Flori-Chunk-SHA256", sha256.as_str())
                .header("Content-Type", "application/octet-stream")
                .body(chunk),
        )
        .await
    }

    pub async fn verify_upload(
        &self,
        upload_id: UploadId,
        request: &VerifyUploadRequest,
    ) -> Result<VerifyUploadResponse, ClientError> {
        self.send_json_body(
            self.request(
                Method::POST,
                &format!("runner/v1/uploads/{upload_id}/verify"),
            )?,
            request,
        )
        .await
    }
}

pub fn manifest_sha256(
    job_id: JobId,
    task_id: TaskId,
    exec_id: AttemptId,
    mut entries: Vec<ArtifactManifestEntry>,
) -> Result<Sha256Digest, ClientError> {
    entries.sort_by(|left, right| left.name.cmp(&right.name));
    if entries.windows(2).any(|pair| pair[0].name == pair[1].name) {
        return Err(ClientError::local(ErrorCode::ArtifactUndeclared));
    }
    let manifest = ArtifactManifest::new(job_id, task_id, exec_id, entries);
    let bytes =
        serde_json::to_vec(&manifest).map_err(|_| ClientError::local(ErrorCode::Internal))?;
    digest::sha256(&bytes).map_err(|_| ClientError::local(ErrorCode::Internal))
}
