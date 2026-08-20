mod core_validation_support;

use std::{fs, path::Path};

use flori_core::ErrorCode;

use core_validation_support::{CrashPoint, Fixture, assert_reserved, digest, evidence_bytes};

#[tokio::test]
async fn core_driver_reserves_ledger_before_committing_evidence() {
    let fixture = Fixture::new().await;
    let before = file_count(&fixture.root);
    sqlx::query(
        "CREATE TRIGGER reject_validation_upload BEFORE INSERT ON uploads \
         BEGIN SELECT RAISE(ABORT,'ledger unavailable'); END",
    )
    .execute(&fixture.pool)
    .await
    .expect("failure trigger");

    assert!(
        fixture
            .store
            .drive_core_once(&fixture.artifacts(), 10)
            .await
            .is_err()
    );
    assert_eq!(file_count(&fixture.root), before);
    let state: String = sqlx::query_scalar("SELECT state FROM tasks WHERE id=?")
        .bind(fixture.validate_id.to_string())
        .fetch_one(&fixture.pool)
        .await
        .expect("validate state");
    assert_eq!(state, "ready");
    let uploads: i64 = sqlx::query_scalar("SELECT count(*) FROM uploads")
        .fetch_one(&fixture.pool)
        .await
        .expect("upload count");
    assert_eq!(uploads, 0);
}

#[tokio::test]
async fn core_validation_recovery_converges_every_file_commit_window() {
    for (point, state) in [
        (CrashPoint::LedgerOnly, "receiving"),
        (CrashPoint::FsyncAhead, "receiving"),
        (CrashPoint::Verified, "verified"),
        (CrashPoint::RenamedAhead, "verified"),
        (CrashPoint::Moved, "moved"),
    ] {
        let fixture = Fixture::new().await;
        let reserved = fixture.reserve(point).await;
        assert_reserved(&fixture, &reserved, state).await;

        fixture
            .store
            .reconcile_uploads(&fixture.artifacts(), 10)
            .await
            .expect("recover validation");
        fixture.assert_completed(&reserved).await;
    }
}

#[tokio::test]
async fn moved_validation_replays_after_database_failure_without_duplicate_artifact() {
    let fixture = Fixture::new().await;
    let reserved = fixture.reserve(CrashPoint::Moved).await;
    sqlx::query(
        "CREATE TRIGGER reject_evidence_artifact BEFORE INSERT ON artifacts \
         WHEN NEW.kind='evidence' BEGIN SELECT RAISE(ABORT,'commit unavailable'); END",
    )
    .execute(&fixture.pool)
    .await
    .expect("failure trigger");

    assert!(
        fixture
            .store
            .reconcile_uploads(&fixture.artifacts(), 10)
            .await
            .is_err()
    );
    assert_reserved(&fixture, &reserved, "moved").await;
    sqlx::query("DROP TRIGGER reject_evidence_artifact")
        .execute(&fixture.pool)
        .await
        .expect("drop trigger");
    fixture
        .store
        .reconcile_uploads(&fixture.artifacts(), 11)
        .await
        .expect("retry commit");
    fixture.assert_completed(&reserved).await;
    fixture
        .store
        .reconcile_uploads(&fixture.artifacts(), 12)
        .await
        .expect("idempotent replay");
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM artifacts WHERE attempt_id=?")
        .bind(reserved.attempt_id.to_string())
        .fetch_one(&fixture.pool)
        .await
        .expect("artifact count");
    assert_eq!(count, 1);
}

#[tokio::test]
async fn core_validation_recovery_fails_closed_on_digest_or_path_drift() {
    let digest_fixture = Fixture::new().await;
    let digest_reserved = digest_fixture.reserve(CrashPoint::Moved).await;
    let mut drifted = evidence_bytes();
    drifted[0] ^= 1;
    assert_ne!(digest(&drifted), digest(&digest_reserved.bytes));
    fs::write(
        digest_fixture.root.join(&digest_reserved.final_path),
        drifted,
    )
    .expect("drift final bytes");
    let error = digest_fixture
        .store
        .reconcile_uploads(&digest_fixture.artifacts(), 10)
        .await
        .expect_err("digest drift must fail");
    assert_eq!(error.code(), ErrorCode::CorruptState);
    assert_reserved(&digest_fixture, &digest_reserved, "moved").await;

    let path_fixture = Fixture::new().await;
    let path_reserved = path_fixture.reserve(CrashPoint::LedgerOnly).await;
    sqlx::query("UPDATE uploads SET final_relative_path=? WHERE id=?")
        .bind(format!("{}.drift", path_reserved.final_path))
        .bind(path_reserved.upload_id.to_string())
        .execute(&path_fixture.pool)
        .await
        .expect("drift ledger path");
    let error = path_fixture
        .store
        .reconcile_uploads(&path_fixture.artifacts(), 10)
        .await
        .expect_err("path drift must fail");
    assert_eq!(error.code(), ErrorCode::CorruptState);
    assert_reserved(&path_fixture, &path_reserved, "receiving").await;
}

fn file_count(path: &Path) -> usize {
    fs::read_dir(path)
        .expect("read artifact directory")
        .map(|entry| {
            let path = entry.expect("directory entry").path();
            if path.is_dir() { file_count(&path) } else { 1 }
        })
        .sum()
}
