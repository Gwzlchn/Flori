use super::*;

#[tokio::test]
async fn concurrent_poll_has_one_winner_and_secret_exists_only_in_claim() {
    let database = TestDatabase::new();
    let foundation = foundation(&database, &["media"]).await;
    let credential_value = "TEST_COOKIE_VALUE";
    attach_credential(
        &foundation.pool,
        foundation.source_id,
        "youtube_cookie",
        credential_value,
    )
    .await;
    sqlx::query("UPDATE sources SET kind='youtube_video' WHERE id=?")
        .bind(foundation.source_id.to_string())
        .execute(&foundation.pool)
        .await
        .expect("video source kind");
    let spec_json: String = sqlx::query_scalar("SELECT spec_json FROM tasks WHERE id=?")
        .bind(foundation.task_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("task spec");
    let mut spec: CompiledTaskSpec = serde_json::from_str(&spec_json).expect("strict spec");
    spec.executor = Executor::VideoAcquire;
    let bindings = TaskInputBindings::VideoAcquire {
        source: TaskInputReference::Source,
    };
    sqlx::query(
        "UPDATE tasks SET executor='video.acquire',spec_json=?,input_bindings_json=? WHERE id=?",
    )
    .bind(serde_json::to_string(&spec).expect("video spec"))
    .bind(serde_json::to_string(&bindings).expect("video bindings"))
    .bind(foundation.task_id.to_string())
    .execute(&foundation.pool)
    .await
    .expect("video acquire task");
    let store = Arc::new(foundation.store);
    let (left, right) = tokio::join!(
        store.poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example"),
        store.poll_and_claim(foundation.runner_id, 10, 70, "https://flori.example"),
    );
    let claims = [left.expect("left poll"), right.expect("right poll")];
    assert_eq!(claims.iter().filter(|claim| claim.is_some()).count(), 1);
    let claim = claims.into_iter().flatten().next().expect("winning claim");
    assert_eq!(claim.job_id, foundation.job_id);
    assert_eq!(claim.task_id, foundation.task_id);
    assert_eq!(claim.attempt_no, 1);
    assert_eq!(
        claim.secret_inputs.credential.expect("cookie").value,
        credential_value
    );
    assert!(matches!(
        claim.resolved_inputs,
        ResolvedTaskInputs::VideoAcquire { .. }
    ));
    let persisted: String =
        sqlx::query_scalar("SELECT spec_json || input_bindings_json FROM tasks WHERE id=?")
            .bind(foundation.task_id.to_string())
            .fetch_one(&foundation.pool)
            .await
            .expect("persisted task");
    assert!(!persisted.contains(credential_value));
    let attempts: i64 = sqlx::query_scalar("SELECT count(*) FROM attempts WHERE task_id=?")
        .bind(foundation.task_id.to_string())
        .fetch_one(&foundation.pool)
        .await
        .expect("attempt count");
    assert_eq!(attempts, 1);
}
