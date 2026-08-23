use std::{fs, path::PathBuf};

use flori_core::{ErrorCode, SourceId};

use super::{ArtifactStoreError, NasArtifactStore};

impl NasArtifactStore {
    pub(crate) fn stage_source_delete(
        &self,
        source_id: SourceId,
        rows_exist: bool,
    ) -> Result<bool, ArtifactStoreError> {
        let source = self.root.join("sources").join(source_id.to_string());
        let trash_root = self.trash_root("sources")?;
        let trash = trash_root.join(source_id.to_string());
        reject_existing(&trash)?;
        match checked_directory(&source)? {
            Some(()) => {
                fs::rename(&source, &trash)?;
                sync_parent(&source)?;
                sync_directory(&trash_root)?;
                Ok(true)
            }
            None if !rows_exist => Ok(false),
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
        let trash_root = self.trash_root("sources")?;
        let trash = trash_root.join(source_id.to_string());
        if checked_directory(&trash)?.is_some() {
            fs::remove_dir_all(&trash)?;
            sync_directory(&trash_root)?;
        }
        Ok(())
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

fn checked_directory(path: &std::path::Path) -> Result<Option<()>, ArtifactStoreError> {
    match fs::symlink_metadata(path) {
        Ok(metadata) if metadata.is_dir() && !metadata.file_type().is_symlink() => Ok(Some(())),
        Ok(_) => Err(invalid()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

fn reject_existing(path: &std::path::Path) -> Result<(), ArtifactStoreError> {
    match fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Ok(_) => Err(invalid()),
        Err(error) => Err(error.into()),
    }
}

fn sync_parent(path: &std::path::Path) -> Result<(), ArtifactStoreError> {
    let parent = path.parent().ok_or_else(invalid)?;
    sync_directory(parent)
}

fn sync_directory(path: &std::path::Path) -> Result<(), ArtifactStoreError> {
    fs::File::open(path)?.sync_all()?;
    Ok(())
}

fn invalid() -> ArtifactStoreError {
    ArtifactStoreError::with_code(ErrorCode::ArtifactInvalidPath)
}
