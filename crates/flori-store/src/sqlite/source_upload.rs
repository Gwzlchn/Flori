mod prepare;

use flori_core::{CreateUploadSource, Sha256Digest, SourceId, UploadId};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PreparedSourceUpload {
    pub source_id: SourceId,
    pub upload_id: Option<UploadId>,
    pub received_bytes: u64,
}

#[derive(Clone, Copy, Debug)]
pub struct StartSourceUpload<'a> {
    pub request: &'a CreateUploadSource,
    pub request_sha256: &'a Sha256Digest,
    pub media_type: &'a str,
    pub size_bytes: u64,
    pub created_at_ms: i64,
}
