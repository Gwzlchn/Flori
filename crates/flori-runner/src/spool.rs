use std::fmt;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};

use flori_core::{AttemptId, ErrorCode, LogFrame, SecretInputs, Sha256Digest, UploadId};
use serde::{Deserialize, Serialize};

mod redact;

use redact::redact_frame;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SpoolUpload {
    pub exec_id: AttemptId,
    pub upload_id: UploadId,
    pub name: String,
    pub relative_path: String,
    pub size_bytes: u64,
    pub sha256: Sha256Digest,
    pub received_bytes: u64,
}

pub struct Spool {
    root: PathBuf,
    max_bytes: u64,
}

#[derive(Debug)]
pub struct SpoolError {
    code: ErrorCode,
    source: Option<io::Error>,
}

impl SpoolError {
    const fn rejected(code: ErrorCode) -> Self {
        Self { code, source: None }
    }

    #[must_use]
    pub const fn code(&self) -> ErrorCode {
        self.code
    }
}

impl fmt::Display for SpoolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "Runner spool failed: {:?}", self.code)
    }
}

impl std::error::Error for SpoolError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        self.source
            .as_ref()
            .map(|source| source as &(dyn std::error::Error + 'static))
    }
}

impl From<io::Error> for SpoolError {
    fn from(source: io::Error) -> Self {
        Self {
            code: ErrorCode::StorageUnavailable,
            source: Some(source),
        }
    }
}

impl Spool {
    pub fn open(root: impl AsRef<Path>, max_bytes: u64) -> Result<Self, SpoolError> {
        if max_bytes == 0 {
            return Err(SpoolError::rejected(ErrorCode::InvalidRequest));
        }
        fs::create_dir_all(root.as_ref())?;
        reject_symlink(root.as_ref())?;
        Ok(Self {
            root: root.as_ref().to_path_buf(),
            max_bytes,
        })
    }

    pub fn queue_log(
        &self,
        exec_id: AttemptId,
        frame: &LogFrame,
        secrets: &SecretInputs,
    ) -> Result<LogFrame, SpoolError> {
        let frame = redact_frame(frame, secrets)?;
        let mut frames = self.logs(exec_id)?;
        match frames.last() {
            Some(last) if frame.sequence == last.sequence && frame.sha256 == last.sha256 => {
                return Ok(frame);
            }
            Some(last) if frame.sequence == last.sequence => {
                return Err(SpoolError::rejected(ErrorCode::LogSequenceConflict));
            }
            Some(last) if frame.sequence != last.sequence + 1 => {
                return Err(SpoolError::rejected(ErrorCode::LogSequenceGap));
            }
            None if frame.sequence == 0 => {
                return Err(SpoolError::rejected(ErrorCode::LogSequenceGap));
            }
            _ => {}
        }
        frames.push(frame.clone());
        self.write_json(&self.attempt_dir(exec_id).join("logs.json"), &frames)?;
        Ok(frame)
    }

    pub fn logs(&self, exec_id: AttemptId) -> Result<Vec<LogFrame>, SpoolError> {
        self.read_json(&self.attempt_dir(exec_id).join("logs.json"))
    }

    pub fn acknowledge_logs(
        &self,
        exec_id: AttemptId,
        last_sequence: u64,
    ) -> Result<(), SpoolError> {
        let frames = self
            .logs(exec_id)?
            .into_iter()
            .filter(|frame| frame.sequence > last_sequence)
            .collect::<Vec<_>>();
        self.write_or_remove(&self.attempt_dir(exec_id).join("logs.json"), &frames)
    }

    pub fn save_upload(&self, upload: &SpoolUpload) -> Result<(), SpoolError> {
        if upload.received_bytes > upload.size_bytes || !safe_relative_path(&upload.relative_path) {
            return Err(SpoolError::rejected(ErrorCode::InvalidRequest));
        }
        let mut uploads = self.uploads(upload.exec_id)?;
        match uploads.iter_mut().find(|item| item.name == upload.name) {
            Some(existing)
                if existing.upload_id != upload.upload_id
                    || existing.exec_id != upload.exec_id
                    || existing.relative_path != upload.relative_path
                    || existing.size_bytes != upload.size_bytes
                    || existing.sha256 != upload.sha256
                    || upload.received_bytes < existing.received_bytes =>
            {
                return Err(SpoolError::rejected(ErrorCode::Conflict));
            }
            Some(existing) => existing.clone_from(upload),
            None => uploads.push(upload.clone()),
        }
        uploads.sort_by(|left, right| left.name.cmp(&right.name));
        self.write_json(
            &self.attempt_dir(upload.exec_id).join("uploads.json"),
            &uploads,
        )
    }

