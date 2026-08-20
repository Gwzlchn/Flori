use flori_core::{
    ArtifactId, ArtifactKind, ArtifactManifestEntry, AttemptId, PendingAttemptUpload, UploadId,
    UploadState,
};
use flori_store::artifact::{UploadRecord, task_artifact_path};

use super::{CrashPoint, Fixture, Reserved, digest, evidence_bytes, seed};

impl Fixture {
    pub(super) async fn reserve(&self, point: CrashPoint) -> Reserved {
        let bytes = evidence_bytes();
        let sha256 = digest(&bytes);
        let attempt_id = AttemptId::generate();
        let upload_id = UploadId::generate();
        let artifact_id = ArtifactId::generate();
        let final_path = task_artifact_path(
            self.source_id,
            self.job_id,
            self.validate_id,
            artifact_id,
            "evidence.json",
        )
        .expect("evidence path");
        let pending = PendingAttemptUpload {
            artifact_id,
            declaration_name: "evidence".to_owned(),
            artifact: ArtifactManifestEntry {
                name: "evidence".to_owned(),
                kind: ArtifactKind::Evidence,
                media_type: "application/json".to_owned(),
                size_bytes: bytes.len() as u64,
                sha256: sha256.clone(),
                relative_path: final_path.clone(),
            },
        };
        let mut record = UploadRecord::new(
            upload_id,
            "evidence",
            &final_path,
            bytes.len() as u64,
            sha256.clone(),
            "evidence",
            seed::evidence_declaration().max_bytes,
        )
        .expect("upload record");

        let mut transaction = self.pool.begin().await.expect("transaction");
        sqlx::query(
            "INSERT INTO attempts(id,task_id,attempt_no,runner_id,state,lease_expires_at_ms, \
             last_log_sequence,started_at_ms) VALUES(?,?,1,NULL,'leased',0,0,1)",
        )
        .bind(attempt_id.to_string())
        .bind(self.validate_id.to_string())
        .execute(&mut *transaction)
        .await
        .expect("core attempt");
        sqlx::query(
            "UPDATE tasks SET state='leased',current_attempt_id=?,started_at_ms=1 WHERE id=?",
        )
        .bind(attempt_id.to_string())
        .bind(self.validate_id.to_string())
        .execute(&mut *transaction)
        .await
        .expect("leased task");
        sqlx::query(
            "INSERT INTO uploads(id,owner_kind,owner_id,commit_json,name,target_id,staging_path, \
             final_relative_path,expected_size_bytes,expected_sha256,received_bytes,state, \
             created_at_ms,updated_at_ms) VALUES(?,'attempt',?,?,?,?,?,?,?,?,0,'receiving',1,1)",
        )
        .bind(upload_id.to_string())
        .bind(attempt_id.to_string())
        .bind(serde_json::to_string(&pending).expect("pending json"))
        .bind("evidence")
        .bind(artifact_id.to_string())
        .bind(record.staging_relative_path().to_string_lossy().as_ref())
        .bind(&final_path)
        .bind(bytes.len() as i64)
        .bind(sha256.as_str())
        .execute(&mut *transaction)
        .await
        .expect("upload ledger");
        transaction.commit().await.expect("reserve commit");

        if !matches!(point, CrashPoint::LedgerOnly) {
            let artifacts = self.artifacts();
            artifacts
                .append(&mut record, 0, &bytes)
                .expect("staging write");
            artifacts.verify_staging(&record).expect("staging digest");
            if !matches!(point, CrashPoint::FsyncAhead) {
                sqlx::query(
                    "UPDATE uploads SET received_bytes=?,state='verified',updated_at_ms=2 WHERE id=?",
                )
                .bind(bytes.len() as i64)
                .bind(upload_id.to_string())
                .execute(&self.pool)
                .await
                .expect("verified ledger");
                record
                    .restore_progress(bytes.len() as u64, UploadState::Verified)
                    .expect("verified record");
                if matches!(point, CrashPoint::RenamedAhead | CrashPoint::Moved) {
                    artifacts.move_verified(&record).expect("move final");
                    if matches!(point, CrashPoint::Moved) {
                        sqlx::query("UPDATE uploads SET state='moved',updated_at_ms=3 WHERE id=?")
                            .bind(upload_id.to_string())
                            .execute(&self.pool)
                            .await
                            .expect("moved ledger");
                    }
                }
            }
        }
        Reserved {
            attempt_id,
            upload_id,
            final_path,
            bytes,
        }
    }
}

pub(super) async fn assert_reserved(fixture: &Fixture, reserved: &Reserved, state: &str) {
    let upload: (String, i64) =
        sqlx::query_as("SELECT state,received_bytes FROM uploads WHERE id=?")
            .bind(reserved.upload_id.to_string())
            .fetch_one(&fixture.pool)
            .await
            .expect("reserved upload");
    assert_eq!(upload.0, state);
    let task: (String, String) =
        sqlx::query_as("SELECT state,current_attempt_id FROM tasks WHERE id=?")
            .bind(fixture.validate_id.to_string())
            .fetch_one(&fixture.pool)
            .await
            .expect("leased task");
    assert_eq!(task, ("leased".to_owned(), reserved.attempt_id.to_string()));
    let artifacts: i64 = sqlx::query_scalar("SELECT count(*) FROM artifacts WHERE attempt_id=?")
        .bind(reserved.attempt_id.to_string())
        .fetch_one(&fixture.pool)
        .await
        .expect("artifact count");
    assert_eq!(artifacts, 0);
    if state == "receiving" && upload.1 == 0 {
        assert!(!fixture.root.join(&reserved.final_path).exists());
    }
}
