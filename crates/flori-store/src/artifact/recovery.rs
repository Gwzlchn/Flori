use std::{fs, path::Path};

use flori_core::{ErrorCode, UploadState};

use super::{ArtifactStoreError, NasArtifactStore, UploadRecord};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RecoveryAction {
    ResumeReceiving,
    MoveVerified,
    MarkMoved,
    RetryCommit,
    DeleteFilesThenLedger,
    DeleteLedger,
}

impl NasArtifactStore {
    pub fn discard(&self, upload: &UploadRecord) -> Result<(), ArtifactStoreError> {
        match self.recovery_action(upload, false)? {
            RecoveryAction::DeleteLedger => Ok(()),
            RecoveryAction::DeleteFilesThenLedger => {
                for relative in [
                    upload.staging_relative_path(),
                    Path::new(&upload.final_relative_path).to_path_buf(),
                ] {
                    let path = self.safe_path(&relative, false)?;
                    match fs::remove_file(path) {
                        Ok(()) => {}
                        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                        Err(error) => return Err(error.into()),
                    }
                }
                Ok(())
            }
            _ => Err(ArtifactStoreError::with_code(ErrorCode::CorruptState)),
        }
    }

    pub fn move_verified(&self, upload: &UploadRecord) -> Result<(), ArtifactStoreError> {
        match self.recovery_action(upload, true)? {
            RecoveryAction::MoveVerified => {
                let final_path = self.safe_path(Path::new(&upload.final_relative_path), true)?;
                fs::rename(self.root.join(upload.staging_relative_path()), final_path)?;
                Ok(())
            }
            RecoveryAction::MarkMoved | RecoveryAction::RetryCommit => Ok(()),
            _ => Err(ArtifactStoreError::with_code(ErrorCode::Conflict)),
        }
    }

    pub fn recovery_action(
        &self,
        upload: &UploadRecord,
        owner_valid: bool,
    ) -> Result<RecoveryAction, ArtifactStoreError> {
        upload.revalidate()?;
        let staging = upload.staging_relative_path();
        let has_staging = self.file_len(&staging)?.is_some();
        let final_path = Path::new(&upload.final_relative_path);
        let has_final = self.file_len(final_path)?.is_some();
        if !owner_valid {
            return Ok(if has_staging || has_final {
                RecoveryAction::DeleteFilesThenLedger
            } else {
                RecoveryAction::DeleteLedger
            });
        }
        if upload.expected_size_bytes > self.max_size_bytes {
            return Err(ArtifactStoreError::with_code(ErrorCode::CorruptState));
        }
        let action = match (upload.state, has_staging, has_final) {
            (UploadState::Receiving, false, false) if upload.received_bytes == 0 => {
                RecoveryAction::ResumeReceiving
            }
            (UploadState::Receiving, true, false) => {
                let length = self.file_len(&staging)?;
                if length != Some(upload.received_bytes)
                    || length.is_some_and(|length| length > upload.expected_size_bytes)
                {
                    return Err(ArtifactStoreError::with_code(ErrorCode::CorruptState));
                }
                RecoveryAction::ResumeReceiving
            }
            (UploadState::Verified, true, false) => RecoveryAction::MoveVerified,
            (UploadState::Verified, false, true) => RecoveryAction::MarkMoved,
            (UploadState::Moved, false, true) => RecoveryAction::RetryCommit,
            _ => return Err(ArtifactStoreError::with_code(ErrorCode::CorruptState)),
        };
        if upload.state != UploadState::Receiving {
            self.check_exact(if has_staging { &staging } else { final_path }, upload)
                .map_err(|error| {
                    if error.code() == ErrorCode::StorageUnavailable {
                        error
                    } else {
                        ArtifactStoreError::with_code(ErrorCode::CorruptState)
                    }
                })?;
        }
        Ok(action)
    }
}