    pub fn uploads(&self, exec_id: AttemptId) -> Result<Vec<SpoolUpload>, SpoolError> {
        self.read_json(&self.attempt_dir(exec_id).join("uploads.json"))
    }

    pub fn remove_upload(&self, exec_id: AttemptId, upload_id: UploadId) -> Result<(), SpoolError> {
        let uploads = self
            .uploads(exec_id)?
            .into_iter()
            .filter(|upload| upload.upload_id != upload_id)
            .collect::<Vec<_>>();
        self.write_or_remove(&self.attempt_dir(exec_id).join("uploads.json"), &uploads)
    }

    pub fn clear_attempt(&self, exec_id: AttemptId) -> Result<(), SpoolError> {
        let directory = self.attempt_dir(exec_id);
        match fs::remove_dir_all(directory) {
            Ok(()) => sync_directory(&self.root),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }

    fn attempt_dir(&self, exec_id: AttemptId) -> PathBuf {
        self.root.join(exec_id.to_string())
    }

    fn read_json<T>(&self, path: &Path) -> Result<Vec<T>, SpoolError>
    where
        T: for<'de> Deserialize<'de>,
    {
        match fs::read(path) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map_err(|_| SpoolError::rejected(ErrorCode::CorruptState)),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(Vec::new()),
            Err(error) => Err(error.into()),
        }
    }

    fn write_or_remove<T: Serialize>(&self, path: &Path, values: &[T]) -> Result<(), SpoolError> {
        if values.is_empty() {
            match fs::remove_file(path) {
                Ok(()) => {
                    if let Some(parent) = path.parent() {
                        sync_directory(parent)?;
                    }
                    return Ok(());
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
                Err(error) => return Err(error.into()),
            }
        }
        self.write_json(path, values)
    }

    fn write_json<T: Serialize + ?Sized>(&self, path: &Path, value: &T) -> Result<(), SpoolError> {
        let bytes =
            serde_json::to_vec(value).map_err(|_| SpoolError::rejected(ErrorCode::Internal))?;
        let previous = fs::metadata(path).map_or(0, |metadata| metadata.len());
        let total = directory_bytes(&self.root)?;
        if total
            .saturating_sub(previous)
            .saturating_add(bytes.len() as u64)
            > self.max_bytes
        {
            return Err(SpoolError::rejected(ErrorCode::ArtifactTooLarge));
        }
        let parent = path
            .parent()
            .ok_or_else(|| SpoolError::rejected(ErrorCode::InvalidRequest))?;
        fs::create_dir_all(parent)?;
        reject_symlink(parent)?;
        if path
            .symlink_metadata()
            .is_ok_and(|metadata| metadata.file_type().is_symlink())
        {
            return Err(SpoolError::rejected(ErrorCode::StorageUnavailable));
        }
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_err(|_| SpoolError::rejected(ErrorCode::Internal))?
            .as_nanos();
        let temporary = parent.join(format!(
            ".{}.{}.{}.tmp",
            std::process::id(),
            nonce,
            file_name(path)?
        ));
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&temporary)?;
        let result = (|| {
            file.write_all(&bytes)?;
            file.sync_all()?;
            fs::rename(&temporary, path)?;
            sync_directory(parent)
        })();
        if result.is_err() {
            let _ = fs::remove_file(temporary);
        }
        result
    }
}

fn safe_relative_path(path: &str) -> bool {
    !path.is_empty()
        && !path.starts_with('/')
        && !path.contains('\\')
        && path
            .split('/')
            .all(|part| !part.is_empty() && part != "." && part != ".." && !part.starts_with('.'))
}

fn directory_bytes(root: &Path) -> Result<u64, SpoolError> {
    let mut total = 0_u64;
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let metadata = entry.path().symlink_metadata()?;
        if metadata.file_type().is_symlink() {
            return Err(SpoolError::rejected(ErrorCode::StorageUnavailable));
        }
        if metadata.is_dir() {
            total = total.saturating_add(directory_bytes(&entry.path())?);
        } else {
            total = total.saturating_add(metadata.len());
        }
    }
    Ok(total)
}

fn reject_symlink(path: &Path) -> Result<(), SpoolError> {
    if path.symlink_metadata()?.file_type().is_symlink() {
        return Err(SpoolError::rejected(ErrorCode::StorageUnavailable));
    }
    Ok(())
}

fn file_name(path: &Path) -> Result<&str, SpoolError> {
    path.file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| SpoolError::rejected(ErrorCode::InvalidRequest))
}

fn sync_directory(directory: &Path) -> Result<(), SpoolError> {
    fs::File::open(directory)?.sync_all().map_err(Into::into)
}
