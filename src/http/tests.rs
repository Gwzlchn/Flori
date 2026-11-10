use std::{fs, path::PathBuf, time::Duration};

use axum::{body::Body as AxumBody, http::Request as HttpRequest, http::header};
use http_body_util::BodyExt;
use serde_json::{Value, json};
use tower::ServiceExt;

use super::*;
use crate::{
    api::JobStatus,
    exec::tests::{config, fake, gone, wait_for, wait_status},
};

struct App {
    dir: tempfile::TempDir,
    router: Router,
    state: AppState,
}

async fn app() -> App {
    let dir = tempfile::tempdir().unwrap();
    let config = Arc::new(config(dir.path()));
    fs::create_dir_all(&config.static_dir).unwrap();
    fs::write(config.static_dir.join("index.html"), "<!doctype html>index").unwrap();
    fs::write(config.static_dir.join("app.js"), "console.log(1)").unwrap();
    let db = db::open(&dir.path().join("t.sqlite")).await.unwrap();
    let exec = exec::launch(db.clone(), config.clone(), "1.0".into())
        .await
        .unwrap();
    let state = AppState { db, exec, config };
    App {
        router: router(state.clone()),
        dir,
        state,
    }
}

fn auth() -> String {
    format!("Basic {}", BASE64.encode("flori:test-only"))
}

impl App {
    async fn send(
        &self,
        req: HttpRequest<AxumBody>,
    ) -> (StatusCode, axum::http::HeaderMap, Vec<u8>) {
        let res = self.router.clone().oneshot(req).await.unwrap();
        let (parts, body) = res.into_parts();
        (
            parts.status,
            parts.headers,
            body.collect().await.unwrap().to_bytes().to_vec(),
        )
    }

    async fn call(&self, method: &str, uri: &str, body: Option<Value>) -> (StatusCode, Value) {
        let req = HttpRequest::builder()
            .method(method)
            .uri(uri)
            .header(AUTHORIZATION, auth())
            .header(header::CONTENT_TYPE, "application/json");
        let req = req
            .body(body.map_or_else(AxumBody::empty, |b| AxumBody::from(b.to_string())))
            .unwrap();
        let (status, _, bytes) = self.send(req).await;
        (
            status,
            serde_json::from_slice(&bytes).unwrap_or(Value::Null),
        )
    }

    async fn get(&self, uri: &str) -> (StatusCode, Value) {
        self.call("GET", uri, None).await
    }

    async fn post(&self, uri: &str, body: Value) -> (StatusCode, Value) {
        self.call("POST", uri, Some(body)).await
    }

    async fn domain(&self, name: &str) -> i64 {
        let (status, body) = self.post("/api/domains", json!({"name": name})).await;
        assert_eq!(status, StatusCode::CREATED, "{body}");
        body["id"].as_i64().unwrap()
    }

    async fn collection(&self, domain_id: i64, name: &str) -> i64 {
        let body = json!({"domain_id": domain_id, "name": name});
        let (status, body) = self.post("/api/collections", body).await;
        assert_eq!(status, StatusCode::CREATED, "{body}");
        body["id"].as_i64().unwrap()
    }

    fn job_dir(&self, id: i64) -> PathBuf {
        job_dir(&self.state.config.data_dir, id)
    }
}

fn code(body: &Value) -> &str {
    body["code"].as_str().unwrap_or_default()
}

#[tokio::test]
async fn auth_guards_everything_but_health() {
    let app = app().await;
    let plain = |uri: &str, auth: Option<&str>| {
        let mut req = HttpRequest::builder().uri(uri);
        if let Some(auth) = auth {
            req = req.header(AUTHORIZATION, auth);
        }
        req.body(AxumBody::empty()).unwrap()
    };
    let wrong = format!("Basic {}", BASE64.encode("flori:test-onlyX"));
    for uri in [
        "/api/domains",
        "/",
        "/jobs/1",
        "/app.js",
        "/api/jobs/1/files/job.json",
    ] {
        let (status, headers, _) = app.send(plain(uri, None)).await;
        assert_eq!(status, StatusCode::UNAUTHORIZED, "{uri}");
        assert!(
            headers[WWW_AUTHENTICATE]
                .to_str()
                .unwrap()
                .starts_with("Basic")
        );
        assert_eq!(
            app.send(plain(uri, Some(&wrong))).await.0,
            StatusCode::UNAUTHORIZED
        );
        assert_eq!(
            app.send(plain(uri, Some("Bearer x"))).await.0,
            StatusCode::UNAUTHORIZED
        );
    }
    let (status, _, body) = app.send(plain("/api/health", None)).await;
    assert_eq!(status, StatusCode::OK);
    let body: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["version"], env!("CARGO_PKG_VERSION"));

    let ok = auth();
    for (uri, content) in [
        ("/", "index"),
        ("/jobs/12", "index"),
        ("/app.js", "console.log(1)"),
    ] {
        let (status, _, body) = app.send(plain(uri, Some(&ok))).await;
        assert_eq!(status, StatusCode::OK, "{uri}");
        assert!(String::from_utf8(body).unwrap().contains(content), "{uri}");
    }
    let (status, body) = app.get("/api/nope").await;
    assert_eq!((status, code(&body)), (StatusCode::NOT_FOUND, "not_found"));
    let (status, body) = app.get("/api/jobs/abc").await;
    assert_eq!((status, code(&body)), (StatusCode::NOT_FOUND, "not_found"));
}

