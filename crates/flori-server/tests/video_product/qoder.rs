use std::{
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

use flori_core::{AiResultEnvelope, JobId};
use sqlx::SqlitePool;

pub(super) struct QoderFixture {
    pub(super) executable: PathBuf,
    calls: PathBuf,
}

pub(super) fn invalid_then_repaired(root: &Path, repaired: &AiResultEnvelope) -> QoderFixture {
    let mut invalid = repaired.clone();
    let AiResultEnvelope::VideoNote { terms, .. } = &mut invalid else {
        panic!("video envelope");
    };
    terms.evidence_candidates[0].quote.push_str(" invalid");
    let primary = root.join("qoder-primary.json");
    let repair = root.join("qoder-repair.json");
    fs::write(&primary, result(&invalid)).expect("primary output");
    fs::write(&repair, result(repaired)).expect("repair output");
    let calls = root.join("qoder-calls");
    let executable = root.join("tools/qodercli");
    fs::write(
        &executable,
        format!(
            "#!/bin/sh\ncat >/dev/null\nn=0; [ ! -f '{calls}' ] || n=$(cat '{calls}'); n=$((n+1)); printf '%s' \"$n\" > '{calls}'; if [ \"$n\" -eq 1 ]; then cat '{primary}'; else cat '{repair}'; fi\n",
            calls = calls.display(),
            primary = primary.display(),
            repair = repair.display(),
        ),
    )
    .expect("Qoder script");
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700)).expect("Qoder executable");
    QoderFixture { executable, calls }
}

pub(super) async fn assert_repaired(pool: &SqlitePool, job_id: JobId, fixture: &QoderFixture) {
    assert_eq!(
        fs::read_to_string(&fixture.calls).expect("Qoder calls"),
        "2"
    );
    let invocations: Vec<(String, String)> = sqlx::query_as(
        "SELECT invocation_key,state FROM ai_usage WHERE job_id=? ORDER BY created_at_ms,id",
    )
    .bind(job_id.to_string())
    .fetch_all(pool)
    .await
    .expect("usage invocations");
    assert_eq!(
        invocations,
        vec![
            ("primary".into(), "final".into()),
            ("repair".into(), "final".into())
        ]
    );
}

fn result(envelope: &AiResultEnvelope) -> String {
    let result = serde_json::to_string(envelope).expect("envelope");
    format!(
        r#"{{"type":"result","subtype":"success","duration_ms":1,"duration_api_ms":1,"is_error":false,"num_turns":1,"result":{},"stop_reason":"end_turn","total_cost_usd":0,"total_credits":0.5,"usage":{{}},"modelUsage":{{}},"permission_denials":[],"fast_mode_state":"off","uuid":"fake","session_id":"fake"}}"#,
        serde_json::to_string(&result).expect("nested")
    )
}
