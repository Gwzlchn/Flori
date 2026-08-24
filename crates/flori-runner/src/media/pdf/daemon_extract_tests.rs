use std::net::TcpListener;

use flori_core::{ArtifactId, ArtifactKind, Executor, ResolvedArtifact, ResolvedTaskInputs};

use super::support::{SuccessCase, TestRoot, digest, success_server};
use super::{claim, config, extract_declarations, run_until_server_closes};

#[tokio::test]
async fn downloads_input_and_uploads_strict_structure() {
    let root = TestRoot::new("extract");
    let listener = TcpListener::bind("127.0.0.1:0").expect("listener");
    let base = format!("http://{}", listener.local_addr().expect("address"));
    let pdf = b"%PDF-1.7\ndigital fixture".to_vec();
    let artifact_id = ArtifactId::generate();
    let claim = claim(
        Executor::DocumentExtract,
        ResolvedTaskInputs::DocumentExtract {
            pdf: ResolvedArtifact {
                artifact_id,
                name: "original".into(),
                kind: ArtifactKind::SourceOriginal,
                media_type: "application/pdf".into(),
                size_bytes: pdf.len() as u64,
                sha256: digest(&pdf),
                download_url: format!("{base}/api/v1/artifacts/{artifact_id}/content"),
            },
        },
        extract_declarations(),
    );
    let server = success_server(
        listener,
        SuccessCase {
            claim: claim.clone(),
            input: pdf,
            input_path: format!("/api/v1/artifacts/{artifact_id}/content"),
            input_media_type: "application/pdf",
        },
    );
    let python = root.script(
        "python",
        r#"output="$4"
id="$5"
printf '\211PNG\r\n\032\nfigure' > "$output/figures/figure-001.png"
printf '\211PNG\r\n\032\ntable' > "$output/tables/table-001.png"
cat > "$output/document.json" <<EOF
{"schema":"flori.document_structure.v1","source_artifact_id":"$id","language":"en","pages":[{"page":1,"width_pt":100.0,"height_pt":200.0}],"sections":[{"id":"s1","heading":"Intro","blocks":[{"page":1,"bbox":{"x1":1.0,"y1":1.0,"x2":90.0,"y2":20.0},"text":"this-page-has-more-than-thirty-two-visible-characters"}]}],"figures":[{"id":"f1","page":1,"bbox":{"x1":1.0,"y1":30.0,"x2":90.0,"y2":60.0},"caption":"Figure","artifact_name":"figures/figure-001.png"}],"tables":[{"id":"t1","page":1,"bbox":{"x1":1.0,"y1":70.0,"x2":90.0,"y2":100.0},"caption":"Table","text":"plain text","artifact_name":"tables/table-001.png"}]}
EOF"#,
    );
    let config = config(
        &root,
        root.script("unused-info", "exit 1"),
        root.script("unused-text", "exit 1"),
        python,
    );
    run_until_server_closes(&base, &config, &server).await;
    let uploads = server.join().expect("server");
    assert_eq!(uploads.len(), 3);
    let output = uploads
        .iter()
        .find(|upload| upload.name == "structure")
        .expect("structure");
    let document: flori_core::DocumentStructure =
        serde_json::from_slice(&output.bytes).expect("strict structure");
    assert_eq!(document.source_artifact_id, artifact_id);
    assert!(
        uploads
            .iter()
            .any(|upload| upload.name == "figures/figure-001.png")
    );
    assert!(
        uploads
            .iter()
            .any(|upload| upload.name == "tables/table-001.png")
    );
}
