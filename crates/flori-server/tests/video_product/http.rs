use std::{net::SocketAddr, time::Duration};

use flori_core::{
    CreateJobRequest, CreateRemoteSource, CreateUploadSource, CreatedJob, CreatedSource,
};
use serde::de::DeserializeOwned;
use sqlx::SqlitePool;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpStream,
};

const BOUNDARY: &str = "flori-video-product-boundary";

pub(super) async fn upload(
    address: SocketAddr,
    metadata: &CreateUploadSource,
    video: &[u8],
) -> CreatedSource {
    let metadata = serde_json::to_vec(metadata).expect("metadata");
    let mut body = Vec::new();
    part(&mut body, "metadata", None, "application/json", &metadata);
    part(
        &mut body,
        "file",
        Some("local-video.mp4"),
        "video/mp4",
        video,
    );
    body.extend_from_slice(format!("--{BOUNDARY}--\r\n").as_bytes());
    decode(
        exchange(
            address,
            "POST",
            "/api/v1/sources/uploads",
            &format!("Content-Type: multipart/form-data; boundary={BOUNDARY}\r\n"),
            &body,
        )
        .await,
    )
}

pub(super) async fn create_job(
    address: SocketAddr,
    source: &CreatedSource,
    request: &CreateJobRequest,
) -> CreatedJob {
    let body = serde_json::to_vec(request).expect("job request");
    decode(
        exchange(
            address,
            "POST",
            &format!("/api/v1/sources/{}/jobs", source.source_id),
            "Content-Type: application/json\r\n",
            &body,
        )
        .await,
    )
}

pub(super) async fn create_remote(
    address: SocketAddr,
    request: &CreateRemoteSource,
) -> CreatedSource {
    let body = serde_json::to_vec(request).expect("source request");
    decode(
        exchange(
            address,
            "POST",
            "/api/v1/sources",
            "Content-Type: application/json\r\n",
            &body,
        )
        .await,
    )
}

pub(super) async fn get_json<T: DeserializeOwned>(address: SocketAddr, path: &str) -> T {
    decode(exchange(address, "GET", path, "", &[]).await)
}

pub(super) async fn wait_task_for(
    pool: &SqlitePool,
    job: flori_core::JobId,
    key: &str,
    expected: &str,
    timeout: Duration,
) {
    tokio::time::timeout(timeout, async {
        loop {
            let state: String =
                sqlx::query_scalar("SELECT state FROM tasks WHERE job_id=? AND task_key=?")
                    .bind(job.to_string())
                    .bind(key)
                    .fetch_one(pool)
                    .await
                    .expect("task state");
            if state == expected {
                return;
            }
            assert!(
                !matches!(state.as_str(), "failed" | "canceled"),
                "task {key} became {state}"
            );
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("task timeout");
}

pub(super) async fn wait_published(pool: &SqlitePool, job: flori_core::JobId) {
    tokio::time::timeout(Duration::from_secs(10), async {
        loop {
            let state: String = sqlx::query_scalar("SELECT state FROM jobs WHERE id=?")
                .bind(job.to_string())
                .fetch_one(pool)
                .await
                .expect("job state");
            if state == "succeeded" {
                return;
            }
            assert_ne!(state, "failed", "video Job failed");
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
    })
    .await
    .expect("publish timeout");
}

async fn exchange(
    address: SocketAddr,
    method: &str,
    path: &str,
    headers: &str,
    body: &[u8],
) -> Vec<u8> {
    let mut stream = TcpStream::connect(address).await.expect("connect");
    stream.write_all(format!(
        "{method} {path} HTTP/1.1\r\nHost: localhost\r\nX-Flori-Protocol: 1\r\nConnection: close\r\n{headers}Content-Length: {}\r\n\r\n",
        body.len()
    ).as_bytes()).await.expect("headers");
    stream.write_all(body).await.expect("body");
    let mut response = Vec::new();
    stream.read_to_end(&mut response).await.expect("response");
    response
}

fn part(body: &mut Vec<u8>, name: &str, file: Option<&str>, media: &str, bytes: &[u8]) {
    let filename = file.map_or(String::new(), |value| format!("; filename=\"{value}\""));
    body.extend_from_slice(format!(
        "--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{name}\"{filename}\r\nContent-Type: {media}\r\n\r\n"
    ).as_bytes());
    body.extend_from_slice(bytes);
    body.extend_from_slice(b"\r\n");
}

fn decode<T: DeserializeOwned>(response: Vec<u8>) -> T {
    let split = response
        .windows(4)
        .position(|item| item == b"\r\n\r\n")
        .expect("HTTP response");
    let head = std::str::from_utf8(&response[..split]).expect("headers UTF-8");
    assert!(
        head.starts_with("HTTP/1.1 200 "),
        "{head}: {}",
        String::from_utf8_lossy(&response[split + 4..])
    );
    serde_json::from_slice(&response[split + 4..]).expect("strict response")
}
