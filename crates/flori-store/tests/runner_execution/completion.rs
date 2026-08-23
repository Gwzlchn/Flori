use super::*;

#[tokio::test]
async fn authenticated_completion_checks_required_usage_manifest_and_nas() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let artifacts =
        NasArtifactStore::new(database.directory.join("artifacts"), 1024).expect("artifact store");
    let claim = foundation
        .store
        .poll_and_claim(foundation.runner_id, 10, 100, "https://flori.example")
        .await
        .expect("poll")
        .expect("claim");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: manifest_digest(&foundation, claim.exec_id, Vec::new()),
                    },
                    11,
                )
                .await,
        ),
        ErrorCode::ArtifactUndeclared
    );
    let original = upload_bytes(
        &foundation,
        &artifacts,
        claim.exec_id,
        "original",
        "application/pdf",
        b"pdf",
        12,
    )
    .await;
    foundation
        .store
        .append_log_frames(
            &artifacts,
            foundation.runner_id,
            claim.exec_id,
            &[frame(1, "complete")],
            15,
        )
        .await
        .expect("task log");
    let expected = manifest_digest(&foundation, claim.exec_id, vec![&original]);
    sqlx::query(
        "INSERT INTO ai_usage(id,job_id,task_id,attempt_id,invocation_key,state,tool,model, \
         effort,created_at_ms) VALUES(?,?,?,?,?,'started','qoder_cli','test','high',18)",
    )
    .bind(AiUsageId::generate().to_string())
    .bind(foundation.job_id.to_string())
    .bind(foundation.task_id.to_string())
    .bind(claim.exec_id.to_string())
    .bind("open")
    .execute(&foundation.pool)
    .await
    .expect("open usage");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: expected.clone(),
                    },
                    19,
                )
                .await,
        ),
        ErrorCode::UsageConflict
    );
    sqlx::query("DELETE FROM ai_usage WHERE attempt_id=?")
        .bind(claim.exec_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("close usage fixture");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: digest("wrong"),
                    },
                    20,
                )
                .await,
        ),
        ErrorCode::DigestMismatch
    );
    let original_path = database
        .directory
        .join("artifacts")
        .join(&original.artifact.relative_path);
    fs::write(&original_path, b"bad").expect("mutate final artifact");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: expected.clone(),
                    },
                    21,
                )
                .await,
        ),
        ErrorCode::CorruptState
    );
    fs::write(&original_path, b"pdf").expect("restore final artifact");
    let request = CompleteAttemptRequest {
        manifest_sha256: expected,
    };
    assert_eq!(
        foundation
            .store
            .complete_authenticated_attempt(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &request,
                22,
            )
            .await
            .expect("complete")
            .state,
        AttemptState::Succeeded
    );
    let event_kinds: Vec<String> = sqlx::query_scalar(
        "SELECT kind FROM job_events WHERE scope='job' AND scope_id=? ORDER BY id",
    )
    .bind(foundation.job_id.to_string())
    .fetch_all(&foundation.pool)
    .await
    .expect("attempt events");
    assert_eq!(
        event_kinds,
        [
            "task_state",
            "job_state",
            "job_state",
            "task_state",
            "log_cursor",
            "artifact_committed",
            "artifact_committed",
            "task_state",
            "task_state",
        ]
    );
    sqlx::query(
        "UPDATE artifacts SET media_type='text/html' WHERE attempt_id=? AND name='original'",
    )
    .bind(claim.exec_id.to_string())
    .execute(&foundation.pool)
    .await
    .expect("forge committed media type");
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &request,
                    23,
                )
                .await,
        ),
        ErrorCode::CorruptState
    );
    sqlx::query(
        "UPDATE artifacts SET media_type='application/pdf' WHERE attempt_id=? AND name='original'",
    )
    .bind(claim.exec_id.to_string())
    .execute(&foundation.pool)
    .await
    .expect("restore committed media type");
    assert_eq!(
        foundation
            .store
            .complete_authenticated_attempt(
                &artifacts,
                foundation.runner_id,
                claim.exec_id,
                &request,
                24,
            )
            .await
            .expect("idempotent complete")
            .state,
        AttemptState::Succeeded
    );
    assert_eq!(
        store_error_code(
            foundation
                .store
                .complete_authenticated_attempt(
                    &artifacts,
                    foundation.runner_id,
                    claim.exec_id,
                    &CompleteAttemptRequest {
                        manifest_sha256: digest("different"),
                    },
                    25,
                )
                .await,
        ),
        ErrorCode::DigestMismatch
    );
}