#[tokio::test]
async fn domains_and_collections_validate_names_and_refuse_deletes_in_use() {
    let app = app().await;
    for bad in ["ML", "-ml", "", "a_b", "数学", &"a".repeat(49)] {
        let (status, body) = app.post("/api/domains", json!({"name": bad})).await;
        assert_eq!(
            (status, code(&body)),
            (StatusCode::BAD_REQUEST, "invalid_name"),
            "{bad:?}"
        );
    }
    let (status, body) = app
        .post("/api/domains", json!({"name": "ml", "extra": 1}))
        .await;
    assert_eq!(
        (status, code(&body)),
        (StatusCode::BAD_REQUEST, "invalid_request")
    );
    let ml = app.domain("ml").await;
    let math = app.domain(&format!("0{}", "a".repeat(47))).await;
    let (status, body) = app.post("/api/domains", json!({"name": "ml"})).await;
    assert_eq!((status, code(&body)), (StatusCode::CONFLICT, "conflict"));
    assert_eq!(body.get("job_id"), None);

    let papers = app.collection(ml, "papers").await;
    app.collection(math, "papers").await;
    let (status, body) = app
        .post(
            "/api/collections",
            json!({"domain_id": ml, "name": "papers"}),
        )
        .await;
    assert_eq!((status, code(&body)), (StatusCode::CONFLICT, "conflict"));
    let (status, body) = app
        .post("/api/collections", json!({"domain_id": 999, "name": "x"}))
        .await;
    assert_eq!((status, code(&body)), (StatusCode::NOT_FOUND, "not_found"));
    let (status, body) = app
        .post("/api/collections", json!({"domain_id": ml, "name": "Bad"}))
        .await;
    assert_eq!(
        (status, code(&body)),
        (StatusCode::BAD_REQUEST, "invalid_name")
    );
    let unsorted = json!({"domain_id": ml, "name": "unsorted"});
    let (status, body) = app.post("/api/collections", unsorted).await;
    assert_eq!(
        (status, code(&body)),
        (StatusCode::BAD_REQUEST, "invalid_name"),
        "reserved name"
    );
    let (_, list) = app.get("/api/collections").await;
    assert_eq!(list.as_array().unwrap().len(), 2);

    let (status, _) = app
        .call("DELETE", &format!("/api/domains/{ml}"), None)
        .await;
    assert_eq!(status, StatusCode::CONFLICT, "domain with a collection");
    let job = json!({"url": "2401.12345", "domain_id": ml, "collection_id": papers});
    assert_eq!(app.post("/api/jobs", job).await.0, StatusCode::CREATED);
    let (status, body) = app
        .call("DELETE", &format!("/api/collections/{papers}"), None)
        .await;
    assert_eq!(
        (status, code(&body)),
        (StatusCode::CONFLICT, "conflict"),
        "collection with a job"
    );
    let empty = app.collection(ml, "empty").await;
    let (status, _) = app
        .call("DELETE", &format!("/api/collections/{empty}"), None)
        .await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    let (status, _) = app
        .call("DELETE", &format!("/api/collections/{empty}"), None)
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let (status, _) = app.call("DELETE", "/api/domains/999", None).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    // A domain whose only dependant is a job without a collection.
    let solo = app.domain("unsorted").await;
    let job = json!({"url": "2401.12345", "domain_id": solo});
    assert_eq!(app.post("/api/jobs", job).await.0, StatusCode::CREATED);
    let (status, body) = app
        .call("DELETE", &format!("/api/domains/{solo}"), None)
        .await;
    assert_eq!(
        (status, code(&body)),
        (StatusCode::CONFLICT, "conflict"),
        "domain with a job"
    );
    let lone = app.domain("lone").await;
    let (status, _) = app
        .call("DELETE", &format!("/api/domains/{lone}"), None)
        .await;
    assert_eq!(status, StatusCode::NO_CONTENT);
    let (_, list) = app.get("/api/domains").await;
    let names: Vec<&str> = list
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["name"].as_str().unwrap())
        .collect();
    assert_eq!(
        names,
        [format!("0{}", "a".repeat(47)).as_str(), "ml", "unsorted"]
    );
}

#[tokio::test]
async fn duplicate_submissions_conflict_with_the_existing_job() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let (status, body) = app
        .post(
            "/api/jobs",
            json!({"url": "https://b23.tv/x", "domain_id": ml}),
        )
        .await;
    assert_eq!(
        (status, code(&body)),
        (StatusCode::BAD_REQUEST, "invalid_url")
    );
    let (status, body) = app
        .post("/api/jobs", json!({"url": "2401.12345", "domain_id": 999}))
        .await;
    assert_eq!((status, code(&body)), (StatusCode::NOT_FOUND, "not_found"));
    let (status, body) = app
        .post(
            "/api/jobs",
            json!({"url": "2401.12345", "domain_id": ml, "x": 1}),
        )
        .await;
    assert_eq!(
        (status, code(&body)),
        (StatusCode::BAD_REQUEST, "invalid_request")
    );

    let submit =
        json!({"url": "https://arxiv.org/abs/2401.12345v2", "domain_id": ml, "translate": "off"});
    let (status, job) = app.post("/api/jobs", submit).await;
    assert_eq!(status, StatusCode::CREATED, "{job}");
    assert_eq!(
        (
            &job["platform"],
            &job["source_id"],
            &job["url"],
            &job["translate"]
        ),
        (
            &json!("arxiv"),
            &json!("2401.12345"),
            &json!("https://arxiv.org/abs/2401.12345"),
            &json!("off")
        )
    );
    let (status, body) = app
        .post(
            "/api/jobs",
            json!({"url": "arXiv:2401.12345v1", "domain_id": ml}),
        )
        .await;
    assert_eq!((status, code(&body)), (StatusCode::CONFLICT, "conflict"));
    assert_eq!(body["job_id"], job["id"]);

    let papers = app.collection(ml, "papers").await;
    let other = json!({"url": "2401.12345", "domain_id": ml, "collection_id": papers});
    assert_eq!(
        app.post("/api/jobs", other).await.0,
        StatusCode::CREATED,
        "another collection"
    );
    let ai = app.domain("ai").await;
    let wrong = json!({"url": "2401.99999", "domain_id": ai, "collection_id": papers});
    assert_eq!(
        app.post("/api/jobs", wrong).await.0,
        StatusCode::NOT_FOUND,
        "collection of another domain"
    );
    let (_, list) = app.get("/api/jobs?limit=1").await;
    assert_eq!(list.as_array().unwrap().len(), 1);
    assert_eq!(list[0]["collection_id"], papers, "newest first");
}

