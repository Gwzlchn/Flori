use std::{fs, net::SocketAddr, path::Path};

use flori_core::{
    AiAudit, AiTool, DocumentStructure, EvidenceManifest, JobId, SourceId, TaskLogLine,
    TermsManifest,
};
use serde::Serialize;
use sha2::{Digest, Sha256};
use sqlx::SqlitePool;

use super::{fixture::ExpectedEvidence, http};

type ArtifactRow = (String, String, String, String, String, i64, String, String);

pub(super) struct VerifyContext<'a> {
    pub(super) pool: &'a SqlitePool,
    pub(super) sqlite_path: &'a Path,
    pub(super) artifact_root: &'a Path,
    pub(super) address: SocketAddr,
    pub(super) source_id: SourceId,
    pub(super) job_id: JobId,
    pub(super) mode: VerifyMode<'a>,
    pub(super) media_log: &'a Path,
    pub(super) secrets: [&'a str; 2],
}

#[derive(Clone, Copy)]
pub(super) enum VerifyMode<'a> {
    Fake {
        expected: &'a ExpectedEvidence,
        captured_prompt: &'a Path,
    },
    Real,
}

#[derive(Serialize)]
struct Receipt {
    schema: &'static str,
    sqlite_path: String,
    nas_root: String,
    receipt_path: String,
    source_id: SourceId,
    job_id: JobId,
    source_input_path: String,
    usage: Vec<UsageReceipt>,
    artifacts: Vec<ArtifactReceipt>,
}

#[derive(Serialize)]
struct UsageReceipt {
    tool: String,
    model: String,
    effort: String,
    state: String,
    input_tokens: Option<i64>,
    output_tokens: Option<i64>,
    credits_micros: Option<i64>,
}

#[derive(Serialize)]
struct ArtifactReceipt {
    artifact_id: String,
    task_key: String,
    name: String,
    kind: String,
    media_type: String,
    size_bytes: i64,
    sha256: String,
    absolute_path: String,
}

pub(super) async fn load_document(
    pool: &SqlitePool,
    artifact_root: &Path,
    job_id: JobId,
) -> DocumentStructure {
    let relative: String = sqlx::query_scalar(
        "SELECT relative_path FROM artifacts WHERE job_id=? AND kind='document_structure'",
    )
    .bind(job_id.to_string())
    .fetch_one(pool)
    .await
    .expect("document_structure artifact");
    let document: DocumentStructure = serde_json::from_slice(
        &fs::read(artifact_root.join(relative)).expect("read document_structure"),
    )
    .expect("strict DocumentStructure");
    document.validate().expect("valid DocumentStructure");
    assert!(!document.figures.is_empty(), "fixture must yield a Figure");
    assert!(
        !document.tables.is_empty(),
        "fixture must yield a Table region"
    );
    document
}

pub(super) async fn verify_and_write_receipt(context: &VerifyContext<'_>) -> String {
    let state: (String, Option<String>, Option<String>) = sqlx::query_as(
        "SELECT j.state,s.current_job_id,s.previous_job_id FROM jobs j JOIN sources s ON s.id=j.source_id WHERE j.id=?",
    )
    .bind(context.job_id.to_string())
    .fetch_one(context.pool)
    .await
    .expect("published Job");
    assert_eq!(
        state,
        ("succeeded".into(), Some(context.job_id.to_string()), None)
    );
    let uploads: i64 = sqlx::query_scalar("SELECT count(*) FROM uploads")
        .fetch_one(context.pool)
        .await
        .expect("upload ledger count");
    assert_eq!(
        uploads, 0,
        "successful chain must close every upload ledger"
    );

    let source_input: String =
        sqlx::query_scalar("SELECT relative_path FROM source_inputs WHERE source_id=?")
            .bind(context.source_id.to_string())
            .fetch_one(context.pool)
            .await
            .expect("source input path");
    let source_input = canonical(&context.artifact_root.join(source_input));
    let rows: Vec<ArtifactRow> = sqlx::query_as(
        "SELECT a.id,t.task_key,a.name,a.kind,a.media_type,a.size_bytes,a.sha256,a.relative_path FROM artifacts a JOIN tasks t ON t.id=a.task_id WHERE a.job_id=? ORDER BY t.task_key,a.name",
    )
    .bind(context.job_id.to_string())
    .fetch_all(context.pool)
    .await
    .expect("Job artifacts");
    let mut artifacts = Vec::with_capacity(rows.len());
    let mut evidence = None;
    let mut kinds = Vec::new();
    for (id, task, name, kind, media, size, sha256, relative) in rows {
        let path = context.artifact_root.join(&relative);
        let bytes = fs::read(&path).expect("read Artifact bytes");
        assert_eq!(i64::try_from(bytes.len()).expect("Artifact size"), size);
        assert_eq!(digest(&bytes), sha256);
        assert!(
            !fs::symlink_metadata(&path)
                .expect("Artifact metadata")
                .file_type()
                .is_symlink()
        );
        strict_content(&kind, &bytes, &mut evidence);
        kinds.push((kind.clone(), name.clone()));
        artifacts.push(ArtifactReceipt {
            artifact_id: id,
            task_key: task,
            name,
            kind,
            media_type: media,
            size_bytes: size,
            sha256,
            absolute_path: canonical(&path),
        });
    }
    for required in [
        "source_original",
        "document_structure",
        "figure",
        "table_region",
        "smart_note",
        "summary",
        "terms",
        "ai_audit",
        "evidence",
    ] {
        assert!(
            kinds.iter().any(|(kind, _)| kind == required),
            "missing {required}"
        );
    }
    let evidence = evidence.expect("canonical evidence Artifact");
    assert!(
        !evidence.items.is_empty(),
        "published evidence must not be empty"
    );
    if let VerifyMode::Fake { expected, .. } = context.mode {
        let entry = evidence
            .items
            .iter()
            .find(|entry| entry.evidence_id == expected.evidence_id)
            .expect("expected canonical evidence");
        assert_eq!(entry.source_artifact_id, expected.source_artifact_id);
        assert_eq!(entry.locator, expected.locator);
        assert_eq!(entry.quote, expected.quote);
    }

    http::verify_search_and_evidence(context, &evidence).await;
    let usage = usage(context.pool, context.job_id).await;
    assert_eq!(usage.len(), 1);
    assert_eq!(usage[0].tool, "qoder_cli");
    assert_eq!(usage[0].state, "final");
    assert_eq!(
        (usage[0].input_tokens, usage[0].output_tokens),
        (None, None)
    );
    match context.mode {
        VerifyMode::Fake { .. } => assert_eq!(usage[0].credits_micros, Some(1_250_000)),
        VerifyMode::Real => assert!(usage[0].credits_micros.is_some_and(|credits| credits > 0)),
    }
    assert_no_secrets(context, &artifacts);
    let receipt_path = context
        .sqlite_path
        .parent()
        .expect("receipt root")
        .join("receipt.json");
    let receipt = Receipt {
        schema: "flori.pdf_product_receipt.v1",
        sqlite_path: canonical(context.sqlite_path),
        nas_root: canonical(context.artifact_root),
        receipt_path: receipt_path.display().to_string(),
        source_id: context.source_id,
        job_id: context.job_id,
        source_input_path: source_input,
        usage,
        artifacts,
    };
    let compact = serde_json::to_string(&receipt).expect("serialize receipt");
    fs::write(
        &receipt_path,
        serde_json::to_vec_pretty(&receipt).expect("pretty receipt"),
    )
    .expect("write receipt");
    compact
}

