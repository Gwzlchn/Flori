use std::{fs, path::PathBuf};

use flori_core::{
    ArtifactId, ErrorCode, JobId, Sha256Digest, SourceId, SourceInputId, TaskId, UploadId,
    UploadState,
};
use flori_store::artifact::{
    ArtifactStoreError, NasArtifactStore, RecoveryAction, UploadRecord, retained_artifact_path,
    source_input_path, task_artifact_path,
};

const ABC_SHA256: &str = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

fn error_code<T>(result: Result<T, ArtifactStoreError>) -> ErrorCode {
    match result {
        Err(error) => error.code(),
        Ok(_) => panic!("expected artifact store error"),
    }
}

struct TestRoot(PathBuf);

impl TestRoot {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("flori-artifact-{}", UploadId::generate()));
        fs::create_dir(&path).expect("create isolated NAS root");
        Self(path)
    }
}

impl Drop for TestRoot {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("remove isolated NAS root");
    }
}

fn upload(root: &TestRoot) -> (NasArtifactStore, UploadRecord) {
    let store = NasArtifactStore::new(&root.0, 3).expect("open NAS store");
    let path = task_artifact_path(
        SourceId::generate(),
        JobId::generate(),
        TaskId::generate(),
        ArtifactId::generate(),
        "note.md",
    )
    .expect("server path");
    let record = upload_record("smart_note", path, 3, ABC_SHA256);
    (store, record)
}

fn upload_record(name: &str, path: impl Into<PathBuf>, size: u64, digest: &str) -> UploadRecord {
    record_result(name, path, size, digest, name, size).expect("validated upload record")
}

fn record_result(
    name: &str,
    path: impl Into<PathBuf>,
    size: u64,
    digest: &str,
    declared_name: &str,
    declared_max_size: u64,
) -> Result<UploadRecord, ArtifactStoreError> {
    UploadRecord::new(
        UploadId::generate(),
        name,
        path.into()
            .into_os_string()
            .into_string()
            .expect("UTF-8 path"),
        size,
        Sha256Digest::parse(digest).expect("digest"),
        declared_name,
        declared_max_size,
    )
}

fn stage_abc(store: &NasArtifactStore, record: &mut UploadRecord) {
    store.append(record, 0, b"a").expect("first append");
    store.append(record, 1, b"bc").expect("second append");
    store.verify_staging(record).expect("verify staging");
}

#[test]
fn server_paths_reject_unsafe_names_and_routes() {
    let source = SourceId::generate();
    assert!(source_input_path(source, SourceInputId::generate(), "paper.pdf").is_ok());
    assert!(retained_artifact_path(source, ArtifactId::generate(), "source.pdf").is_ok());
    assert!(
        task_artifact_path(
            source,
            JobId::generate(),
            TaskId::generate(),
            ArtifactId::generate(),
            "note.md"
        )
        .is_ok()
    );

    for name in [
        "/etc/passwd",
        "..",
        ".hidden",
        "nested/file",
        "nested\\file",
    ] {
        assert_eq!(
            error_code(source_input_path(source, SourceInputId::generate(), name)),
            ErrorCode::ArtifactInvalidPath
        );
    }

    for path in [
        "/absolute",
        "sources/../escape/file",
        "sources/id/.hidden/file",
    ] {
        assert_eq!(
            error_code(UploadRecord::new(
                UploadId::generate(),
                "smart_note",
                path,
                3,
                Sha256Digest::parse(ABC_SHA256).expect("digest"),
                "smart_note",
                3,
            )),
            ErrorCode::ArtifactInvalidPath
        );
    }
}

#[test]
fn declaration_and_exact_path_fail_before_staging_is_created() {
    let root = TestRoot::new();
    let _store = NasArtifactStore::new(&root.0, 3).expect("store");
    let valid_path = task_artifact_path(
        SourceId::generate(),
        JobId::generate(),
        TaskId::generate(),
        ArtifactId::generate(),
        "note.md",
    )
    .expect("valid path");

    assert_eq!(
        error_code(record_result(
            "summary",
            &valid_path,
            3,
            ABC_SHA256,
            "smart_note",
            3,
        )),
        ErrorCode::ArtifactUndeclared
    );
    assert_eq!(
        error_code(record_result(
            "smart_note",
            &valid_path,
            4,
            ABC_SHA256,
            "smart_note",
            3,
        )),
        ErrorCode::ArtifactTooLarge
    );
    assert_eq!(
        error_code(record_result(
            "smart_note",
            "sources/foo/bar",
            3,
            ABC_SHA256,
            "smart_note",
            3,
        )),
        ErrorCode::ArtifactInvalidPath
    );
    assert_eq!(
        error_code(record_result(
            "smart_note",
            format!("{valid_path}/extra"),
            3,
            ABC_SHA256,
            "smart_note",
            3,
        )),
        ErrorCode::ArtifactInvalidPath
    );
    assert!(!root.0.join(".staging").exists());
}

