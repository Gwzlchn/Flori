use std::{collections::BTreeSet, fs, path::Path};

use flori_core::{ErrorCode, JobId, SourceId};

use super::{
    ArtifactStoreError, NasArtifactStore,
    delete::{checked_directory, invalid, reject_existing, sync_directory, sync_parent},
    path::validate_final_path,
};

impl NasArtifactStore {
    pub(crate) fn stage_job_prune(
        &self,
        source_id: SourceId,
        job_id: JobId,
        all_paths: &[String],
        retained_paths: &[String],
    ) -> Result<bool, ArtifactStoreError> {
        let prefix = job_prefix(source_id, job_id);
        let all = expected_paths(&prefix, all_paths)?;
        let retained = expected_paths(&prefix, retained_paths)?;
        if !retained.is_subset(&all) {
            return Err(corrupt());
        }
        let job = self.root.join(prefix.trim_end_matches('/'));
        let trash_root = self.root.join(".trash/jobs");
        checked_create(&self.root.join(".trash"))?;
        checked_create(&trash_root)?;
        let trash = trash_root.join(job_id.to_string());
        reject_existing(&trash)?;
        match checked_directory(&job)? {
            Some(()) => {
                if collect_job_files(&job, &prefix)? != all {
                    return Err(corrupt());
                }
                fs::rename(&job, &trash)?;
                let result = (|| {
                    sync_parent(&job)?;
                    sync_directory(&trash_root)?;
                    for path in retained {
                        let suffix = path.strip_prefix(&prefix).ok_or_else(corrupt)?;
                        let source = trash.join(suffix);
                        checked_file(&source)?;
                        let target = self.safe_path(Path::new(&path), true)?;
                        fs::copy(source, &target)?;
                        fs::File::open(&target)?.sync_all()?;
                    }
                    if checked_directory(&job)?.is_some() {
                        sync_tree_directories(&job)?;
                    }
                    Ok(())
                })();
                if let Err(error) = result {
                    rollback_stage(&job, &trash)?;
                    return Err(error);
                }
                Ok(true)
            }
            None if all.is_empty() => Ok(false),
            None => Err(corrupt()),
        }
    }

    pub(crate) fn reconcile_job_prune(
        &self,
        source_id: SourceId,
        job_id: JobId,
        database_paths: &[String],
    ) -> Result<(), ArtifactStoreError> {
        let prefix = job_prefix(source_id, job_id);
        let expected = expected_paths(&prefix, database_paths)?;
        let job = self.root.join(prefix.trim_end_matches('/'));
        let trash = self.root.join(".trash/jobs").join(job_id.to_string());
        if checked_directory(&trash)?.is_none() {
            return Ok(());
        }
        let actual = match checked_directory(&job)? {
            Some(()) => collect_job_files(&job, &prefix)?,
            None => BTreeSet::new(),
        };
        if actual == expected {
            return remove_trash(&trash);
        }
        let trashed = collect_job_files(&trash, &prefix)?;
        if !actual.is_subset(&expected) || trashed != expected {
            return Err(corrupt());
        }
        if checked_directory(&job)?.is_some() {
            fs::remove_dir_all(&job)?;
            sync_parent(&job)?;
        }
        fs::rename(&trash, &job)?;
        sync_parent(&trash)?;
        sync_parent(&job)
    }

    pub(crate) fn finish_job_prune(&self, job_id: JobId) -> Result<(), ArtifactStoreError> {
        let trash = self.root.join(".trash/jobs").join(job_id.to_string());
        if checked_directory(&trash)?.is_some() {
            remove_trash(&trash)?;
        }
        Ok(())
    }

    pub(crate) fn job_prune_trash(&self) -> Result<Vec<JobId>, ArtifactStoreError> {
        let root = self.root.join(".trash/jobs");
        checked_create(&self.root.join(".trash"))?;
        checked_create(&root)?;
        let mut jobs = Vec::new();
        for entry in fs::read_dir(root)? {
            let entry = entry?;
            checked_directory(&entry.path())?.ok_or_else(invalid)?;
            jobs.push(
                entry
                    .file_name()
                    .into_string()
                    .map_err(|_| invalid())?
                    .parse()
                    .map_err(|_| invalid())?,
            );
        }
        jobs.sort_unstable();
        Ok(jobs)
    }
}

fn expected_paths(prefix: &str, paths: &[String]) -> Result<BTreeSet<String>, ArtifactStoreError> {
    let expected = paths.iter().cloned().collect::<BTreeSet<_>>();
    if expected.len() != paths.len()
        || expected
            .iter()
            .any(|path| !path.starts_with(prefix) || validate_final_path(path).is_err())
    {
        return Err(corrupt());
    }
    Ok(expected)
}

fn collect_job_files(
    directory: &Path,
    prefix: &str,
) -> Result<BTreeSet<String>, ArtifactStoreError> {
    let mut files = BTreeSet::new();
    collect(directory, directory, prefix, &mut files)?;
    Ok(files)
}

fn collect(
    base: &Path,
    directory: &Path,
    prefix: &str,
    files: &mut BTreeSet<String>,
) -> Result<(), ArtifactStoreError> {
    for entry in fs::read_dir(directory)? {
        let entry = entry?;
        let entry_path = entry.path();
        let metadata = fs::symlink_metadata(&entry_path)?;
        if metadata.file_type().is_symlink() {
            return Err(invalid());
        }
        if metadata.is_dir() {
            collect(base, &entry_path, prefix, files)?;
        } else if metadata.is_file() {
            let suffix = entry_path
                .strip_prefix(base)
                .map_err(|_| invalid())?
                .to_str()
                .ok_or_else(invalid)?;
            if !files.insert(format!("{prefix}{suffix}")) {
                return Err(invalid());
            }
        } else {
            return Err(invalid());
        }
    }
    Ok(())
}

fn checked_create(path: &Path) -> Result<(), ArtifactStoreError> {
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
fn checked_file(path: &Path) -> Result<(), ArtifactStoreError> {
    let metadata = fs::symlink_metadata(path)?;
    if metadata.is_file() && !metadata.file_type().is_symlink() {
        Ok(())
    } else {
        Err(invalid())
    }
}
fn remove_trash(path: &Path) -> Result<(), ArtifactStoreError> {
    collect_job_files(path, "")?;
    fs::remove_dir_all(path)?;
    sync_parent(path)
}
fn rollback_stage(job: &Path, trash: &Path) -> Result<(), ArtifactStoreError> {
    if checked_directory(job)?.is_some() {
        collect_job_files(job, "")?;
        fs::remove_dir_all(job)?;
        sync_parent(job)?;
    }
    fs::rename(trash, job)?;
    sync_parent(trash)?;
    sync_parent(job)
}
fn sync_tree_directories(path: &Path) -> Result<(), ArtifactStoreError> {
    for entry in fs::read_dir(path)? {
        let entry = entry?;
        if fs::symlink_metadata(entry.path())?.is_dir() {
            sync_tree_directories(&entry.path())?;
        }
    }
    sync_directory(path)
}
fn job_prefix(source_id: SourceId, job_id: JobId) -> String {
    format!("sources/{source_id}/jobs/{job_id}/")
}
fn corrupt() -> ArtifactStoreError {
    ArtifactStoreError::with_code(ErrorCode::CorruptState)
}