#[tokio::test]
async fn files_stay_inside_the_job_and_support_range() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let (_, job) = app
        .post("/api/jobs", json!({"url": "2401.12345", "domain_id": ml}))
        .await;
    let id = job["id"].as_i64().unwrap();
    let dir = app.job_dir(id);
    let video: Vec<u8> = (0..1000u32).map(|i| (i % 251) as u8).collect();
    fs::create_dir_all(dir.join("media")).unwrap();
    fs::write(dir.join("media/video.mp4"), &video).unwrap();
    fs::write(
        dir.join("media/page.html"),
        "<script>fetch('/api/jobs')</script>",
    )
    .unwrap();
    fs::write(app.dir.path().join("secret.txt"), "secret").unwrap();
    std::os::unix::fs::symlink(app.dir.path().join("secret.txt"), dir.join("link.txt")).unwrap();

    let get = |uri: String, range: Option<&str>| {
        let mut req = HttpRequest::builder()
            .uri(uri)
            .header(AUTHORIZATION, auth());
        if let Some(range) = range {
            req = req.header(header::RANGE, range);
        }
        req.body(AxumBody::empty()).unwrap()
    };
    let (status, headers, body) = app
        .send(get(format!("/api/jobs/{id}/files/media/video.mp4"), None))
        .await;
    assert_eq!((status, body.len()), (StatusCode::OK, 1000));
    assert_eq!(headers[header::CONTENT_TYPE], "video/mp4");
    let range = Some("bytes=100-199");
    let (status, headers, body) = app
        .send(get(format!("/api/jobs/{id}/files/media/video.mp4"), range))
        .await;
    assert_eq!(status, StatusCode::PARTIAL_CONTENT);
    assert_eq!(headers[header::CONTENT_RANGE], "bytes 100-199/1000");
    assert_eq!(body, video[100..200]);
    // One policy value: a second CSP header would be enforced as well, not merged.
    let sandboxed = |headers: &axum::http::HeaderMap| {
        let policies: Vec<_> = headers
            .get_all(header::CONTENT_SECURITY_POLICY)
            .iter()
            .collect();
        policies == ["sandbox allow-same-origin; frame-ancestors 'none'"]
            && headers[header::X_CONTENT_TYPE_OPTIONS] == "nosniff"
    };
    assert!(sandboxed(&headers), "range response");
    let (status, headers, _) = app
        .send(get(format!("/api/jobs/{id}/files/media/page.html"), None))
        .await;
    assert_eq!(status, StatusCode::OK);
    assert!(
        headers[header::CONTENT_TYPE]
            .to_str()
            .unwrap()
            .starts_with("text/html")
    );
    assert!(sandboxed(&headers), "html response");
    let (_, headers, _) = app
        .send(get(format!("/api/jobs/{id}/files/missing"), None))
        .await;
    assert!(sandboxed(&headers), "404 response");

    for path in [
        "../../../secret.txt",
        "..%2F..%2F..%2Fsecret.txt",
        "link.txt",
        "%2Fetc%2Fpasswd",
        "media",
        "missing.txt",
    ] {
        let (status, _, body) = app
            .send(get(format!("/api/jobs/{id}/files/{path}"), None))
            .await;
        assert_eq!(status, StatusCode::NOT_FOUND, "{path}");
        assert_eq!(code(&serde_json::from_slice(&body).unwrap()), "not_found");
    }
    let (status, _, _) = app
        .send(get("/api/jobs/999/files/job.json".into(), None))
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn logs_return_whole_lines_from_an_offset() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let (id, log) = hold_log(&app, ml).await;
    let header = fs::read_to_string(&log).unwrap();
    fs::OpenOptions::new()
        .append(true)
        .open(&log)
        .and_then(|mut f| std::io::Write::write_all(&mut f, "第一行\npartial".as_bytes()))
        .unwrap();

    let (status, chunk) = app.get(&format!("/api/jobs/{id}/logs")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(chunk["text"], format!("{header}第一行\n"));
    let next = chunk["next_offset"].as_u64().unwrap();
    assert_eq!(next as usize, header.len() + "第一行\n".len());
    let (_, chunk) = app.get(&format!("/api/jobs/{id}/logs?offset={next}")).await;
    assert_eq!(
        (chunk["text"].as_str(), chunk["next_offset"].as_u64()),
        (Some(""), Some(next))
    );
    let (status, _) = app.get("/api/jobs/999/logs").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
    let past = fs::metadata(&log).unwrap().len() + 1;
    for offset in [
        "-1".to_owned(),
        past.to_string(),
        "9223372036854775808".into(),
        u64::MAX.to_string(),
    ] {
        let (status, body) = app
            .get(&format!("/api/jobs/{id}/logs?offset={offset}"))
            .await;
        assert_eq!(
            (status, code(&body)),
            (StatusCode::BAD_REQUEST, "invalid_request"),
            "{offset}"
        );
    }
    app.post(&format!("/api/jobs/{id}/cancel"), json!(null))
        .await;
}

