#[path = "public_detail_http/fixture.rs"]
mod fixture;

use fixture::{Harness, assert_error, body, status};
use flori_core::{ErrorCode, JobId, JobView, SourceView};

#[tokio::test]
async fn source_and_job_details_are_strict_complete_and_stably_ordered() {
    let harness = Harness::new().await;
    let response = harness
        .get(&format!("/api/v1/sources/{}", harness.source_id))
        .await;
    assert_eq!(status(&response), 200);
    let source: SourceView = serde_json::from_slice(body(&response)).expect("source detail");
    assert_eq!(source.current_job_id, Some(harness.current_job_id));
    assert_eq!(source.previous_job_id, Some(harness.previous_job_id));
    assert_eq!(source.canonical_ref, "upload:paper");

    let response = harness
        .get(&format!("/api/v1/jobs/{}", harness.current_job_id))
        .await;
    assert_eq!(status(&response), 200);
    let job: JobView = serde_json::from_slice(body(&response)).expect("job detail");
    assert_eq!(job.source_id, harness.source_id);
    assert!(!job.inputs.translate);
    assert_eq!(job.tasks.len(), 2);
    assert_eq!(job.tasks[0].task_key, "acquire");
    assert_eq!(job.tasks[1].task_key, "note");
    assert_eq!(job.tasks[1].spec.needs, ["acquire"]);
    assert_eq!(job.tasks[1].task_id, harness.note_task_id);
    assert_eq!(job.tasks[1].pinned_runner_id, Some(harness.runner_id));
    assert_eq!(job.tasks[1].selected_model.as_deref(), Some("model-a"));
    assert_eq!(job.tasks[1].selected_effort.as_deref(), Some("high"));
    assert_eq!(job.tasks[1].runner_config_revision, Some(7));
    assert_eq!(
        job.tasks[1].current_attempt_id,
        Some(harness.current_attempt_id)
    );
    assert_eq!(
        job.tasks[1]
            .attempts
            .iter()
            .map(|attempt| attempt.attempt_no)
            .collect::<Vec<_>>(),
        [1, 2]
    );
    assert_eq!(job.artifacts.len(), 2);
    assert_eq!(job.artifacts[0].name, "source");
    assert_eq!(job.artifacts[1].name, "smart_note");

    assert_error(
        &harness.get("/api/v1/sources/not-a-uuid").await,
        400,
        ErrorCode::InvalidRequest,
    );
    assert_error(
        &harness
            .get(&format!("/api/v1/jobs/{}", JobId::generate()))
            .await,
        404,
        ErrorCode::NotFound,
    );

    let mut connection = harness.pool.acquire().await.expect("fixture connection");
    sqlx::query("PRAGMA ignore_check_constraints=ON")
        .execute(&mut *connection)
        .await
        .expect("allow corrupt fixture");
    sqlx::query("UPDATE jobs SET state='unknown' WHERE id=?")
        .bind(harness.current_job_id.to_string())
        .execute(&mut *connection)
        .await
        .expect("corrupt job state");
    assert_error(
        &harness
            .get(&format!("/api/v1/jobs/{}", harness.current_job_id))
            .await,
        500,
        ErrorCode::CorruptState,
    );
    sqlx::query("UPDATE jobs SET state='succeeded' WHERE id=?")
        .bind(harness.current_job_id.to_string())
        .execute(&mut *connection)
        .await
        .expect("restore job state");
    sqlx::query("UPDATE tasks SET spec_json='{}' WHERE id=?")
        .bind(harness.note_task_id.to_string())
        .execute(&mut *connection)
        .await
        .expect("corrupt task spec");
    assert_error(
        &harness
            .get(&format!("/api/v1/jobs/{}", harness.current_job_id))
            .await,
        500,
        ErrorCode::CorruptState,
    );
}
