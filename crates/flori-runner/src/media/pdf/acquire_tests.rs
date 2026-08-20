use std::net::{IpAddr, Ipv4Addr, SocketAddr};
use std::os::unix::fs::symlink;
use std::time::Duration;

use flori_core::{ArtifactId, ResolvedSourceInput, SourceId, SourceInputId};

use super::*;

#[test]
fn arxiv_ids_are_path_safe() {
    for value in ["1706.03762", "1706.03762v7", "hep-th/9901001"] {
        assert!(valid_arxiv_id(value));
    }
    for value in ["", "../secret", "/1706.03762", "1706.03762?x=1"] {
        assert!(!valid_arxiv_id(value));
    }
}

#[test]
fn three_source_kinds_resolve_to_one_download_contract() {
    let mut source = ResolvedSource {
        source_id: SourceId::generate(),
        kind: SourceKind::Arxiv,
        canonical_ref: "arxiv:1706.03762".into(),
        input: None,
    };
    let (url, expected) = source_url(&source).expect("arXiv source");
    assert_eq!(url.as_str(), "https://arxiv.org/pdf/1706.03762");
    assert!(expected.is_none());

    source.kind = SourceKind::PdfUrl;
    source.canonical_ref = "url:https://example.com/paper.pdf".into();
    assert_eq!(
        source_url(&source).expect("PDF URL").0.as_str(),
        "https://example.com/paper.pdf"
    );

    let digest = Sha256Digest::parse("a".repeat(64)).expect("test digest");
    source.kind = SourceKind::PdfUpload;
    source.canonical_ref = "upload:paper".into();
    source.input = Some(ResolvedSourceInput {
        source_input_id: SourceInputId::generate(),
        name: "paper.pdf".into(),
        media_type: "application/pdf".into(),
        size_bytes: 42,
        sha256: digest.clone(),
        download_url: "https://core.example.com/source-input".into(),
    });
    let (url, expected) = source_url(&source).expect("uploaded PDF");
    assert_eq!(url.as_str(), "https://core.example.com/source-input");
    assert_eq!(expected, Some((42, &digest)));
}

#[tokio::test]
async fn destination_creation_never_overwrites_or_follows_symlinks() {
    let root = std::env::temp_dir().join(format!("flori-acquire-{}", ArtifactId::generate()));
    std::fs::create_dir(&root).expect("create test root");
    let existing = root.join("paper.pdf");
    std::fs::write(&existing, b"keep").expect("write existing file");
    assert!(create_file(&existing).await.is_err());
    assert_eq!(
        std::fs::read(&existing).expect("read existing file"),
        b"keep"
    );

    let outside = root.join("outside");
    let linked = root.join("linked");
    std::fs::create_dir(&outside).expect("create outside directory");
    symlink(&outside, &linked).expect("create directory symlink");
    assert_eq!(
        create_file(&linked.join("paper.pdf")).await.err(),
        Some(ErrorCode::ArtifactInvalidPath)
    );
    std::fs::remove_dir_all(root).expect("remove test root");
}

#[tokio::test]
async fn local_http_is_rejected_before_a_connection() {
    let listener =
        tokio::net::TcpListener::bind(SocketAddr::new(IpAddr::V4(Ipv4Addr::LOCALHOST), 0))
            .await
            .expect("bind fake HTTP server");
    let address = listener.local_addr().expect("fake HTTP address");
    let source = ResolvedSource {
        source_id: SourceId::generate(),
        kind: SourceKind::PdfUrl,
        canonical_ref: format!("url:http://{address}/paper.pdf"),
        input: None,
    };
    let root = std::env::temp_dir().join(format!("flori-http-{}", ArtifactId::generate()));
    std::fs::create_dir(&root).expect("create test root");
    let result = acquire_pdf(
        &source,
        &root.join("paper.pdf"),
        &PdfAcquireConfig {
            pdfinfo: "/usr/bin/pdfinfo".into(),
            pdftotext: "/usr/bin/pdftotext".into(),
            max_bytes: 1024,
            max_probe_output_bytes: 4096,
            timeout: Duration::from_millis(100),
        },
    )
    .await;
    assert_eq!(result.err(), Some(ErrorCode::UnsupportedSource));
    assert!(
        tokio::time::timeout(Duration::from_millis(20), listener.accept())
            .await
            .is_err()
    );
    std::fs::remove_dir_all(root).expect("remove test root");
}

#[test]
fn redirects_are_relative_safe_and_capped_at_five() {
    let current = parse_http_url("https://example.com/a/paper.pdf").expect("base URL");
    assert_eq!(
        redirect_url(&current, "../final.pdf", 4)
            .expect("fifth redirect")
            .as_str(),
        "https://example.com/final.pdf"
    );
    for (location, count) in [
        ("https://example.com/sixth.pdf", 5),
        ("file:///tmp/paper.pdf", 0),
        ("https://user@example.com/paper.pdf", 0),
    ] {
        assert_eq!(
            redirect_url(&current, location, count),
            Err(ErrorCode::UnsupportedSource)
        );
    }
}
