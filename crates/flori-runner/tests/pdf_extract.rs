use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::time::Duration;

use flori_core::{ArtifactId, ArtifactKind, ErrorCode, ResolvedArtifact, Sha256Digest};
use flori_runner::{PdfExtractConfig, extract_pdf};
use sha2::{Digest, Sha256};

struct TestRoot(PathBuf);

impl TestRoot {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!("flori-pdf-{}", ArtifactId::generate()));
        std::fs::create_dir(&path).expect("create test root");
        Self(path)
    }

    fn script(&self, name: &str, body: &str) -> PathBuf {
        let path = self.0.join(name);
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write fake tool");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700))
            .expect("make fake tool executable");
        path
    }
}

impl Drop for TestRoot {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.0).expect("remove test root");
    }
}

fn fixture() -> (PathBuf, ResolvedArtifact) {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../tests/fixtures/vnext/digital-paper.pdf")
        .canonicalize()
        .expect("golden PDF");
    let bytes = std::fs::read(&path).expect("read golden PDF");
    let sha256 = Sha256Digest::parse(
        Sha256::digest(&bytes)
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>(),
    )
    .expect("fixture digest");
    (
        path,
        ResolvedArtifact {
            artifact_id: ArtifactId::generate(),
            name: "original".into(),
            kind: ArtifactKind::SourceOriginal,
            media_type: "application/pdf".into(),
            size_bytes: u64::try_from(bytes.len()).expect("fixture size"),
            sha256,
            download_url: "https://example.invalid/content".into(),
        },
    )
}

fn config(root: &TestRoot, python_body: &str) -> PdfExtractConfig {
    PdfExtractConfig {
        python: root.script("python", python_body),
        timeout: Duration::from_secs(2),
        max_structure_bytes: 4096,
        max_asset_bytes: 4096,
        max_assets: 4,
    }
}

const VALID_EXTRACTOR: &str = r#"
output="$4"
id="$5"
printf '\211PNG\r\n\032\nfigure' > "$output/figures/figure-001.png"
printf '\211PNG\r\n\032\ntable' > "$output/tables/table-001.png"
cat > "$output/document.json" <<EOF
{"schema":"flori.document_structure.v1","source_artifact_id":"$id","language":"en","pages":[{"page":1,"width_pt":100.0,"height_pt":200.0}],"sections":[{"id":"section-1","heading":"Intro","blocks":[{"page":1,"bbox":{"x1":1.0,"y1":1.0,"x2":90.0,"y2":20.0},"text":"this-page-has-more-than-thirty-two-visible-characters"}]}],"figures":[{"id":"figure-1","page":1,"bbox":{"x1":1.0,"y1":30.0,"x2":90.0,"y2":60.0},"caption":"Figure 1","artifact_name":"figures/figure-001.png"}],"tables":[{"id":"table-1","page":1,"bbox":{"x1":1.0,"y1":70.0,"x2":90.0,"y2":100.0},"caption":"Table 1","text":"plain table text","artifact_name":"tables/table-001.png"}]}
EOF
"#;

#[tokio::test]
async fn fake_extractor_emits_strict_structure_and_regions() {
    let root = TestRoot::new();
    let (input, artifact) = fixture();
    let output = root.0.join("output");
    let extraction = extract_pdf(&artifact, &input, &output, &config(&root, VALID_EXTRACTOR))
        .await
        .expect("extract digital PDF");

    assert_eq!(extraction.source_artifact_id, artifact.artifact_id);
    assert_eq!(extraction.figures.len(), 1);
    assert_eq!(extraction.tables.len(), 1);
    let stored: flori_core::DocumentStructure = serde_json::from_slice(
        &std::fs::read(output.join("document.json")).expect("read normalized structure"),
    )
    .expect("strict normalized structure");
    assert_eq!(stored, extraction);
}

#[tokio::test]
async fn rejects_unknown_json_symlinks_and_undeclared_files() {
    for (script, expected) in [
        (
            VALID_EXTRACTOR.replace("\"tables\":[", "\"extra\":true,\"tables\":["),
            ErrorCode::ExecutorFailed,
        ),
        (
            VALID_EXTRACTOR.replace(
                "printf '\\211PNG\\r\\n\\032\\nfigure' > \"$output/figures/figure-001.png\"",
                "ln -s /etc/passwd \"$output/figures/figure-001.png\"",
            ),
            ErrorCode::ArtifactInvalidPath,
        ),
        (
            format!("{VALID_EXTRACTOR}\nprintf x > \"$output/undeclared\""),
            ErrorCode::ArtifactUndeclared,
        ),
    ] {
        let root = TestRoot::new();
        let (input, artifact) = fixture();
        let output = root.0.join("output");
        let result = extract_pdf(&artifact, &input, &output, &config(&root, &script)).await;
        assert_eq!(result.err(), Some(expected));
        assert!(!output.exists(), "failed output must be removed");
    }
}

#[tokio::test]
async fn enforces_asset_size_and_extractor_timeout() {
    let root = TestRoot::new();
    let (input, artifact) = fixture();
    let mut limited = config(&root, VALID_EXTRACTOR);
    limited.max_asset_bytes = 8;
    assert_eq!(
        extract_pdf(&artifact, &input, &root.0.join("large"), &limited)
            .await
            .err(),
        Some(ErrorCode::ArtifactTooLarge)
    );

    let mut timeout = config(&root, "while true; do :; done");
    timeout.timeout = Duration::from_millis(20);
    assert_eq!(
        extract_pdf(&artifact, &input, &root.0.join("timeout"), &timeout)
            .await
            .err(),
        Some(ErrorCode::AttemptTimeout)
    );
}

#[tokio::test]
async fn pinned_extractor_matches_the_offline_golden_when_enabled() {
    let Some(python) = std::env::var_os("FLORI_PDF_TEST_PYTHON") else {
        return;
    };
    let root = TestRoot::new();
    let (input, artifact) = fixture();
    let output = root.0.join("real-output");
    let config = PdfExtractConfig {
        python: PathBuf::from(python),
        timeout: Duration::from_secs(30),
        max_structure_bytes: 50 * 1024 * 1024,
        max_asset_bytes: 20 * 1024 * 1024,
        max_assets: 128,
    };
    let structure = extract_pdf(&artifact, &input, &output, &config)
        .await
        .expect("run pinned extractor");

    assert_eq!(structure.pages.len(), 1);
    assert!(!structure.sections.is_empty());
    assert_eq!(structure.figures.len(), 1);
    assert_eq!(structure.tables.len(), 1);
    assert!(output.join("figures/figure-001.png").is_file());
    assert!(output.join("tables/table-001.png").is_file());
}
