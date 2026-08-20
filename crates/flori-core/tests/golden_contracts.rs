use flori_core::{
    DocumentStructure, EvidenceLocator, EvidenceManifest, PartsManifest, SubscriptionManifest,
    TermsManifest, TranscriptManifest, validate_pdf_evidence, validate_video_evidence,
};

const DOCUMENT: &str = include_str!("../../../tests/fixtures/vnext/expected/document.json");
const PDF_EVIDENCE: &str = include_str!("../../../tests/fixtures/vnext/expected/pdf-evidence.json");
const VIDEO_EVIDENCE: &str =
    include_str!("../../../tests/fixtures/vnext/expected/video-evidence.json");
const TRANSCRIPT: &str = include_str!("../../../tests/fixtures/vnext/expected/transcript.json");
const PARTS: &str = include_str!("../../../tests/fixtures/vnext/expected/parts.json");
const SUBSCRIPTION: &str = include_str!("../../../tests/fixtures/vnext/expected/subscription.json");
const TERMS: &str = include_str!("../../../tests/fixtures/vnext/expected/terms.json");
const VIDEO_TERMS: &str = include_str!("../../../tests/fixtures/vnext/expected/video-terms.json");
const PDF_NOTE: &str = include_str!("../../../tests/fixtures/vnext/expected/pdf-smart-note.md");
const PDF_SUMMARY: &str = include_str!("../../../tests/fixtures/vnext/expected/pdf-summary.md");
const VIDEO_NOTE: &str = include_str!("../../../tests/fixtures/vnext/expected/video-smart-note.md");
const VIDEO_SUMMARY: &str = include_str!("../../../tests/fixtures/vnext/expected/video-summary.md");

#[test]
fn golden_json_uses_only_the_rust_contracts() {
    let document: DocumentStructure = serde_json::from_str(DOCUMENT).expect("document contract");
    let pdf: EvidenceManifest = serde_json::from_str(PDF_EVIDENCE).expect("PDF evidence contract");
    let video: EvidenceManifest =
        serde_json::from_str(VIDEO_EVIDENCE).expect("video evidence contract");
    let transcript: TranscriptManifest =
        serde_json::from_str(TRANSCRIPT).expect("transcript contract");
    let parts: PartsManifest = serde_json::from_str(PARTS).expect("parts contract");
    let subscription: SubscriptionManifest =
        serde_json::from_str(SUBSCRIPTION).expect("subscription contract");
    let terms: TermsManifest = serde_json::from_str(TERMS).expect("terms contract");
    let video_terms: TermsManifest =
        serde_json::from_str(VIDEO_TERMS).expect("video terms contract");

    document.validate().expect("valid document");
    pdf.validate_structure().expect("valid PDF evidence");
    video.validate_structure().expect("valid video evidence");
    transcript.validate().expect("valid transcript");
    parts.validate().expect("valid parts");
    subscription.validate(2).expect("valid subscription");
    assert_eq!(
        validate_pdf_evidence(&document, &terms, PDF_NOTE, PDF_SUMMARY)
            .expect("canonical PDF evidence"),
        pdf,
    );
    let EvidenceLocator::Video {
        keyframe: Some(keyframe),
        ..
    } = &video.items[0].locator
    else {
        panic!("golden keyframe");
    };
    let known_keyframe = *keyframe;
    assert_eq!(
        validate_video_evidence(
            &transcript,
            &[known_keyframe],
            100,
            &video_terms,
            VIDEO_NOTE,
            VIDEO_SUMMARY,
        )
        .expect("canonical video evidence"),
        video,
    );

    assert!(
        validate_pdf_evidence(
            &document,
            &terms,
            &PDF_NOTE.replace("## AI 分析", "## 其它"),
            PDF_SUMMARY,
        )
        .is_err()
    );
    let mut drifted = video_terms;
    let EvidenceLocator::Video {
        keyframe: Some(keyframe),
        ..
    } = &mut drifted.evidence_candidates[0].locator
    else {
        panic!("video locator");
    };
    keyframe.timestamp_ms = 900;
    assert!(
        validate_video_evidence(
            &transcript,
            &[known_keyframe],
            100,
            &drifted,
            VIDEO_NOTE,
            VIDEO_SUMMARY,
        )
        .is_err()
    );
    let mut invalid_parts = parts;
    invalid_parts.parts[0].video_artifact_name = "videos/../escape.mp4".into();
    assert!(invalid_parts.validate().is_err());
}