#[test]
fn append_enforces_offset_size_and_digest() {
    let root = TestRoot::new();
    let (store, mut record) = upload(&root);
    store.append(&mut record, 0, b"a").expect("first byte");
    assert_eq!(
        error_code(store.append(&mut record, 0, b"b")),
        ErrorCode::Conflict
    );
    assert_eq!(
        error_code(store.append(&mut record, 1, b"bcd")),
        ErrorCode::ArtifactTooLarge
    );
    assert_eq!(
        error_code(store.verify_staging(&record)),
        ErrorCode::DigestMismatch
    );

    store
        .append(&mut record, 1, b"zz")
        .expect("complete wrong content");
    assert_eq!(
        error_code(store.verify_staging(&record)),
        ErrorCode::DigestMismatch
    );
}

#[cfg(unix)]
#[test]
fn symlink_staging_is_rejected_without_writing_target() {
    use std::os::unix::fs::symlink;

    let root = TestRoot::new();
    let (store, mut record) = upload(&root);
    let target = root.0.join("outside");
    fs::write(&target, b"safe").expect("target");
    let staging = root.0.join(record.staging_relative_path());
    fs::create_dir_all(staging.parent().expect("parent")).expect("staging parent");
    symlink(&target, &staging).expect("malicious symlink");

    assert_eq!(
        error_code(store.append(&mut record, 0, b"abc")),
        ErrorCode::ArtifactInvalidPath
    );
    assert_eq!(fs::read(target).expect("target remains"), b"safe");
}

#[test]
fn verified_move_is_atomic_and_idempotent() {
    let root = TestRoot::new();
    let (store, mut record) = upload(&root);
    stage_abc(&store, &mut record);
    record
        .restore_progress(record.expected_size_bytes(), UploadState::Verified)
        .expect("verified ledger");

    store.move_verified(&record).expect("first rename");
    assert!(!root.0.join(record.staging_relative_path()).exists());
    assert_eq!(
        fs::read(root.0.join(record.final_relative_path())).expect("final bytes"),
        b"abc"
    );
    store
        .move_verified(&record)
        .expect("duplicate rename converges");
}

#[test]
fn recovery_covers_each_crash_boundary_without_scanning() {
    let root = TestRoot::new();
    let (store, mut record) = upload(&root);
    store
        .append(&mut record, 0, b"abc")
        .expect("receiving bytes");
    assert_eq!(
        store.recovery_action(&record, true).expect("receiving"),
        RecoveryAction::ResumeReceiving
    );

    record
        .restore_progress(record.expected_size_bytes(), UploadState::Verified)
        .expect("verified ledger");
    assert_eq!(
        store.recovery_action(&record, true).expect("before rename"),
        RecoveryAction::MoveVerified
    );
    store
        .move_verified(&record)
        .expect("rename before state update");
    assert_eq!(
        store.recovery_action(&record, true).expect("after rename"),
        RecoveryAction::MarkMoved
    );

    record
        .restore_progress(record.expected_size_bytes(), UploadState::Moved)
        .expect("moved ledger");
    assert_eq!(
        store
            .recovery_action(&record, true)
            .expect("after state update"),
        RecoveryAction::RetryCommit
    );
    assert_eq!(
        store
            .recovery_action(&record, false)
            .expect("invalid owner"),
        RecoveryAction::DeleteFilesThenLedger
    );
    fs::remove_file(root.0.join(record.final_relative_path())).expect("simulate cleanup");
    assert_eq!(
        store
            .recovery_action(&record, false)
            .expect("cleanup crash"),
        RecoveryAction::DeleteLedger
    );

    fs::write(root.0.join("unrelated"), b"abc").expect("unrelated matching file");
    assert_eq!(
        error_code(store.recovery_action(&record, true)),
        ErrorCode::CorruptState
    );
}

#[test]
fn recovery_rejects_two_files_and_mismatched_final() {
    let root = TestRoot::new();
    let (store, mut record) = upload(&root);
    stage_abc(&store, &mut record);
    record
        .restore_progress(record.expected_size_bytes(), UploadState::Verified)
        .expect("verified ledger");
    let final_path = root.0.join(record.final_relative_path());
    fs::create_dir_all(final_path.parent().expect("final parent")).expect("parent");
    fs::write(&final_path, b"abc").expect("second copy");
    assert_eq!(
        error_code(store.recovery_action(&record, true)),
        ErrorCode::CorruptState
    );

    fs::remove_file(root.0.join(record.staging_relative_path())).expect("remove staging");
    fs::write(final_path, b"bad").expect("bad final");
    assert_eq!(
        error_code(store.recovery_action(&record, true)),
        ErrorCode::CorruptState
    );
}
