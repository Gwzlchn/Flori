use std::{
    fmt::Write as _,
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

use flori_core::{
    AiModelCapability, AiResultEnvelope, AiResultSchema, ArtifactId, CreateRunnerSlot,
    DocumentStructure, DomainId, EvidenceEntry, EvidenceId, EvidenceLocator, Executor, PdfRect,
    PipelineId, PipelineRevisionId, RegisterRunnerRequest, RunnerId, RunnerTool,
    RunnerToolCapability, Sha256Digest, TermEntry, TermsManifest, TermsManifestSchema, UsageUpdate,
};
use flori_pipeline::compile;
use flori_runner::qoder_parse_result;
use flori_store::Store;
use sha2::{Digest, Sha256};
use sqlx::SqlitePool;

pub(super) const MEDIA_REGISTRATION: &str = "pdf-product-media-registration";
pub(super) const QODER_REGISTRATION: &str = "pdf-product-qoder-registration";
pub(super) const MODEL: &str = "fake-qoder-model";
pub(super) const EFFORT: &str = "high";

#[derive(Clone)]
pub(super) struct ExpectedEvidence {
    pub(super) evidence_id: EvidenceId,
    pub(super) source_artifact_id: ArtifactId,
    pub(super) locator: EvidenceLocator,
    pub(super) quote: String,
}

pub(super) struct FakeQoder {
    pub(super) executable: PathBuf,
    pub(super) home: PathBuf,
    pub(super) config: PathBuf,
    pub(super) work: PathBuf,
    pub(super) captured_prompt: PathBuf,
}

pub(super) fn note(document: &DocumentStructure) -> (AiResultEnvelope, ExpectedEvidence) {
    let block = document
        .sections
        .iter()
        .flat_map(|section| &section.blocks)
        .find(|block| block.text.to_ascii_lowercase().contains("evidence"))
        .or_else(|| {
            document
                .sections
                .iter()
                .flat_map(|section| &section.blocks)
                .next()
        })
        .expect("extracted document text block");
    let expected = ExpectedEvidence {
        evidence_id: EvidenceId::generate(),
        source_artifact_id: document.source_artifact_id,
        locator: EvidenceLocator::Pdf {
            page: block.page,
            bbox: block.bbox.clone(),
        },
        quote: block.text.clone(),
    };
    (envelope(&expected), expected)
}

pub(super) fn write_qoder(root: &Path, envelope: &AiResultEnvelope) -> FakeQoder {
    let output = root.join("fake-qoder-output.json");
    let captured_prompt = root.join("fake-qoder.stdin");
    fs::write(&output, qoder_output(envelope)).expect("write fake Qoder output");
    let executable = root.join("fake-qoder");
    let output = safe_shell_path(&output);
    let capture = safe_shell_path(&captured_prompt);
    fs::write(
        &executable,
        format!("#!/bin/sh\n/bin/cat > {capture}\n/bin/cat {output}\n"),
    )
    .expect("write fake Qoder executable");
    fs::set_permissions(&executable, fs::Permissions::from_mode(0o700))
        .expect("make fake Qoder executable");
    let home = root.join("qoder-home");
    let config = root.join("qoder-config");
    let work = root.join("qoder-work");
    for directory in [&home, &config, &work] {
        fs::create_dir(directory).expect("create fake Qoder directory");
    }
    FakeQoder {
        executable,
        home,
        config,
        work,
        captured_prompt,
    }
}

pub(super) async fn seed(
    store: &Store,
    pool: &SqlitePool,
) -> (DomainId, PipelineId, RunnerId, RunnerId) {
    let domain_id = DomainId::generate();
    sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,'PDF','Evidence-first research.',0,0)")
        .bind(domain_id.to_string()).bind(format!("pdf-{domain_id}")).execute(pool).await.expect("domain");
    let prompt = "Generate a readable note, summary, terms, and exact PDF evidence.";
    sqlx::query(
        "INSERT INTO prompts(key,content,sha256,updated_at_ms) VALUES('document_note',?,?,0)",
    )
    .bind(prompt)
    .bind(digest(prompt.as_bytes()).as_str())
    .execute(pool)
    .await
    .expect("prompt");
    let yaml = include_str!("../../../../pipelines/pdf.yml");
    let compilation = compile("pdf", yaml.as_bytes()).expect("frozen PDF Pipeline");
    let pipeline_id = PipelineId::generate();
    store
        .register_pipeline_revision(
            pipeline_id,
            PipelineRevisionId::generate(),
            &compilation,
            "4305cdd",
            yaml,
            0,
        )
        .await
        .expect("Pipeline revision");
    let media = store
        .create_runner_slot(
            &CreateRunnerSlot {
                name: "pdf-product-media".into(),
                tags: vec!["media".into()],
                max_concurrency: 1,
                default_model: None,
                default_effort: None,
            },
            &digest(MEDIA_REGISTRATION.as_bytes()),
            i64::MAX,
            1,
        )
        .await
        .expect("media Runner slot");
    let qoder = store
        .create_runner_slot(
            &CreateRunnerSlot {
                name: "pdf-product-qoder".into(),
                tags: vec!["ai".into()],
                max_concurrency: 1,
                default_model: Some(MODEL.into()),
                default_effort: Some(EFFORT.into()),
            },
            &digest(QODER_REGISTRATION.as_bytes()),
            i64::MAX,
            1,
        )
        .await
        .expect("Qoder Runner slot");
    (domain_id, pipeline_id, media, qoder)
}

