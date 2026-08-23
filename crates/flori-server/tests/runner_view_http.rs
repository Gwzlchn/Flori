use std::{fmt::Write, fs, sync::Arc, time::SystemTime};

use flori_core::{
    AiModelCapability, CreateRunnerSlot, ErrorCode, ErrorResponse, RegisterRunnerRequest,
    RunnerState, RunnerTool, RunnerToolCapability, RunnerView, Sha256Digest,
};
use flori_store::{Store, artifact::NasArtifactStore};
use sha2::{Digest, Sha256};
use sqlx::{SqlitePool, sqlite::SqliteConnectOptions};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::{TcpListener, TcpStream},
};

#[tokio::test]
async fn runner_inventory_is_public_strict_and_secret_free() {
    let root = std::env::temp_dir().join(format!(
        "flori-runner-view-{}",
        flori_core::RequestId::generate()
    ));
    fs::create_dir(&root).expect("test root");
    let database = root.join("flori.sqlite");
    let store = Arc::new(Store::open(&database).await.expect("store"));
    let now = i64::try_from(
        SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .expect("wall clock")
            .as_millis(),
    )
    .expect("timestamp");
    let registration = digest("registration");
    let runner_id = store
        .create_runner_slot(
            &slot("qoder-home", Some(("Ultimate", "high"))),
            &registration,
            now + 60_000,
            now,
        )
        .await
        .expect("slot");
    store
        .register_runner(
            &registration,
            &digest("runner-token"),
            &RegisterRunnerRequest {
                tools: vec![RunnerToolCapability {
                    tool: RunnerTool::QoderCli,
                    version: "1.1.26".into(),
                }],
                ai_models: vec![AiModelCapability {
                    model: "Ultimate".into(),
                    efforts: vec!["high".into()],
                }],
            },
            now,
        )
        .await
        .expect("register");
    store
        .create_runner_slot(
            &slot("waiting-media", None),
            &digest("media-registration"),
            now + 60_000,
            now,
        )
        .await
        .expect("disabled slot");

    let pool = SqlitePool::connect_with(
        SqliteConnectOptions::new()
            .filename(&database)
            .foreign_keys(true),
    )
    .await
    .expect("inspection pool");
    let artifacts =
        Arc::new(NasArtifactStore::new(root.join("artifacts"), 1024 * 1024).expect("artifacts"));
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("bind");
    let address = listener.local_addr().expect("address");
    let server = tokio::spawn(async move {
        axum::serve(
            listener,
            flori_server::app(store, artifacts, "http://localhost/content".into(), 60_000)
                .expect("app"),
        )
        .await
        .expect("serve");
    });

    let response = get(address).await;
    assert_eq!(status(&response), 200);
    let response_body = body(&response);
    let runners: Vec<RunnerView> = serde_json::from_slice(response_body).expect("runner views");
    assert_eq!(runners.len(), 2);
    assert_eq!(runners[0].name, "qoder-home");
    assert_eq!(runners[0].runner_id, runner_id);
    assert_eq!(runners[0].state, RunnerState::Enabled);
    assert!(runners[0].online);
    assert_eq!(runners[0].ai_models[0].model, "Ultimate");
    assert_eq!(runners[1].state, RunnerState::Disabled);
    assert!(!runners[1].online);
    let text = String::from_utf8_lossy(response_body);
    assert!(!text.contains("token"));
    assert!(!text.contains("digest"));

    sqlx::query("UPDATE runners SET tools_json='[{}]' WHERE id=?")
        .bind(runner_id.to_string())
        .execute(&pool)
        .await
        .expect("corrupt inventory");
    let response = get(address).await;
    assert_eq!(status(&response), 500);
    let error: ErrorResponse = serde_json::from_slice(body(&response)).expect("strict error");
    assert_eq!(error.error.code, ErrorCode::CorruptState);

    server.abort();
    let _ = server.await;
    fs::remove_dir_all(root).expect("remove test root");
}

fn slot(name: &str, default: Option<(&str, &str)>) -> CreateRunnerSlot {
    CreateRunnerSlot {
        name: name.into(),
        tags: vec!["ai".into()],
        max_concurrency: 1,
        default_model: default.map(|value| value.0.into()),
        default_effort: default.map(|value| value.1.into()),
    }
}

fn digest(value: &str) -> Sha256Digest {
    let mut output = String::with_capacity(64);
    for byte in Sha256::digest(value.as_bytes()) {
        write!(&mut output, "{byte:02x}").expect("digest");
    }
    Sha256Digest::parse(output).expect("sha256")
}

async fn get(address: std::net::SocketAddr) -> Vec<u8> {
    let mut stream = TcpStream::connect(address).await.expect("connect");
    stream
        .write_all(
            b"GET /api/v1/runners HTTP/1.1\r\nHost: localhost\r\nX-Flori-Protocol: 1\r\nConnection: close\r\n\r\n",
        )
        .await
        .expect("request");
    let mut response = Vec::new();
    stream.read_to_end(&mut response).await.expect("response");
    response
}

fn status(response: &[u8]) -> u16 {
    std::str::from_utf8(response)
        .expect("HTTP")
        .split_whitespace()
        .nth(1)
        .expect("status")
        .parse()
        .expect("status number")
}

fn body(response: &[u8]) -> &[u8] {
    let split = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .expect("body");
    &response[split + 4..]
}
