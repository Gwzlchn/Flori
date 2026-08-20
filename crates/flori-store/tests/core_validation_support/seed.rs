use std::{fs, path::Path, str::FromStr};

use flori_core::{
    ArtifactDeclaration, ArtifactId, ArtifactKind, ArtifactWhen, AttemptId, CompiledTaskSpec,
    DomainId, Executor, JobId, PipelineId, PipelineRevisionId, PromptSnapshotId, SourceId, TaskId,
};
use flori_store::artifact::task_artifact_path;
use sqlx::SqlitePool;

use super::{DOCUMENT, NOTE, SUMMARY, TERMS, digest};

pub(super) async fn foundation(
    pool: &SqlitePool,
    domain_id: DomainId,
    pipeline_id: PipelineId,
    revision_id: PipelineRevisionId,
    source_id: SourceId,
    job_id: JobId,
) {
    sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,'Domain','',0,0)")
        .bind(domain_id.to_string()).bind(format!("domain-{domain_id}")).execute(pool).await.expect("domain");
    sqlx::query("INSERT INTO pipelines(id,key,created_at_ms) VALUES(?,? ,0)")
        .bind(pipeline_id.to_string())
        .bind(format!("pipeline-{pipeline_id}"))
        .execute(pool)
        .await
        .expect("pipeline");
    sqlx::query("INSERT INTO pipeline_revisions(id,pipeline_id,compiler_version,git_commit,yaml_sha256,yaml_text,created_at_ms) VALUES(?,?,1,'test',?,'test: {}',0)")
        .bind(revision_id.to_string()).bind(pipeline_id.to_string()).bind("1".repeat(64)).execute(pool).await.expect("revision");
    sqlx::query("UPDATE pipelines SET current_revision_id=? WHERE id=?")
        .bind(revision_id.to_string())
        .bind(pipeline_id.to_string())
        .execute(pool)
        .await
        .expect("current revision");
    sqlx::query("INSERT INTO sources(id,kind,canonical_ref,domain_id,request_key,request_sha256,created_at_ms,updated_at_ms) VALUES(?,'pdf_url',?,?,?,?,0,0)")
        .bind(source_id.to_string()).bind(format!("https://example.test/{source_id}.pdf")).bind(domain_id.to_string()).bind(format!("source-{source_id}")).bind("2".repeat(64)).execute(pool).await.expect("source");
    sqlx::query("INSERT INTO jobs(id,source_id,pipeline_revision_id,trigger,state,prompt_snapshot_id,prompt_snapshot_sha256,prompt_snapshot_json,inputs_json,request_key,request_sha256,created_at_ms,started_at_ms) VALUES(?,?,?,'initial','running',?,?,'{}','{\"translate\":false}',?,?,0,0)")
        .bind(job_id.to_string()).bind(source_id.to_string()).bind(revision_id.to_string()).bind(PromptSnapshotId::generate().to_string()).bind("3".repeat(64)).bind(format!("job-{job_id}")).bind("4".repeat(64)).execute(pool).await.expect("job");
}

#[allow(clippy::too_many_arguments)]
pub(super) async fn task(
    pool: &SqlitePool,
    job_id: JobId,
    task_id: TaskId,
    key: &str,
    executor: Executor,
    state: &str,
    needs: Vec<String>,
    artifacts: Vec<ArtifactDeclaration>,
) {
    let spec = CompiledTaskSpec {
        executor,
        needs,
        tags: Vec::new(),
        retry: 0,
        timeout_ms: 1_000,
        artifacts,
    };
    let executor = serde_json::to_string(&executor).expect("executor");
    sqlx::query("INSERT INTO tasks(id,job_id,task_key,executor,spec_json,input_bindings_json,state,attempt_limit,timeout_ms,ready_at_ms) VALUES(?,?,?,?,?,'{}',?,1,1000,0)")
        .bind(task_id.to_string()).bind(job_id.to_string()).bind(key).bind(executor.trim_matches('"')).bind(serde_json::to_string(&spec).expect("spec")).bind(state).execute(pool).await.expect("task");
}

pub(super) async fn inputs(
    pool: &SqlitePool,
    root: &Path,
    source_id: SourceId,
    job_id: JobId,
    task_id: TaskId,
    attempt_id: AttemptId,
) {
    let values = [
        (ArtifactId::from_str("018f0000-0000-7000-8000-000000000001").expect("fixed artifact"), "original", "source_original", "application/pdf", "source.pdf", b"%PDF".as_slice()),
        (ArtifactId::generate(), "structure", "document_structure", "application/json", "document.json", DOCUMENT.as_bytes()),
        (ArtifactId::generate(), "smart_note", "smart_note", "text/markdown", "note.md", NOTE.as_bytes()),
        (ArtifactId::generate(), "summary", "summary", "text/markdown", "summary.md", SUMMARY.as_bytes()),
        (ArtifactId::generate(), "terms", "terms", "application/json", "terms.json", TERMS.as_bytes()),
    ];
    for (id, name, kind, media_type, file_name, body) in values {
        let relative = task_artifact_path(source_id, job_id, task_id, id, file_name).expect("path");
        let path = root.join(&relative);
        fs::create_dir_all(path.parent().expect("parent")).expect("parents");
        fs::write(path, body).expect("artifact bytes");
        sqlx::query("INSERT INTO artifacts(id,source_id,job_id,task_id,attempt_id,origin,name,kind,media_type,file_name,size_bytes,sha256,relative_path,retention,created_at_ms) VALUES(?,?,?,?,?,'produced',?,?,?,?,?,?,?,'published',0)")
            .bind(id.to_string()).bind(source_id.to_string()).bind(job_id.to_string()).bind(task_id.to_string()).bind(attempt_id.to_string()).bind(name).bind(kind).bind(media_type).bind(file_name).bind(body.len() as i64).bind(digest(body).as_str()).bind(relative).execute(pool).await.expect("artifact row");
    }
}

pub(super) fn evidence_declaration() -> ArtifactDeclaration {
    ArtifactDeclaration {
        name: "evidence".to_owned(),
        kind: ArtifactKind::Evidence,
        path: "output/evidence.json".to_owned(),
        required: true,
        when: ArtifactWhen::OnSuccess,
        max_files: None,
        max_bytes: 1024 * 1024,
    }
}
