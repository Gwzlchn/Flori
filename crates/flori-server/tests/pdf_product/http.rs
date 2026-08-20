use std::{net::SocketAddr, str};

use flori_core::{
    CreateUploadSource, CreatedSource, EvidenceLocator, EvidenceManifest, EvidenceView, SearchHit,
};
use serde::{Serialize, de::DeserializeOwned};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
};

use super::assertions::{VerifyContext, VerifyMode};

const BOUNDARY: &str = "flori-pdf-product-boundary";

pub(super) async fn upload_pdf(
    address: SocketAddr,
    metadata: &CreateUploadSource,
    file_name: &str,
    pdf: &[u8],
) -> CreatedSource {
    let metadata = serde_json::to_vec(metadata).expect("serialize upload metadata");
    let mut body = Vec::new();
    part(&mut body, "metadata", None, "application/json", &metadata);
    part(&mut body, "file", Some(file_name), "application/pdf", pdf);
    body.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
    let response = exchange(
        address,
        "POST",
        "/api/v1/sources/uploads",
        &format!("Content-Type: multipart/form-data; boundary={BOUNDARY}\r\n"),
        &body,
    )
    .await;
    decode_json(&response, 200)
}

pub(super) async fn post_json<I: Serialize, O: DeserializeOwned>(
    address: SocketAddr,
    path: &str,
    input: &I,
) -> O {
    let body = serde_json::to_vec(input).expect("serialize request");
    let response = exchange(
        address,
        "POST",
        path,
        "Content-Type: application/json\r\n",
        &body,
    )
    .await;
    decode_json(&response, 200)
}

pub(super) async fn get_json<O: DeserializeOwned>(address: SocketAddr, path: &str) -> O {
    let response = exchange(address, "GET", path, "", &[]).await;
    decode_json(&response, 200)
}

pub(super) async fn verify_search_and_evidence(
    context: &VerifyContext<'_>,
    manifest: &EvidenceManifest,
) {
    let query = match context.mode {
        VerifyMode::Fake { .. } => "evidence",
        VerifyMode::Real => "Transformer",
    };
    let hits: Vec<SearchHit> = get_json(
        context.address,
        &format!("/api/v1/search?q={query}&limit=20"),
    )
    .await;
    assert!(!hits.is_empty(), "published note must be searchable");
    assert!(
        hits.iter()
            .all(|hit| hit.source_id == context.source_id && hit.job_id == context.job_id)
    );
    for entry in &manifest.items {
        let view: EvidenceView = get_json(
            context.address,
            &format!("/api/v1/evidence/{}", entry.evidence_id),
        )
        .await;
        assert_eq!(
            (view.source_id, view.job_id),
            (context.source_id, context.job_id)
        );
        assert_eq!(view.evidence_id, entry.evidence_id);
        assert_eq!(view.source_artifact_id, entry.source_artifact_id);
        assert_eq!(view.locator, entry.locator);
        assert_eq!(view.quote, entry.quote);
        assert!(matches!(view.locator, EvidenceLocator::Pdf { .. }));
    }
    if let VerifyMode::Fake { expected, .. } = context.mode {
        assert!(
            hits.iter()
                .any(|hit| hit.evidence_ids.contains(&expected.evidence_id))
        );
    }
}

async fn exchange(
    address: SocketAddr,
    method: &str,
    path: &str,
    extra_headers: &str,
    body: &[u8],
) -> Vec<u8> {
    let head = format!(
        "{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\
         X-Flori-Protocol: 1\r\n{extra_headers}Content-Length: {}\r\n\r\n",
        body.len()
    );
    let mut stream = TcpStream::connect(address)
        .await
        .expect("connect to server");
    stream
        .write_all(head.as_bytes())
        .await
        .expect("write HTTP headers");
    stream.write_all(body).await.expect("write HTTP body");
    let mut response = Vec::new();
    stream
        .read_to_end(&mut response)
        .await
        .expect("read HTTP response");
    response
}

fn part(body: &mut Vec<u8>, name: &str, file_name: Option<&str>, media_type: &str, bytes: &[u8]) {
    body.extend_from_slice(format!("--{BOUNDARY}\r\n").as_bytes());
    let filename = file_name.map_or(String::new(), |name| format!("; filename=\"{name}\""));
    body.extend_from_slice(
        format!(
            "Content-Disposition: form-data; name=\"{name}\"{filename}\r\n\
             Content-Type: {media_type}\r\n\r\n"
        )
        .as_bytes(),
    );
    body.extend_from_slice(bytes);
    body.extend_from_slice(b"\r\n");
}

fn decode_json<O: DeserializeOwned>(response: &[u8], expected_status: u16) -> O {
    let status = str::from_utf8(response)
        .expect("UTF-8 HTTP response")
        .split_whitespace()
        .nth(1)
        .expect("HTTP status")
        .parse::<u16>()
        .expect("numeric HTTP status");
    let body = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .map(|position| &response[position + 4..])
        .expect("HTTP header terminator");
    assert_eq!(
        status,
        expected_status,
        "unexpected HTTP response: {}",
        String::from_utf8_lossy(body)
    );
    serde_json::from_slice(body).expect("strict response JSON")
}