fn strict_content(kind: &str, bytes: &[u8], evidence: &mut Option<EvidenceManifest>) {
    match kind {
        "document_structure" => {
            serde_json::from_slice::<DocumentStructure>(bytes).expect("strict document_structure");
        }
        "terms" => {
            serde_json::from_slice::<TermsManifest>(bytes).expect("strict terms");
        }
        "evidence" => {
            *evidence = Some(serde_json::from_slice(bytes).expect("strict evidence"));
        }
        "ai_audit" => {
            let audit: AiAudit = serde_json::from_slice(bytes).expect("strict AI audit");
            assert_eq!(audit.tool, AiTool::QoderCli);
        }
        "task_log" => {
            for line in std::str::from_utf8(bytes).expect("UTF-8 log").lines() {
                serde_json::from_str::<TaskLogLine>(line).expect("strict TaskLogLine");
            }
        }
        "figure" | "table_region" => assert!(bytes.starts_with(b"\x89PNG\r\n\x1a\n")),
        "source_original" => assert!(bytes.starts_with(b"%PDF-")),
        "smart_note" => {
            let note = std::str::from_utf8(bytes).expect("UTF-8 smart note");
            assert!(note.contains("## 来源事实") && note.contains("## AI 分析"));
        }
        "summary" => assert!(std::str::from_utf8(bytes).unwrap().contains("[[evidence:")),
        other => panic!("unexpected Artifact kind in PDF Job: {other}"),
    }
}

async fn usage(pool: &SqlitePool, job_id: JobId) -> Vec<UsageReceipt> {
    sqlx::query_as::<_, (String, String, String, String, Option<i64>, Option<i64>, Option<i64>)>(
        "SELECT tool,model,effort,state,input_tokens,output_tokens,credits_micros FROM ai_usage WHERE job_id=? ORDER BY id",
    )
    .bind(job_id.to_string())
    .fetch_all(pool)
    .await
    .expect("AI usage")
    .into_iter()
    .map(|row| UsageReceipt { tool: row.0, model: row.1, effort: row.2, state: row.3, input_tokens: row.4, output_tokens: row.5, credits_micros: row.6 })
    .collect()
}

fn assert_no_secrets(context: &VerifyContext<'_>, artifacts: &[ArtifactReceipt]) {
    let mut surfaces = vec![fs::read(context.media_log).unwrap_or_default()];
    if let VerifyMode::Fake {
        captured_prompt, ..
    } = context.mode
    {
        surfaces.push(fs::read(captured_prompt).expect("captured Qoder prompt"));
    }
    surfaces.extend(
        artifacts
            .iter()
            .map(|artifact| fs::read(&artifact.absolute_path).unwrap()),
    );
    assert!(
        context
            .secrets
            .iter()
            .all(|secret| surfaces.iter().all(|surface| !surface
                .windows(secret.len())
                .any(|window| window == secret.as_bytes())))
    );
}

fn digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn canonical(path: &Path) -> String {
    fs::canonicalize(path)
        .expect("canonical output path")
        .display()
        .to_string()
}