/// A job whose second step hangs, so the test can append to job.log undisturbed.
async fn hold_log(app: &App, domain_id: i64) -> (i64, PathBuf) {
    let id = 1;
    fake(&app.state.config, id, "video-transcribe", "hang");
    let job = json!({"url": "https://youtu.be/dQw4w9WgXcQ", "domain_id": domain_id});
    let (_, job) = app.post("/api/jobs", job).await;
    assert_eq!(job["id"], id);
    let log = app.job_dir(id).join("job.log");
    let hanging = || {
        fs::read_to_string(&log).is_ok_and(|t| t.contains("video-transcribe attempt 1 token hang"))
    };
    wait_for("hanging step", hanging).await;
    (id, log)
}

#[tokio::test]
async fn log_lines_longer_than_a_chunk_split_between_characters() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let (id, log) = hold_log(&app, ml).await;
    // 256KiB from the start of this line ends inside the first 数.
    let line = format!("{}数数数\n", "a".repeat(256 * 1024 - 1));
    fs::OpenOptions::new()
        .append(true)
        .open(&log)
        .and_then(|mut f| std::io::Write::write_all(&mut f, line.as_bytes()))
        .unwrap();
    let whole = fs::read_to_string(&log).unwrap();
    let (mut text, mut offset) = (String::new(), 0);
    while offset < whole.len() as u64 {
        let (status, chunk) = app
            .get(&format!("/api/jobs/{id}/logs?offset={offset}"))
            .await;
        assert_eq!(status, StatusCode::OK);
        let piece = chunk["text"].as_str().unwrap();
        assert!(!piece.is_empty() && !piece.contains('\u{FFFD}'));
        text.push_str(piece);
        offset = chunk["next_offset"].as_u64().unwrap();
    }
    assert_eq!(text, whole);
    app.post(&format!("/api/jobs/{id}/cancel"), json!(null))
        .await;
}

