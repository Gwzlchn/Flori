use std::{collections::BTreeSet, fs, net::SocketAddr, path::Path};

use flori_core::{
    AiAudit, AiTool, DocumentStructure, EvidenceLocator, EvidenceManifest, JobId, SourceId,
    TaskLogLine, TermsManifest,
};
use serde::Serialize;
use sha2::{Digest, Sha256};
use sqlx::SqlitePool;

use super::{fixture::ExpectedEvidence, http};

type ArtifactRow = (
    String,
    String,
    String,
    String,
    String,
    String,
    i64,
    String,
    String,
);

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
    ai_call_count: usize,
    usage: Vec<UsageReceipt>,
    artifacts: Vec<ArtifactReceipt>,
}

#[derive(Serialize)]
struct UsageReceipt {
    invocation_key: String,
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
        "SELECT a.id,a.task_id,t.task_key,a.name,a.kind,a.media_type,a.size_bytes,a.sha256,a.relative_path FROM artifacts a JOIN tasks t ON t.id=a.task_id WHERE a.job_id=? ORDER BY t.task_key,a.name",
    )
    .bind(context.job_id.to_string())
    .fetch_all(context.pool)
    .await
    .expect("Job artifacts");
    let mut artifacts = Vec::with_capacity(rows.len());
    let mut evidence = None;
    let mut kinds = Vec::new();
    for (id, task_id, task, name, kind, media, size, sha256, relative) in rows {
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
        http::verify_public_artifact(
            context,
            (&id, &task_id, &name, &kind, &media, size, &sha256),
            &bytes,
        )
        .await;
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
    let quality_invocations = matches!(context.mode, VerifyMode::Real)
        .then(|| verify_attention_quality(&artifacts, &evidence));

    http::verify_search_and_evidence(context, &evidence).await;
    let usage = usage(context.pool, context.job_id).await;
    assert!(usage.iter().all(|row| row.tool == "qoder_cli"
        && row.state == "final"
        && row.input_tokens.is_none()
        && row.output_tokens.is_none()));
    match context.mode {
        VerifyMode::Fake { .. } => {
            assert_eq!(usage.len(), 1);
            assert_eq!(usage[0].credits_micros, Some(1_250_000));
        }
        VerifyMode::Real => {
            assert_eq!(Some(usage.len()), quality_invocations);
            assert!((1..=2).contains(&usage.len()));
            assert!(
                usage
                    .iter()
                    .all(|row| row.credits_micros.is_some_and(|credits| credits > 0))
            );
        }
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
        ai_call_count: usage.len(),
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

fn verify_attention_quality(artifacts: &[ArtifactReceipt], evidence: &EvidenceManifest) -> usize {
    let bytes = |kind| {
        let path = &artifacts
            .iter()
            .find(|artifact| artifact.kind == kind)
            .unwrap_or_else(|| panic!("missing quality Artifact {kind}"))
            .absolute_path;
        fs::read(path).unwrap_or_else(|error| panic!("read {kind}: {error}"))
    };
    let document: DocumentStructure =
        serde_json::from_slice(&bytes("document_structure")).expect("quality document");
    let terms: TermsManifest = serde_json::from_slice(&bytes("terms")).expect("quality terms");
    let audit: AiAudit = serde_json::from_slice(&bytes("ai_audit")).expect("quality audit");
    let note = String::from_utf8(bytes("smart_note")).expect("quality smart note");
    let summary = String::from_utf8(bytes("summary")).expect("quality summary");

    for heading in [
        "### 研究背景、问题与贡献",
        "### 方法与整体设计",
        "### 核心机制与工作流程",
        "### 训练或评估设计",
        "### 主要结果",
        "### Figure 与 Table 解读",
        "### 局限性、适用边界与未决问题",
    ] {
        assert!(note.contains(heading), "quality note missing {heading}");
    }
    for topic in [
        &["Self-Attention", "自注意力"][..],
        &["Scaled Dot-Product Attention", "缩放点积注意力"][..],
        &["Multi-Head Attention", "多头注意力"][..],
        &["Positional Encoding", "位置编码"][..],
    ] {
        assert!(topic.iter().any(|term| note.contains(term)));
    }
    assert!(note.chars().count() >= 1_800 && chinese_chars(&note) >= 500);
    assert!(summary.chars().count() >= 180 && chinese_chars(&summary) >= 80);
    assert!(terms.terms.len() >= 6);
    assert!(
        terms
            .terms
            .iter()
            .all(|term| chinese_chars(&term.explanation) >= 8)
    );
    assert!(evidence.items.len() >= 6);
    let pages = evidence
        .items
        .iter()
        .filter_map(|item| match item.locator {
            EvidenceLocator::Pdf { page, .. } => Some(page),
            EvidenceLocator::Video { .. } => None,
        })
        .collect::<BTreeSet<_>>();
    let quotes = evidence
        .items
        .iter()
        .map(|item| item.quote.split_whitespace().collect::<String>())
        .collect::<BTreeSet<_>>();
    assert!(pages.len() >= 3 && quotes.len() >= 6);
    for needles in [
        &["encoder", "decoder", "model architecture"][..],
        &[
            "scaled dot-product attention",
            "multi-head attention",
            "self-attention",
        ][..],
        &["bleu", "wmt 2014", "outperforms", "table 3"][..],
    ] {
        assert!(evidence_has(&evidence.items, needles));
    }
    assert!(document.figures.iter().any(|figure| {
        evidence
            .items
            .iter()
            .any(|entry| pdf_source_matches(entry, figure.page, &figure.bbox, &figure.caption))
    }));
    assert!(
        document.tables.iter().any(
            |table| evidence.items.iter().any(|entry| pdf_source_matches(
                entry,
                table.page,
                &table.bbox,
                &table.caption
            ) || pdf_source_matches(
                entry,
                table.page,
                &table.bbox,
                &table.text
            ))
        )
    );
    assert!((1..=2).contains(&audit.usage_invocation_keys.len()));
    audit.usage_invocation_keys.len()
}

fn evidence_has(items: &[flori_core::EvidenceEntry], needles: &[&str]) -> bool {
    items.iter().any(|item| {
        let quote = item.quote.to_ascii_lowercase();
        needles.iter().any(|needle| quote.contains(needle))
    })
}

fn pdf_source_matches(
    entry: &flori_core::EvidenceEntry,
    page: u32,
    bbox: &flori_core::PdfRect,
    source: &str,
) -> bool {
    matches!(&entry.locator, EvidenceLocator::Pdf { page: actual, bbox: actual_bbox }
        if *actual == page && actual_bbox == bbox)
        && entry.quote.split_whitespace().eq(source.split_whitespace())
}

fn chinese_chars(value: &str) -> usize {
    value
        .chars()
        .filter(|character| matches!(character, '\u{3400}'..='\u{9fff}'))
        .count()
}

async fn usage(pool: &SqlitePool, job_id: JobId) -> Vec<UsageReceipt> {
    sqlx::query_as::<_, (String, String, String, String, String, Option<i64>, Option<i64>, Option<i64>)>(
        "SELECT invocation_key,tool,model,effort,state,input_tokens,output_tokens,credits_micros FROM ai_usage WHERE job_id=? ORDER BY id",
    )
    .bind(job_id.to_string())
    .fetch_all(pool)
    .await
    .expect("AI usage")
    .into_iter()
    .map(|row| UsageReceipt { invocation_key: row.0, tool: row.1, model: row.2, effort: row.3, state: row.4, input_tokens: row.5, output_tokens: row.6, credits_micros: row.7 })
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
