use std::{
    fs::File,
    io::{Read, Seek, SeekFrom},
    path::Path,
};

use flori_core::{ErrorCode, Sha256Digest};
use sha2::{Digest, Sha256};

use super::{ArtifactStoreError, NasArtifactStore, digest_is, path};

impl NasArtifactStore {
    pub fn open_verified_range(
        &self,
        relative_path: &str,
        expected_size_bytes: u64,
        expected_sha256: &Sha256Digest,
        start: u64,
        end_exclusive: u64,
    ) -> Result<File, ArtifactStoreError> {
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
            offset = chunk_end;
        }
        if offset != expected_size_bytes || !digest_is(&hasher.finalize(), expected_sha256) {
            return Err(ArtifactStoreError::with_code(ErrorCode::DigestMismatch));
        }
        file.seek(SeekFrom::Start(start))?;
        Ok(file)
    }
}

#[cfg(test)]
mod tests {
    use std::{fs, io::Read};

    use flori_core::{SourceId, SourceInputId};

    use super::*;
    use crate::artifact::{sha256, source_input_path};

    #[test]
    fn verified_ranges_return_a_seeked_file_without_range_sized_allocation() {
        let root = std::env::temp_dir().join(format!(
            "flori-content-stream-{}",
            flori_core::RequestId::generate()
        ));
        let store = NasArtifactStore::new(&root, 1024 * 1024).expect("store");
        let relative =
            source_input_path(SourceId::generate(), SourceInputId::generate(), "big.bin")
                .expect("path");
        let path = root.join(&relative);
        fs::create_dir_all(path.parent().expect("parent")).expect("parents");
        let body = vec![b'x'; 512 * 1024];
        fs::write(&path, &body).expect("fixture");

        let mut file = store
            .open_verified_range(&relative, body.len() as u64, &sha256(&body), 1, 2)
            .expect("verified file");
        let mut byte = [0_u8; 1];
        file.read_exact(&mut byte).expect("range byte");
        assert_eq!(byte, [b'x']);
        assert_eq!(file.metadata().expect("metadata").len(), body.len() as u64);

        fs::remove_dir_all(root).expect("cleanup");
    }

    #[test]
    fn verified_range_rejects_size_and_digest_drift() {
        let root = std::env::temp_dir().join(format!(
            "flori-content-drift-{}",
            flori_core::RequestId::generate()
        ));
        let store = NasArtifactStore::new(&root, 1024).expect("store");
        let relative =
            source_input_path(SourceId::generate(), SourceInputId::generate(), "input.bin")
                .expect("path");
        let path = root.join(&relative);
        fs::create_dir_all(path.parent().expect("parent")).expect("parents");
        fs::write(&path, b"actual").expect("fixture");

        for (size, digest) in [(5, sha256(b"actual")), (6, sha256(b"wrong!"))] {
            assert_eq!(
                store
                    .open_verified_range(&relative, size, &digest, 0, size)
                    .expect_err("drift")
                    .code(),
                ErrorCode::DigestMismatch
            );
        }

        fs::remove_dir_all(root).expect("cleanup");
    }
}