#[tokio::test]
async fn detail_merges_manifests_and_builds_the_obsidian_link() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let cfg = &app.state.config;
    let manifest = |title: &str, files: Value, assets: Value, meta: Value| {
        json!({"title": title, "files": files, "assets": assets, "meta": meta}).to_string()
    };
    fake(
        cfg,
        1,
        "video-fetch.json",
        &manifest("Talk", json!({"video": "video.mp4"}), json!([]), json!({})),
    );
    let frames = manifest(
        "x",
        json!({"transcript": "transcript.md", "sections": "s.json"}),
        json!(["frame-0.jpg"]),
        json!({}),
    );
    fake(cfg, 1, "video-frames.json", &frames);
    let translate = manifest(
        "y",
        json!({"transcript": "transcript.md"}),
        json!([]),
        json!({"skipped_reason": "chinese"}),
    );
    fake(cfg, 1, "translate.json", &translate);
    let meta = json!({"vault_path": "flori/ml/unsorted/2026-11-08-yt-dQw4w9WgXcQ/", "note_file": "数学 & AI.md"});
    fake(
        cfg,
        1,
        "publish.json",
        &manifest("z", json!({}), json!([]), meta),
    );
    let (_, job) = app
        .post(
            "/api/jobs",
            json!({"url": "https://youtu.be/dQw4w9WgXcQ", "domain_id": ml}),
        )
        .await;
    assert_eq!(job["id"], 1);
    wait_status(&app.state.db, 1, JobStatus::Succeeded).await;

    let (status, detail) = app.get("/api/jobs/1").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(detail["job"]["title"], "Talk");
    assert_eq!(
        detail["files"],
        json!([
            {"name": "sections", "path": "steps/03-video-frames/s.json"},
            {"name": "transcript", "path": "steps/05-translate/transcript.md"},
            {"name": "video", "path": "steps/01-video-fetch/video.mp4"},
        ])
    );
    assert_eq!(
        detail["assets"],
        json!({"frame-0.jpg": "steps/03-video-frames/assets/frame-0.jpg"})
    );
    assert_eq!(detail["steps"][4]["skipped_reason"], "chinese");
    assert_eq!(detail["steps"][0]["skipped_reason"], Value::Null);
    assert_eq!(detail["preview_base"], "/api/jobs/1/files/");
    assert_eq!(detail["job_url"], "http://nas.lan:8780/jobs/1");
    assert_eq!(
        detail["vault_path"],
        "flori/ml/unsorted/2026-11-08-yt-dQw4w9WgXcQ/"
    );
    assert_eq!(
        detail["obsidian_uri"],
        "obsidian://open?vault=flori&file=ml%2Funsorted%2F2026-11-08-yt-dQw4w9WgXcQ%2F%E6%95%B0%E5%AD%A6%20%26%20AI.md"
    );
    let (status, _) = app.get("/api/jobs/2").await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn retry_resumes_at_the_failed_step() {
    let app = app().await;
    let ml = app.domain("ml").await;
    fake(&app.state.config, 1, "note", "20 0");
    let (_, job) = app
        .post(
            "/api/jobs",
            json!({"url": "https://youtu.be/dQw4w9WgXcQ", "domain_id": ml}),
        )
        .await;
    let id = job["id"].as_i64().unwrap();
    let failed = wait_status(&app.state.db, id, JobStatus::Failed).await;
    assert_eq!(failed.current_step, Some(StepName::Note));
    let (status, body) = app
        .post(&format!("/api/jobs/{id}/cancel"), json!(null))
        .await;
    assert_eq!((status, code(&body)), (StatusCode::CONFLICT, "conflict"));

    let (status, body) = app
        .post(&format!("/api/jobs/{id}/retry"), json!(null))
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    wait_status(&app.state.db, id, JobStatus::Succeeded).await;
    let (_, detail) = app.get(&format!("/api/jobs/{id}")).await;
    let attempts: Vec<u64> = detail["steps"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| s["attempts"].as_u64().unwrap())
        .collect();
    assert_eq!(attempts, [1, 1, 1, 2, 1, 1]);
    assert_eq!(detail["steps"][3]["error"], Value::Null);
    let log = fs::read_to_string(app.job_dir(id).join("job.log")).unwrap();
    assert!(log.contains("== 04-note attempt 1 ==") && log.contains("== 04-note attempt 2 =="));
    assert_eq!(log.matches("== 01-video-fetch attempt").count(), 1, "{log}");
    let (status, _) = app
        .post(&format!("/api/jobs/{id}/retry"), json!(null))
        .await;
    assert_eq!(status, StatusCode::CONFLICT);
    let (status, _) = app.post("/api/jobs/999/retry", json!(null)).await;
    assert_eq!(status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn cancel_kills_the_whole_process_group_and_retry_continues() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let cfg = &app.state.config;
    fake(cfg, 1, "video-transcribe", "hang 0");
    let (_, job) = app
        .post(
            "/api/jobs",
            json!({"url": "https://youtu.be/dQw4w9WgXcQ", "domain_id": ml}),
        )
        .await;
    let id = job["id"].as_i64().unwrap();
    let pid_file = app.job_dir(id).join("fake/video-transcribe.pid");
    wait_for("hanging step", || pid_file.exists()).await;
    let pid: i32 = fs::read_to_string(&pid_file)
        .unwrap()
        .trim()
        .parse()
        .unwrap();
    assert!(!gone(pid));

    let (status, body) = app
        .post(&format!("/api/jobs/{id}/cancel"), json!(null))
        .await;
    assert_eq!(status, StatusCode::OK, "{body}");
    assert_eq!(
        (&body["status"], &body["current_step"]),
        (&json!("canceled"), &json!("video-transcribe"))
    );
    wait_for("grandchild exit", || gone(pid)).await;
    let tmp = app.job_dir(id).join("steps/02-video-transcribe.tmp");
    wait_for("tmp removal", || !tmp.exists()).await;
    let steps = db::steps(&app.state.db, id).await.unwrap();
    assert_eq!(steps[1].status, StepStatus::Canceled);
    assert_eq!(
        db::get_job(&app.state.db, id)
            .await
            .unwrap()
            .unwrap()
            .0
            .status,
        JobStatus::Canceled
    );

    let (status, _) = app
        .post(&format!("/api/jobs/{id}/retry"), json!(null))
        .await;
    assert_eq!(status, StatusCode::OK);
    wait_status(&app.state.db, id, JobStatus::Succeeded).await;
    let log = fs::read_to_string(app.job_dir(id).join("job.log")).unwrap();
    let first = log.find("== 02-video-transcribe attempt 1 ==").unwrap();
    assert!(
        log[first..].contains("== 02-video-transcribe attempt 2 =="),
        "{log}"
    );
}

#[tokio::test]
async fn feeds_bind_list_and_submit_in_batches() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let coll = app.collection(ml, "channel").await;
    let feed = format!("/api/collections/{coll}/feed");
    let put = |url: Value| app.call("PUT", &feed, Some(json!({"url": url})));

    let (status, body) = app.get(&feed).await;
    assert_eq!(
        (status, code(&body)),
        (StatusCode::BAD_REQUEST, "invalid_request"),
        "no feed bound"
    );
    let (status, body) = put(json!("https://www.youtube.com/watch?v=dQw4w9WgXcQ")).await;
    assert_eq!(
        (status, code(&body)),
        (StatusCode::BAD_REQUEST, "invalid_url")
    );
    let (status, body) = put(json!("https://m.youtube.com/@demo/featured")).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(
        body["feed"],
        json!({"kind": "yt_channel", "url": "https://www.youtube.com/@demo/videos"})
    );
    let (status, _) = app
        .call(
            "PUT",
            "/api/collections/999/feed",
            Some(json!({"url": null})),
        )
        .await;
    assert_eq!(status, StatusCode::NOT_FOUND);

    for limit in [0, 101] {
        let (status, body) = app.get(&format!("{feed}?limit={limit}")).await;
        assert_eq!(
            (status, code(&body)),
            (StatusCode::BAD_REQUEST, "invalid_request"),
            "{limit}"
        );
    }
    let (status, list) = app.get(&format!("{feed}?limit=3")).await;
    assert_eq!(status, StatusCode::OK, "{list}");
    assert_eq!(list["items"].as_array().unwrap().len(), 3);
    assert_eq!(list["items"][0]["job"], Value::Null);
    // feed.py starts from a cleared environment plus the proxy.
    assert_eq!(
        list["title"],
        "fake yt_channel env=FLORI_PROXY,HOME,LANG,PATH"
    );

    let jobs = format!("{feed}/jobs");
    let picks = |n: usize| -> Vec<Value> {
        (0..n)
            .map(|i| json!({"platform": "youtube", "source_id": format!("fakevideo{i:02}")}))
            .collect()
    };
    for items in [picks(21), picks(0)] {
        let (status, body) = app.post(&jobs, json!({"items": items})).await;
        assert_eq!(
            (status, code(&body)),
            (StatusCode::BAD_REQUEST, "invalid_request")
        );
    }
    let bad = json!({"items": [{"platform": "youtube", "source_id": "../../x"}]});
    assert_eq!(app.post(&jobs, bad).await.0, StatusCode::BAD_REQUEST);
    let arxiv = json!({"items": [{"platform": "arxiv", "source_id": "2401.12345"}]});
    assert_eq!(app.post(&jobs, arxiv).await.0, StatusCode::BAD_REQUEST);

    let batch = json!({"items": picks(2), "translate": "off"});
    let (status, first) = app.post(&jobs, batch.clone()).await;
    assert_eq!(status, StatusCode::OK, "{first}");
    assert_eq!(
        (
            first["created"].as_array().unwrap().len(),
            first["skipped"].as_array().unwrap().len()
        ),
        (2, 0)
    );
    let (status, again) = app.post(&jobs, batch).await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(again["created"], json!([]));
    let skipped: Vec<&Value> = again["skipped"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| &s["job"]["id"])
        .collect();
    let created: Vec<&Value> = first["created"]
        .as_array()
        .unwrap()
        .iter()
        .map(|s| &s["job"]["id"])
        .collect();
    assert_eq!(skipped, created);
    let (_, job) = app.get(&format!("/api/jobs/{}", created[0])).await;
    assert_eq!(
        job["job"]["url"],
        "https://www.youtube.com/watch?v=fakevideo00"
    );
    assert_eq!(job["job"]["translate"], "off");

    after_interval().await;
    let (_, list) = app.get(&format!("{feed}?limit=3")).await;
    assert_eq!(list["items"][0]["job"]["id"], *created[0]);
    assert_eq!(list["items"][2]["job"], Value::Null);

    let (status, body) = put(json!("https://www.youtube.com/@failing")).await;
    assert_eq!(status, StatusCode::OK, "{body}");
    after_interval().await;
    let (status, body) = app.get(&feed).await;
    assert_eq!((status, code(&body)), (StatusCode::BAD_GATEWAY, "upstream"));
    assert_eq!(body["message"], "upstream refused the request");
    let (status, body) = put(Value::Null).await;
    assert_eq!((status, &body["feed"]), (StatusCode::OK, &Value::Null));
}

