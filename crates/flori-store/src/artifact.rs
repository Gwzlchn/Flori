use std::{
    fmt,
    fs::{self, File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Component, Path, PathBuf},
};

use flori_core::{ErrorCode, UploadState};
use sha2::{Digest, Sha256};

mod path;
mod record;
mod recovery;

pub use path::{retained_artifact_path, source_input_path, task_artifact_path};
pub use record::UploadRecord;
pub use recovery::RecoveryAction;

#[derive(Debug)]
pub struct ArtifactStoreError {
    code: ErrorCode,
    source: Option<std::io::Error>,
}

impl ArtifactStoreError {
    #[must_use]
    pub fn code(&self) -> ErrorCode {
        self.code
    }

    pub(super) fn with_code(code: ErrorCode) -> Self {
        Self { code, source: None }
    }
}

impl From<std::io::Error> for ArtifactStoreError {
    fn from(error: std::io::Error) -> Self {
        Self {
            code: ErrorCode::StorageUnavailable,
            source: Some(error),
        }
    }
}

impl fmt::Display for ArtifactStoreError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "artifact store error: {:?}", self.code)
    }
}

impl std::error::Error for ArtifactStoreError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.source
            .as_ref()
            .map(|error| error as &(dyn std::error::Error + 'static))
    }
}

pub struct NasArtifactStore {
    root: PathBuf,
    max_size_bytes: u64,
}

