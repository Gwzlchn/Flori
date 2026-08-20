use std::{fs::File, io::Read, path::Path};

use flori_core::{ErrorCode, Sha256Digest};
use sha2::{Digest, Sha256};

use super::{ArtifactStoreError, NasArtifactStore, digest_is, path};

impl NasArtifactStore {
    pub fn read_verified_range(
        &self,
        relative_path: &str,
        expected_size_bytes: u64,
        expected_sha256: &Sha256Digest,
        start: u64,
        end_exclusive: u64,
    ) -> Result<Vec<u8>, ArtifactStoreError> {
        path::validate_final_path(relative_path)?;
        if expected_size_bytes > self.max_size_bytes
            || start > end_exclusive
            || end_exclusive > expected_size_bytes
        {
            return Err(ArtifactStoreError::with_code(ErrorCode::CorruptState));
        }
        let path = self.safe_path(Path::new(relative_path), false)?;
        let mut file = File::open(path).map_err(|error| {
            if error.kind() == std::io::ErrorKind::NotFound {
                ArtifactStoreError::with_code(ErrorCode::DigestMismatch)
            } else {
                error.into()
            }
        })?;
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.len() != expected_size_bytes {
            return Err(ArtifactStoreError::with_code(ErrorCode::DigestMismatch));
        }
        let capacity = usize::try_from(end_exclusive - start)
            .map_err(|_| ArtifactStoreError::with_code(ErrorCode::ArtifactTooLarge))?;
        let mut selected = Vec::with_capacity(capacity);
        let mut hasher = Sha256::new();
        let mut buffer = [0_u8; 64 * 1024];
        let mut offset = 0_u64;
        loop {
            let read = file.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            let read = u64::try_from(read)
                .map_err(|_| ArtifactStoreError::with_code(ErrorCode::ArtifactTooLarge))?;
            let chunk_end = offset + read;
            hasher.update(&buffer[..read as usize]);
            let selected_start = start.max(offset);
            let selected_end = end_exclusive.min(chunk_end);
            if selected_start < selected_end {
                selected.extend_from_slice(
                    &buffer[(selected_start - offset) as usize..(selected_end - offset) as usize],
                );
            }
            offset = chunk_end;
        }
        if offset != expected_size_bytes || !digest_is(&hasher.finalize(), expected_sha256) {
            return Err(ArtifactStoreError::with_code(ErrorCode::DigestMismatch));
        }
        Ok(selected)
    }
}