#[tokio::test]
async fn only_one_feed_refresh_runs_at_a_time() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let coll = app.collection(ml, "slow").await;
    let feed = format!("/api/collections/{coll}/feed");
    let body = json!({"url": "https://www.youtube.com/@slowchannel"});
    assert_eq!(app.call("PUT", &feed, Some(body)).await.0, StatusCode::OK);

    let first = tokio::spawn({
        let router = app.router.clone();
        let req = HttpRequest::builder()
            .uri(&feed)
            .header(AUTHORIZATION, auth())
            .body(AxumBody::empty())
            .unwrap();
        async move { router.oneshot(req).await.unwrap().status() }
    });
    tokio::time::sleep(Duration::from_millis(500)).await;
    let (status, body) = app.get(&feed).await;
    assert_eq!((status, code(&body)), (StatusCode::CONFLICT, "conflict"));
    assert!(
        body["message"]
            .as_str()
            .unwrap()
            .contains("already running")
    );
    assert_eq!(first.await.unwrap(), StatusCode::OK);
    after_interval().await;
    assert_eq!(app.get(&format!("{feed}?limit=1")).await.0, StatusCode::OK);
}

async fn after_interval() {
    tokio::time::sleep(FEED_INTERVAL + Duration::from_millis(50)).await;
}

#[tokio::test]
async fn feed_refreshes_wait_out_the_interval() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let coll = app.collection(ml, "feed").await;
    let feed = format!("/api/collections/{coll}/feed");
    let put = |url: &str| app.call("PUT", &feed, Some(json!({"url": url})));
    assert_eq!(put("https://www.youtube.com/@demo").await.0, StatusCode::OK);
    assert_eq!(app.get(&feed).await.0, StatusCode::OK);
    let (status, body) = app.get(&feed).await;
    assert_eq!((status, code(&body)), (StatusCode::CONFLICT, "conflict"));
    assert!(
        body["message"].as_str().unwrap().contains("try again"),
        "{body}"
    );
    after_interval().await;
    // A failed refresh starts the interval as well.
    assert_eq!(
        put("https://www.youtube.com/@failing").await.0,
        StatusCode::OK
    );
    assert_eq!(app.get(&feed).await.0, StatusCode::BAD_GATEWAY);
    assert_eq!(app.get(&feed).await.0, StatusCode::CONFLICT);
    after_interval().await;
    assert_eq!(app.get(&feed).await.0, StatusCode::BAD_GATEWAY);
}

/// The fake feed.py accepts only the contract's normal forms, so this catches a parser that
/// stores something feed.py would refuse.
#[tokio::test]
async fn every_feed_kind_binds_to_a_form_feed_py_accepts() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let coll = app.collection(ml, "feeds").await;
    let feed = format!("/api/collections/{coll}/feed");
    for input in [
        "https://space.bilibili.com/946974/video",
        "https://space.bilibili.com/100/channel/collectiondetail?sid=200",
        "https://space.bilibili.com/100/lists/200?type=series",
        "https://space.bilibili.com/247209804/favlist?fid=987654&ftype=create",
        "https://www.bilibili.com/medialist/detail/ml987654",
        "https://www.youtube.com/channel/UCYO_jab_esuFRV4b17AJtAw/featured",
        "https://www.youtube.com/playlist?list=PLZHQObOWTQDPD3MizzM2xVFitgF8hE_ab",
    ] {
        after_interval().await;
        let (status, body) = app.call("PUT", &feed, Some(json!({"url": input}))).await;
        assert_eq!(status, StatusCode::OK, "{input}: {body}");
        let (status, list) = app.get(&format!("{feed}?limit=1")).await;
        assert_eq!(status, StatusCode::OK, "{input}: {list}");
        assert_eq!(list["items"].as_array().unwrap().len(), 1);
    }
}

