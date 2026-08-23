use std::{collections::BTreeSet, fs, path::PathBuf};

use flori_core::{ErrorCode, SourceId};

use super::{ArtifactStoreError, NasArtifactStore, path::validate_final_path};

impl NasArtifactStore {
    pub(crate) fn stage_source_delete(
        &self,
        source_id: SourceId,
        expected_paths: &[String],
    ) -> Result<bool, ArtifactStoreError> {
        let source = self.root.join("sources").join(source_id.to_string());
        let trash_root = self.trash_root("sources")?;
        let trash = trash_root.join(source_id.to_string());
        reject_existing(&trash)?;
        match checked_directory(&source)? {
            Some(()) => {
                let expected = expected_source_paths(source_id, expected_paths)?;
                let mut actual = BTreeSet::new();
                collect_files(&self.root, &source, &mut actual)?;
                if actual != expected {
                    return Err(ArtifactStoreError::with_code(ErrorCode::CorruptState));
                }
                fs::rename(&source, &trash)?;
                sync_parent(&source)?;
                sync_directory(&trash_root)?;
                Ok(true)
            }
            None if expected_paths.is_empty() => Ok(false),
            None => Err(ArtifactStoreError::with_code(ErrorCode::CorruptState)),
        }
    }

    pub(crate) fn restore_source_delete(
        &self,
        source_id: SourceId,
    ) -> Result<(), ArtifactStoreError> {
        let source = self.root.join("sources").join(source_id.to_string());
        let trash_root = self.trash_root("sources")?;
        let trash = trash_root.join(source_id.to_string());
        if checked_directory(&trash)?.is_none() {
            return Ok(());
        }
        reject_existing(&source)?;
        if let Some(parent) = source.parent() {
            create_checked(parent)?;
        }
        fs::rename(&trash, &source)?;
        sync_directory(&trash_root)?;
        sync_parent(&source)
    }

    pub(crate) fn finish_source_delete(
        &self,
        source_id: SourceId,
    ) -> Result<(), ArtifactStoreError> {
        let source = self.root.join("sources").join(source_id.to_string());
        reject_existing(&source)?;
        let trash_root = self.trash_root("sources")?;
        let trash = trash_root.join(source_id.to_string());
        if checked_directory(&trash)?.is_some() {
            fs::remove_dir_all(&trash)?;
            sync_directory(&trash_root)?;
        }
        reject_existing(&source)
    }

    pub(crate) fn source_delete_trash(&self) -> Result<Vec<SourceId>, ArtifactStoreError> {
        let trash_root = self.trash_root("sources")?;
        let mut ids = Vec::new();
        for entry in fs::read_dir(trash_root)? {
            let entry = entry?;
            let name = entry.file_name().into_string().map_err(|_| invalid())?;
            let id = name.parse().map_err(|_| invalid())?;
            checked_directory(&entry.path())?.ok_or_else(invalid)?;
            ids.push(id);
        }
        ids.sort_unstable();
        Ok(ids)
    }

    fn trash_root(&self, kind: &str) -> Result<PathBuf, ArtifactStoreError> {
        let trash = self.root.join(".trash");
        create_checked(&trash)?;
        let root = trash.join(kind);
        create_checked(&root)?;
        Ok(root)
    }
}

fn expected_source_paths(
    source_id: SourceId,
    paths: &[String],
) -> Result<BTreeSet<String>, ArtifactStoreError> {
    let prefix = format!("sources/{source_id}/");
    let mut expected = BTreeSet::new();
    for path in paths {
        validate_final_path(path)?;
        if !path.starts_with(&prefix) || !expected.insert(path.clone()) {
            return Err(ArtifactStoreError::with_code(ErrorCode::CorruptState));
        }
    }
    Ok(expected)
}

fn collect_files(
    root: &std::path::Path,
    directory: &std::path::Path,
    files: &mut BTreeSet<String>,
) -> Result<(), ArtifactStoreError> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let metadata = fs::symlink_metadata(entry.path())?;
        if metadata.file_type().is_symlink() {
            return Err(invalid());
        }
        if metadata.is_dir() {
            collect_files(root, &entry.path(), files)?;
        } else if metadata.is_file() {
            let relative = entry
                .path()
                .strip_prefix(root)
                .map_err(|_| invalid())?
                .to_str()
                .ok_or_else(invalid)?
                .to_owned();
            if !files.insert(relative) {
                return Err(invalid());
            }
        } else {
            return Err(invalid());
        }
    }
    Ok(())
}

fn create_checked(path: &std::path::Path) -> Result<(), ArtifactStoreError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => Ok(()),
        Ok(_) => Err(invalid()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            fs::create_dir(path)?;
            sync_parent(path)
        }
        Err(error) => Err(error.into()),
    }
}

pub(super) fn checked_directory(path: &std::path::Path) -> Result<Option<()>, ArtifactStoreError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => Ok(Some(())),
        Ok(_) => Err(invalid()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

pub(super) fn reject_existing(path: &std::path::Path) -> Result<(), ArtifactStoreError> {
    match fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Ok(_) => Err(invalid()),
        Err(error) => Err(error.into()),
    }
}

pub(super) fn sync_parent(path: &std::path::Path) -> Result<(), ArtifactStoreError> {
    let parent = path.parent().ok_or_else(invalid)?;
    sync_directory(parent)
}

pub(super) fn sync_directory(path: &std::path::Path) -> Result<(), ArtifactStoreError> {
    fs::File::open(path)?.sync_all()?;
    Ok(())
}

pub(super) fn invalid() -> ArtifactStoreError {
    ArtifactStoreError::with_code(ErrorCode::ArtifactInvalidPath)
}
