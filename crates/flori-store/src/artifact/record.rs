use std::path::PathBuf;

use flori_core::{ErrorCode, Sha256Digest, UploadId, UploadState};

use super::{
    ArtifactStoreError,
    path::{validate_final_path, validate_name},
};

pub struct UploadRecord {
    pub(super) id: UploadId,
    pub(super) name: String,
    pub(super) final_relative_path: String,
    pub(super) expected_size_bytes: u64,
    pub(super) expected_sha256: Sha256Digest,
    pub(super) received_bytes: u64,
    pub(super) state: UploadState,
    pub(super) declared_max_size_bytes: u64,
}

impl UploadRecord {
    pub fn new(
        id: UploadId,
        name: impl Into<String>,
        final_relative_path: impl Into<String>,
        expected_size_bytes: u64,
        expected_sha256: Sha256Digest,
        declared_name: &str,
        declared_max_size_bytes: u64,
    ) -> Result<Self, ArtifactStoreError> {
        let name = name.into();
        if name != declared_name {
            return Err(ArtifactStoreError::with_code(ErrorCode::ArtifactUndeclared));
        }
        let upload = Self {
            id,
            name,
            final_relative_path: final_relative_path.into(),
            expected_size_bytes,
            expected_sha256,
            received_bytes: 0,
            state: UploadState::Receiving,
            declared_max_size_bytes,
        };
        upload.revalidate()?;
        Ok(upload)
    }

    pub fn restore_progress(
        &mut self,
        received_bytes: u64,
        state: UploadState,
    ) -> Result<(), ArtifactStoreError> {
        if received_bytes > self.expected_size_bytes
            || (state != UploadState::Receiving && received_bytes != self.expected_size_bytes)
        {
            return Err(ArtifactStoreError::with_code(ErrorCode::CorruptState));
        }
        self.received_bytes = received_bytes;
        self.state = state;
        self.revalidate()
    }

    #[must_use]
    pub fn staging_relative_path(&self) -> PathBuf {
        PathBuf::from(format!(".staging/uploads/{}", self.id))
    }

    #[must_use]
    pub fn final_relative_path(&self) -> &str {
        &self.final_relative_path
    }

    #[must_use]
    pub fn expected_size_bytes(&self) -> u64 {
        self.expected_size_bytes
    }

    pub(super) fn revalidate(&self) -> Result<(), ArtifactStoreError> {
        validate_name(&self.name)?;
        validate_final_path(&self.final_relative_path)?;
        if self.expected_size_bytes > self.declared_max_size_bytes {
            return Err(ArtifactStoreError::with_code(ErrorCode::ArtifactTooLarge));
        }
        if self.received_bytes > self.expected_size_bytes
            || (self.state != UploadState::Receiving
                && self.received_bytes != self.expected_size_bytes)
        {
            return Err(ArtifactStoreError::with_code(ErrorCode::CorruptState));
        }
        Ok(())
    }
}
