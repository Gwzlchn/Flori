use std::{collections::BTreeMap, net::TcpListener, thread, time::Duration};

use flori_core::{
    AiResultEnvelope, AiResultSchema, AiUsageId, AiUsageState, ArtifactId, ArtifactManifestEntry,
    AttemptAck, AttemptState, DocumentPage, DocumentSection, DocumentStructure,
    DocumentStructureSchema, DocumentTextBlock, ErrorCode, EvidenceEntry, EvidenceId,
    EvidenceLocator, FailAttemptRequest, PdfRect, StartUploadRequest, StartUploadResponse,
    TermEntry, TermsManifest, TermsManifestSchema, UploadCursor, UploadId, UsageAck, UsageUpdate,
    VerifyUploadResponse,
};
use tokio::sync::watch;

use super::support::{content_response, json_response, read_request};

pub(crate) struct Observed {
    pub usage: Vec<UsageUpdate>,
    pub uploaded: BTreeMap<String, Vec<u8>>,
    pub state: AttemptState,
    pub error: Option<ErrorCode>,
}

pub(crate) fn server(
    listener: TcpListener,
    claim: flori_core::TaskClaim,
    document: Vec<u8>,
    document_sha: flori_core::Sha256Digest,
    stop: watch::Sender<bool>,
) -> thread::JoinHandle<Observed> {
    thread::spawn(move || {
        let mut usage = Vec::new();
        let mut uploaded = BTreeMap::new();
        let mut current = None;
        loop {
            let (mut stream, _) = listener.accept().expect("accept");
            let (head, body) = read_request(&mut stream);
            let request = head.lines().next().expect("request line");
            if request.starts_with("POST /runner/v1/poll ") {
                json_response(&mut stream, &claim);
            } else if request.starts_with("GET /api/v1/artifacts/") {
                content_response(&mut stream, &document, &document_sha);
            } else if request.contains("/usage ") {
                let update: UsageUpdate = serde_json::from_slice(&body).expect("usage");
                let state = match update {
                    UsageUpdate::Started { .. } => AiUsageState::Started,
                    UsageUpdate::Final { .. } => AiUsageState::Final,
                };
                usage.push(update);
                json_response(
                    &mut stream,
                    &UsageAck {
                        usage_id: AiUsageId::generate(),
                        state,
                        applied: true,
                    },
                );
            } else if request.starts_with("POST /runner/v1/attempts/")
                && request.contains("/uploads ")
            {
                let start: StartUploadRequest = serde_json::from_slice(&body).expect("start");
                let kind = claim
                    .output_declarations
                    .iter()
                    .find(|item| item.name == start.name)
                    .expect("declaration")
                    .kind;
                let upload_id = UploadId::generate();
                let artifact = ArtifactManifestEntry {
                    kind,
                    name: start.name,
                    media_type: start.media_type,
                    size_bytes: start.size_bytes,
                    sha256: start.sha256,
                    relative_path: format!("sources/job/{upload_id}"),
                };
                current = Some((upload_id, artifact.clone(), Vec::new()));
                json_response(
                    &mut stream,
                    &StartUploadResponse {
                        upload_id,
                        received_bytes: 0,
                        artifact,
                    },
                );
            } else if request.starts_with("PUT /runner/v1/uploads/") {
                let (upload_id, _, bytes) = current.as_mut().expect("upload");
                bytes.extend_from_slice(&body);
                json_response(
                    &mut stream,
                    &UploadCursor {
                        upload_id: *upload_id,
                        received_bytes: bytes.len() as u64,
                    },
                );
            } else if request.contains("/verify ") {
                let (upload_id, artifact, bytes) = current.take().expect("verify");
                uploaded.insert(artifact.name.clone(), bytes);
                json_response(
                    &mut stream,
                    &VerifyUploadResponse {
                        upload_id,
                        artifact,
                    },
                );
            } else if request.contains("/complete ") {
                json_response(
                    &mut stream,
                    &AttemptAck {
                        exec_id: claim.exec_id,
                        state: AttemptState::Succeeded,
                    },
                );
                thread::sleep(Duration::from_millis(50));
                stop.send(true).expect("stop");
                return Observed {
                    usage,
                    uploaded,
                    state: AttemptState::Succeeded,
                    error: None,
                };
            } else if request.contains("/fail ") {
                let failed: FailAttemptRequest = serde_json::from_slice(&body).expect("fail");
                json_response(
                    &mut stream,
                    &AttemptAck {
                        exec_id: claim.exec_id,
                        state: AttemptState::Failed,
                    },
                );
                thread::sleep(Duration::from_millis(50));
                stop.send(true).expect("stop");
                return Observed {
                    usage,
                    uploaded,
                    state: AttemptState::Failed,
                    error: Some(failed.error_code),
                };
            } else {
                panic!("unexpected request: {request}");
            }
        }
    })
}

pub(crate) fn document(source_artifact_id: ArtifactId) -> DocumentStructure {
    DocumentStructure {
        schema: DocumentStructureSchema::V1,
        source_artifact_id,
        language: "en".into(),
        pages: vec![DocumentPage {
            page: 1,
            width_pt: 100.0,
            height_pt: 100.0,
        }],
        sections: vec![DocumentSection {
            id: "section-1".into(),
            heading: "Source".into(),
            blocks: vec![DocumentTextBlock {
                page: 1,
                bbox: PdfRect {
                    x1: 0.0,
                    y1: 0.0,
                    x2: 100.0,
                    y2: 20.0,
                },
                text: "source fact".into(),
            }],
        }],
        figures: vec![],
        tables: vec![],
    }
}

pub(crate) fn note(
    source_artifact_id: ArtifactId,
    evidence_id: EvidenceId,
    valid: bool,
) -> AiResultEnvelope {
    let marker = format!("[[evidence:{evidence_id}]]");
    AiResultEnvelope::DocumentNote {
        schema: AiResultSchema::V1,
        smart_note_markdown: format!(
            "## 来源事实\n\nsource fact {marker}\n\n## AI 分析\n\nanalysis"
        ),
        summary_markdown: format!("summary {marker}"),
        terms: TermsManifest {
            schema: TermsManifestSchema::V1,
            terms: valid
                .then(|| TermEntry {
                    term: "term".into(),
                    explanation: "meaning".into(),
                    evidence_ids: vec![evidence_id],
                })
                .into_iter()
                .collect(),
            evidence_candidates: valid
                .then(|| EvidenceEntry {
                    evidence_id,
                    source_artifact_id,
                    locator: EvidenceLocator::Pdf {
                        page: 1,
                        bbox: PdfRect {
                            x1: 0.0,
                            y1: 0.0,
                            x2: 100.0,
                            y2: 20.0,
                        },
                    },
                    quote: "source fact".into(),
                })
                .into_iter()
                .collect(),
        },
    }
}

pub(crate) fn qoder(result: &AiResultEnvelope) -> String {
    let result = serde_json::to_string(result).expect("result JSON");
    format!(
        r#"{{"type":"result","subtype":"success","duration_ms":1,"duration_api_ms":1,"is_error":false,"num_turns":1,"result":{},"stop_reason":"end_turn","total_cost_usd":0,"total_credits":1,"usage":{{}},"modelUsage":{{}},"permission_denials":[],"fast_mode_state":"off","uuid":"fake","session_id":"fake"}}"#,
        serde_json::to_string(&result).expect("nested result")
    )
}
