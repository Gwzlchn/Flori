use std::{fs, sync::Arc};

use flori_core::PdfSetupView;
use flori_store::{Store, artifact::NasArtifactStore};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

#[tokio::test]
async fn empty_store_bootstrap_is_visible_over_the_public_contract() {
    let root = std::env::temp_dir().join(format!(
        "flori-pdf-setup-http-{}",
        flori_core::RequestId::generate()
    ));
    fs::create_dir(&root).expect("test root");
    let store = Arc::new(
        Store::open(root.join("flori.sqlite"))
            .await
            .expect("empty store"),
    );
    let expected = store
        .bootstrap_pdf(
            include_str!("../../../pipelines/pdf.yml"),
            "note",
            "translate",
            "test",
            1,
        )
        .await
        .expect("bootstrap");
    let artifacts = Arc::new(
        NasArtifactStore::new(root.join("artifacts"), 1024 * 1024).expect("artifact store"),
    );
    let listener = TcpListener::bind("localhost:0").await.expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move {
        axum::serve(
            listener,
            flori_server::app(
                store,
                artifacts,
                "http://localhost/content".to_owned(),
                60_000,
            )
            .expect("app"),
        )
        .await
        .expect("serve");
    });

    let mut stream = TcpStream::connect(address).await.expect("connect");
    stream
        .write_all(
            b"GET /api/v1/pdf/setup HTTP/1.1\r\nHost: localhost\r\nX-Flori-Protocol: 1\r\nConnection: close\r\n\r\n",
        )
        .await
        .expect("request");
    let mut response = Vec::new();
    stream.read_to_end(&mut response).await.expect("response");
    let split = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .expect("response body");
    assert!(response.starts_with(b"HTTP/1.1 200"));
    let actual: PdfSetupView =
        serde_json::from_slice(&response[split + 4..]).expect("strict setup response");
    assert_eq!(actual, expected);

    server.abort();
    let _ = server.await;
    fs::remove_dir_all(root).expect("remove fixture");
}
