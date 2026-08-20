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
    declaration_name: String,
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
        if !logical_name_matches(&name, declared_name) {
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
            declaration_name: declared_name.to_owned(),
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

    #[must_use]
    pub(crate) fn expected_sha256(&self) -> &Sha256Digest {
        &self.expected_sha256
    }

    #[must_use]
    pub(crate) fn received_bytes(&self) -> u64 {
        self.received_bytes
    }

    #[must_use]
    pub(crate) fn state(&self) -> UploadState {
        self.state
    }

    pub(super) fn revalidate(&self) -> Result<(), ArtifactStoreError> {
        if !logical_name_matches(&self.name, &self.declaration_name) {
            return Err(ArtifactStoreError::with_code(ErrorCode::CorruptState));
        }
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

fn logical_name_matches(name: &str, declaration_name: &str) -> bool {
    if name == declaration_name {
        return validate_name(name).is_ok();
    }
    name.strip_prefix(declaration_name)
        .and_then(|suffix| suffix.strip_prefix('/'))
        .is_some_and(|basename| validate_name(basename).is_ok())
}