impl NasArtifactStore {
    pub fn new(root: impl Into<PathBuf>, max_size_bytes: u64) -> Result<Self, ArtifactStoreError> {
        let root = root.into();
        fs::create_dir_all(&root)?;
        let metadata = fs::symlink_metadata(&root)?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(ArtifactStoreError::with_code(
                ErrorCode::ArtifactInvalidPath,
            ));
        }
        let root = fs::canonicalize(root)?;
        Ok(Self {
            root,
            max_size_bytes,
        })
    }

    pub fn append(
        &self,
        upload: &mut UploadRecord,
        offset: u64,
        bytes: &[u8],
    ) -> Result<(), ArtifactStoreError> {
        let digest = sha256(bytes);
        upload.received_bytes = self.append_chunk(upload, offset, &digest, bytes)?;
        Ok(())
    }

    pub(crate) fn validate_upload(&self, upload: &UploadRecord) -> Result<(), ArtifactStoreError> {
        upload.revalidate()?;
        if upload.expected_size_bytes > self.max_size_bytes {
            return Err(ArtifactStoreError::with_code(ErrorCode::ArtifactTooLarge));
        }
        Ok(())
    }

    pub fn append_chunk(
        &self,
        upload: &UploadRecord,
        offset: u64,
        chunk_sha256: &flori_core::Sha256Digest,
        bytes: &[u8],
    ) -> Result<u64, ArtifactStoreError> {
        upload.revalidate()?;
        if upload.state != UploadState::Receiving || sha256(bytes) != *chunk_sha256 {
            return Err(ArtifactStoreError::with_code(
                if upload.state != UploadState::Receiving {
                    ErrorCode::Conflict
                } else {
                    ErrorCode::DigestMismatch
                },
            ));
        }
        let next = offset
            .checked_add(bytes.len() as u64)
            .ok_or_else(|| ArtifactStoreError::with_code(ErrorCode::ArtifactTooLarge))?;
        if next > upload.expected_size_bytes
            || next > upload.declared_max_size_bytes
            || next > self.max_size_bytes
        {
            return Err(ArtifactStoreError::with_code(ErrorCode::ArtifactTooLarge));
        }
        let relative = upload.staging_relative_path();
        let path = self.safe_path(&relative, true)?;
        let file_len = fs::metadata(&path).map_or_else(
            |error| {
                if error.kind() == std::io::ErrorKind::NotFound {
                    Ok(0)
                } else {
                    Err(ArtifactStoreError::from(error))
                }
            },
            |metadata| Ok(metadata.len()),
        )?;
        if offset < upload.received_bytes {
            if next > upload.received_bytes || file_len < next {
                return Err(ArtifactStoreError::with_code(ErrorCode::Conflict));
            }
            self.verify_range(&path, offset, bytes, chunk_sha256)?;
            return Ok(upload.received_bytes);
        }
        if offset != upload.received_bytes {
            return Err(ArtifactStoreError::with_code(ErrorCode::Conflict));
        }
        if file_len == next && file_len > upload.received_bytes {
            self.verify_range(&path, offset, bytes, chunk_sha256)?;
            return Ok(next);
        }
        if file_len != upload.received_bytes {
            return Err(ArtifactStoreError::with_code(ErrorCode::Conflict));
        }
        let mut file = OpenOptions::new().create(true).append(true).open(path)?;
        file.write_all(bytes)?;
        file.sync_data()?;
        Ok(next)
    }

    pub fn verify_staging(&self, upload: &UploadRecord) -> Result<(), ArtifactStoreError> {
        upload.revalidate()?;
        if upload.received_bytes != upload.expected_size_bytes {
            return Err(ArtifactStoreError::with_code(ErrorCode::DigestMismatch));
        }
        if upload.expected_size_bytes == 0 {
            self.append_chunk(upload, 0, &sha256(&[]), &[])?;
        }
        self.check_exact(&upload.staging_relative_path(), upload)
    }

    fn file_len(&self, relative: &Path) -> Result<Option<u64>, ArtifactStoreError> {
        let path = self.safe_path(relative, false)?;
        match fs::metadata(path) {
            Ok(metadata) if metadata.is_file() => Ok(Some(metadata.len())),
            Ok(_) => Err(ArtifactStoreError::with_code(
                ErrorCode::ArtifactInvalidPath,
            )),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(error) => Err(error.into()),
        }
    }

    fn check_exact(
        &self,
        relative: &Path,
        upload: &UploadRecord,
    ) -> Result<(), ArtifactStoreError> {
        if self.file_len(relative)? != Some(upload.expected_size_bytes) {
            return Err(ArtifactStoreError::with_code(ErrorCode::DigestMismatch));
        }
        let mut file = File::open(self.root.join(relative))?;
        let mut hasher = Sha256::new();
        let mut buffer = [0_u8; 8192];
        loop {
            let read = file.read(&mut buffer)?;
            if read == 0 {
                break;
            }
            hasher.update(&buffer[..read]);
        }
        let digest = hasher.finalize();
        if !digest
            .iter()
            .zip(upload.expected_sha256.as_str().as_bytes().chunks_exact(2))
            .all(|(actual, hex)| *actual == (hex_value(hex[0]) << 4 | hex_value(hex[1])))
        {
            return Err(ArtifactStoreError::with_code(ErrorCode::DigestMismatch));
        }
        Ok(())
    }

    fn verify_range(
        &self,
        path: &Path,
        offset: u64,
        expected: &[u8],
        expected_sha256: &flori_core::Sha256Digest,
    ) -> Result<(), ArtifactStoreError> {
        let mut file = File::open(path)?;
        file.seek(SeekFrom::Start(offset))?;
        let mut actual = vec![0_u8; expected.len()];
        file.read_exact(&mut actual)?;
        if sha256(&actual) != *expected_sha256 || actual != expected {
            return Err(ArtifactStoreError::with_code(ErrorCode::Conflict));
        }
        Ok(())
    }

    fn safe_path(
        &self,
        relative: &Path,
        create_parents: bool,
    ) -> Result<PathBuf, ArtifactStoreError> {
        let mut current = self.root.clone();
        let mut components = relative.components().peekable();
        while let Some(component) = components.next() {
            let Component::Normal(component) = component else {
                return Err(ArtifactStoreError::with_code(
                    ErrorCode::ArtifactInvalidPath,
                ));
            };
            current.push(component);
            let is_final = components.peek().is_none();
            match fs::symlink_metadata(&current) {
                Ok(metadata) if metadata.file_type().is_symlink() => {
                    return Err(ArtifactStoreError::with_code(
                        ErrorCode::ArtifactInvalidPath,
                    ));
                }
                Ok(metadata) if !is_final && !metadata.is_dir() => {
                    return Err(ArtifactStoreError::with_code(
                        ErrorCode::ArtifactInvalidPath,
                    ));
                }
                Ok(_) => {}
                Err(error)
                    if error.kind() == std::io::ErrorKind::NotFound
                        && create_parents
                        && !is_final =>
                {
                    fs::create_dir(&current)?;
                }
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                    return Ok(self.root.join(relative));
                }
                Err(error) => return Err(error.into()),
            }
        }
        Ok(current)
    }
}

fn hex_value(byte: u8) -> u8 {
    if byte <= b'9' {
        byte - b'0'
    } else {
        byte - b'a' + 10
    }
}

fn sha256(bytes: &[u8]) -> flori_core::Sha256Digest {
    let digest = Sha256::digest(bytes);
    let mut value = String::with_capacity(64);
    for byte in digest {
        use std::fmt::Write as _;
        write!(&mut value, "{byte:02x}").expect("writing to String cannot fail");
    }
    flori_core::Sha256Digest::parse(value).expect("SHA-256 formatter is canonical")
}