#[tokio::test]
async fn writes_from_other_sites_are_refused() {
    let app = app().await;
    let ml = app.domain("ml").await;
    hold_log(&app, ml).await;
    let cancel = |headers: &[(&str, &str)]| {
        let mut req = HttpRequest::builder()
            .method("POST")
            .uri("/api/jobs/1/cancel")
            .header(AUTHORIZATION, auth());
        for (name, value) in headers {
            req = req.header(*name, *value);
        }
        req.body(AxumBody::empty()).unwrap()
    };
    for headers in [
        &[("sec-fetch-site", "cross-site")][..],
        &[("sec-fetch-site", "same-site")],
        &[("origin", "http://evil.example"), ("host", "nas.lan:8780")],
        &[("origin", "null"), ("host", "nas.lan:8780")],
    ] {
        let (status, _, _) = app.send(cancel(headers)).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{headers:?}");
    }
    // Only GET and HEAD skip the write rule; OPTIONS and TRACE do not.
    let evil = [("origin", "http://evil.example"), ("host", "nas.lan:8780")];
    let (status, _, _) = app
        .send(browser("OPTIONS", "/api/jobs/1/cancel", &evil))
        .await;
    assert_eq!(status, StatusCode::FORBIDDEN, "OPTIONS");
    let (status, _, body) = app.send(browser("GET", "/api/jobs/1", &[])).await;
    assert_eq!(status, StatusCode::OK);
    let detail: Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        detail["job"]["status"], "running",
        "refused cancels changed nothing"
    );
    let same = [
        ("sec-fetch-site", "same-origin"),
        ("origin", "http://nas.lan:8780"),
        ("host", "nas.lan:8780"),
    ];
    let (status, _, _) = app.send(cancel(&same)).await;
    assert_eq!(status, StatusCode::OK);
}