pub(super) fn media_capabilities() -> RegisterRunnerRequest {
    RegisterRunnerRequest {
        tools: vec![RunnerToolCapability {
            tool: RunnerTool::PdfExtractor,
            version: "1.27.2.3".into(),
        }],
        ai_models: Vec::new(),
    }
}

pub(super) fn qoder_capabilities() -> RegisterRunnerRequest {
    RegisterRunnerRequest {
        tools: vec![RunnerToolCapability {
            tool: RunnerTool::QoderCli,
            version: flori_runner::QODERCLI_VERSION.into(),
        }],
        ai_models: vec![AiModelCapability {
            model: MODEL.into(),
            efforts: vec![EFFORT.into()],
        }],
    }
}

pub(super) fn digest(bytes: &[u8]) -> Sha256Digest {
    let mut value = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut value, "{byte:02x}").expect("String write");
    }
    Sha256Digest::parse(value).expect("canonical SHA-256")
}

fn envelope(expected: &ExpectedEvidence) -> AiResultEnvelope {
    let marker = format!("[[evidence:{}]]", expected.evidence_id);
    AiResultEnvelope::DocumentNote {
        schema: AiResultSchema::V1,
        smart_note_markdown: format!(
            "# PDF note\n\n## 来源事实\n\n{} {marker}\n\n## AI 分析\n\nThe source demonstrates a verifiable artifact pipeline.\n",
            expected.quote
        ),
        summary_markdown: format!("{} {marker}\n", expected.quote),
        terms: TermsManifest {
            schema: TermsManifestSchema::V1,
            terms: vec![TermEntry {
                term: "Evidence".into(),
                explanation: "A claim linked to an exact source location.".into(),
                evidence_ids: vec![expected.evidence_id],
            }],
            evidence_candidates: vec![EvidenceEntry {
                evidence_id: expected.evidence_id,
                source_artifact_id: expected.source_artifact_id,
                locator: expected.locator.clone(),
                quote: expected.quote.clone(),
            }],
        },
    }
}

fn qoder_output(envelope: &AiResultEnvelope) -> String {
    let result = serde_json::to_string(envelope).expect("serialize AI result envelope");
    format!(
        r#"{{"type":"result","subtype":"success","duration_ms":1,"duration_api_ms":1,"is_error":false,"num_turns":1,"result":{},"stop_reason":"end_turn","total_cost_usd":0,"total_credits":1.25,"usage":{{}},"modelUsage":{{}},"permission_denials":[],"fast_mode_state":"off","uuid":"fake","session_id":"fake"}}"#,
        serde_json::to_string(&result).expect("nest AI result envelope")
    )
}

fn safe_shell_path(path: &Path) -> &str {
    let path = path.to_str().expect("test path must be UTF-8");
    assert!(
        path.bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"/._-".contains(&byte)),
        "test path is not safe for the fixed shell fixture"
    );
    path
}

#[test]
fn fake_qoder_output_round_trips_through_the_real_adapter() {
    let expected = ExpectedEvidence {
        evidence_id: EvidenceId::generate(),
        source_artifact_id: ArtifactId::generate(),
        locator: EvidenceLocator::Pdf {
            page: 1,
            bbox: PdfRect {
                x1: 1.25,
                y1: 2.5,
                x2: 30.75,
                y2: 40.0,
            },
        },
        quote: "Evidence is exact.".into(),
    };
    let envelope = envelope(&expected);
    let parsed = qoder_parse_result(
        Some(0),
        qoder_output(&envelope).as_bytes(),
        1024 * 1024,
        Executor::AiDocumentNote,
        "primary".into(),
    )
    .expect("real Qoder adapter must accept the fixture");
    assert_eq!(parsed.envelope, envelope);
    assert!(matches!(
        parsed.usage,
        UsageUpdate::Final {
            credits_micros: Some(1_250_000),
            ..
        }
    ));
}