/// An authenticated request with the given headers.
fn browser(method: &str, uri: &str, headers: &[(&str, &str)]) -> HttpRequest<AxumBody> {
    let mut req = HttpRequest::builder()
        .method(method)
        .uri(uri)
        .header(AUTHORIZATION, auth())
        .header(header::CONTENT_TYPE, "application/json");
    for (name, value) in headers {
        req = req.header(*name, *value);
    }
    let body = if method == "POST" {
        AxumBody::from(r#"{"name":"x"}"#)
    } else {
        AxumBody::empty()
    };
    req.body(body).unwrap()
}

#[tokio::test]
async fn every_response_forbids_framing() {
    let app = app().await;
    let anonymous = HttpRequest::builder()
        .uri("/api/jobs")
        .body(AxumBody::empty())
        .unwrap();
    let health = HttpRequest::builder()
        .uri("/api/health")
        .body(AxumBody::empty())
        .unwrap();
    let cross_post = browser("POST", "/api/domains", &[("sec-fetch-site", "cross-site")]);
    for (what, req, expected) in [
        ("page", browser("GET", "/", &[]), StatusCode::OK),
        ("api", browser("GET", "/api/jobs", &[]), StatusCode::OK),
        ("401", anonymous, StatusCode::UNAUTHORIZED),
        ("403", cross_post, StatusCode::FORBIDDEN),
        ("health", health, StatusCode::OK),
    ] {
        let (status, headers, _) = app.send(req).await;
        assert_eq!(status, expected, "{what}");
        assert_eq!(headers[header::X_FRAME_OPTIONS], "DENY", "{what}");
        let policies: Vec<_> = headers
            .get_all(header::CONTENT_SECURITY_POLICY)
            .iter()
            .collect();
        assert_eq!(policies, ["frame-ancestors 'none'"], "{what}");
    }
}

#[tokio::test]
async fn other_sites_and_frames_reach_only_top_level_pages() {
    let app = app().await;
    let refused = [
        (
            "GET",
            "/api/collections/999/feed",
            vec![("sec-fetch-site", "cross-site")],
        ),
        (
            "GET",
            "/api/collections/999/feed",
            vec![("sec-fetch-site", "same-site")],
        ),
        (
            "GET",
            "/api/jobs",
            vec![
                ("sec-fetch-site", "cross-site"),
                ("sec-fetch-mode", "navigate"),
                ("sec-fetch-dest", "document"),
            ],
        ),
        (
            "GET",
            "/api/jobs/1/files/x",
            vec![
                ("sec-fetch-site", "cross-site"),
                ("sec-fetch-dest", "image"),
            ],
        ),
        (
            "GET",
            "/",
            vec![
                ("sec-fetch-site", "cross-site"),
                ("sec-fetch-dest", "image"),
            ],
        ),
        (
            "GET",
            "/jobs/1",
            vec![
                ("sec-fetch-site", "cross-site"),
                ("sec-fetch-mode", "no-cors"),
                ("sec-fetch-dest", "document"),
            ],
        ),
        (
            "GET",
            "/",
            vec![
                ("sec-fetch-site", "same-origin"),
                ("sec-fetch-dest", "iframe"),
            ],
        ),
        (
            "GET",
            "/",
            vec![
                ("sec-fetch-site", "same-origin"),
                ("sec-fetch-dest", "frame"),
            ],
        ),
        ("GET", "/", vec![("sec-fetch-dest", "embed")]),
        ("GET", "/", vec![("sec-fetch-dest", "object")]),
        (
            "POST",
            "/api/domains",
            vec![
                ("sec-fetch-site", "same-origin"),
                ("sec-fetch-dest", "iframe"),
            ],
        ),
    ];
    for (method, uri, headers) in refused {
        let (status, _, _) = app.send(browser(method, uri, &headers)).await;
        assert_eq!(status, StatusCode::FORBIDDEN, "{method} {uri} {headers:?}");
    }
    let navigate = [
        ("sec-fetch-site", "cross-site"),
        ("sec-fetch-mode", "navigate"),
        ("sec-fetch-dest", "document"),
    ];
    let (status, _, body) = app.send(browser("GET", "/jobs/1", &navigate)).await;
    assert_eq!(status, StatusCode::OK, "a job_url link from a note opens");
    assert!(String::from_utf8(body).unwrap().contains("index"));
    let allowed = [
        (
            "GET",
            "/",
            vec![
                ("sec-fetch-site", "none"),
                ("sec-fetch-mode", "navigate"),
                ("sec-fetch-dest", "document"),
            ],
        ),
        (
            "GET",
            "/api/jobs",
            vec![
                ("sec-fetch-site", "same-origin"),
                ("sec-fetch-dest", "empty"),
            ],
        ),
        ("GET", "/api/domains", vec![("sec-fetch-site", "none")]),
        (
            "GET",
            "/app.js",
            vec![
                ("sec-fetch-site", "same-origin"),
                ("sec-fetch-dest", "script"),
            ],
        ),
    ];
    for (method, uri, headers) in allowed {
        let (status, _, _) = app.send(browser(method, uri, &headers)).await;
        assert_eq!(status, StatusCode::OK, "{method} {uri} {headers:?}");
    }
    // The SPA's own page loads, images and video ranges, as the browser labels them.
    let same_page = [
        ("sec-fetch-site", "same-origin"),
        ("sec-fetch-mode", "navigate"),
        ("sec-fetch-dest", "document"),
    ];
    assert_eq!(
        app.send(browser("GET", "/jobs/1", &same_page)).await.0,
        StatusCode::OK
    );
    let ml = app.domain("ml").await;
    let (_, job) = app
        .post("/api/jobs", json!({"url": "2401.12345", "domain_id": ml}))
        .await;
    let media = app.job_dir(job["id"].as_i64().unwrap()).join("media");
    fs::create_dir_all(&media).unwrap();
    fs::write(media.join("fig.png"), "png").unwrap();
    fs::write(media.join("video.mp4"), vec![0u8; 1000]).unwrap();
    let files = format!("/api/jobs/{}/files/media", job["id"]);
    let image = [
        ("sec-fetch-site", "same-origin"),
        ("sec-fetch-mode", "no-cors"),
        ("sec-fetch-dest", "image"),
    ];
    let (status, _, _) = app
        .send(browser("GET", &format!("{files}/fig.png"), &image))
        .await;
    assert_eq!(status, StatusCode::OK, "image in a preview");
    let video = [
        ("sec-fetch-site", "same-origin"),
        ("sec-fetch-mode", "no-cors"),
        ("sec-fetch-dest", "video"),
        ("range", "bytes=0-99"),
    ];
    let (status, _, _) = app
        .send(browser("GET", &format!("{files}/video.mp4"), &video))
        .await;
    assert_eq!(status, StatusCode::PARTIAL_CONTENT, "video playback");
    // Refused before authentication: a foreign page gets no 401 login prompt.
    let anonymous = HttpRequest::builder()
        .uri("/api/jobs")
        .header("sec-fetch-site", "cross-site")
        .body(AxumBody::empty())
        .unwrap();
    assert_eq!(app.send(anonymous).await.0, StatusCode::FORBIDDEN);
    let same = [
        ("sec-fetch-site", "same-origin"),
        ("sec-fetch-dest", "empty"),
    ];
    let (status, _, _) = app.send(browser("POST", "/api/domains", &same)).await;
    assert_eq!(status, StatusCode::CREATED);
}

#[tokio::test]
async fn a_refresh_dropped_midway_still_starts_the_interval() {
    let app = app().await;
    let ml = app.domain("ml").await;
    let coll = app.collection(ml, "slow").await;
    let feed = format!("/api/collections/{coll}/feed");
    let bind = json!({"url": "https://www.youtube.com/@slowchannel"});
    assert_eq!(app.call("PUT", &feed, Some(bind)).await.0, StatusCode::OK);
    let refresh = tokio::spawn({
        let router = app.router.clone();
        let req = browser("GET", &feed, &[]);
        async move { router.oneshot(req).await }
    });
    // Lets the spawned request reach the lock first; "already running" then proves it started.
    tokio::time::sleep(Duration::from_millis(100)).await;
    loop {
        let (status, body) = app.get(&feed).await;
        assert_eq!(status, StatusCode::CONFLICT, "{body}");
        if body["message"]
            .as_str()
            .unwrap()
            .contains("already running")
        {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    // The client goes away: the handler and its feed.py run are dropped.
    refresh.abort();
    assert!(refresh.await.unwrap_err().is_cancelled());
    let (status, body) = app.get(&feed).await;
    assert_eq!((status, code(&body)), (StatusCode::CONFLICT, "conflict"));
    assert!(
        body["message"].as_str().unwrap().contains("try again"),
        "{body}"
    );
}
