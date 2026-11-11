//! Routes, Basic Auth, static files with SPA fallback, job file preview with Range, feed listing.

use std::{
    collections::BTreeMap,
    io,
    io::SeekFrom,
    sync::{Arc, PoisonError},
    time::Duration,
};

use axum::{
    Extension, Json, Router,
    extract::{
        FromRequest, FromRequestParts, Path, Query, Request, State, rejection::PathRejection,
    },
    http::{
        HeaderValue, Method, StatusCode,
        header::{
            AUTHORIZATION, CACHE_CONTROL, CONTENT_SECURITY_POLICY, HOST, ORIGIN, REFERRER_POLICY,
            WWW_AUTHENTICATE, X_CONTENT_TYPE_OPTIONS, X_FRAME_OPTIONS,
        },
        request::Parts,
    },
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{any, get, post, put},
};
use base64::{Engine, engine::general_purpose::STANDARD as BASE64};
use serde::de::DeserializeOwned;
use sha2::{Digest, Sha256};
use sqlx::SqlitePool;
use tokio::{
    io::{AsyncReadExt, AsyncSeekExt},
    process::Command,
    sync::Mutex,
    time::Instant,
};
use tower_http::services::{ServeDir, ServeFile};

use crate::{
    Config,
    api::{
        AiCall, ApiError, Collection, Domain, ErrorCode, FeedList, FeedPicked, FeedQuery,
        FeedSubmit, FeedSubmitResult, Health, JobDetail, JobFile, JobRef, JobSummary, JobsQuery,
        LogChunk, LogQuery, NewCollection, NewDomain, Punctuation, SetFeed, StepStatus, SubmitJob,
    },
    db::{self, conflict, job_dir, not_found},
    exec::{self, ExecHandle, Stop},
    pipeline::{
        FeedKind, MetaValue, StepName, TranslateMode, feed_kind_arg, parse_feed, parse_source,
        read_job, read_manifest, step_dir_name, video_source,
    },
};

const FEED_TIMEOUT: Duration = Duration::from_secs(60);
/// Least time between the starts of two feed refreshes, so a page that keeps requesting the feed
/// cannot hammer Bilibili with the user's SESSDATA. Counting from the start also covers a
/// refresh whose client went away mid-run.
#[cfg(not(test))]
const FEED_INTERVAL: Duration = Duration::from_secs(10);
#[cfg(test)]
const FEED_INTERVAL: Duration = Duration::from_millis(300);
const LOG_CHUNK: u64 = 256 * 1024;
/// Wrong credentials are counted per window. Past the limit each further one is answered after
/// the delay, so a guesser gets one try per delay on each connection it holds. The delay is fixed
/// and nothing is locked: the user's own requests share the count.
const FAILURE_WINDOW: Duration = Duration::from_secs(60);
const FAILURE_LIMIT: u32 = 10;
const FAILURE_DELAY: Duration = Duration::from_secs(2);
/// Characters kept of each request field in an auth failure line. hyper only bounds the request
/// head as a whole, so a long method, path or Host would otherwise flood the log.
const LOGGED_CHARS: usize = 200;
/// CSP of the SPA's pages. markdown-it writes table alignment as `style` attributes, hence
/// 'unsafe-inline' for styles but not scripts; its images may be data: URLs. Images on other
/// hosts stay unloaded, so previewing an untrusted note does not reveal the reader to them.
const PAGE_POLICY: &str = "default-src 'self'; img-src 'self' data:; media-src 'self'; \
    style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'self'; \
    frame-ancestors 'none'";

#[derive(Clone)]
pub struct AppState {
    pub db: SqlitePool,
    pub exec: ExecHandle,
    pub config: Arc<Config>,
}

/// Held while feed.py runs, with the time the last refresh started. A refresh while another runs
/// or within `FEED_INTERVAL` of the last start gets 409 instead of queueing.
#[derive(Clone, Default)]
pub struct FeedRefresh(Arc<Mutex<Option<Instant>>>);

pub fn router(state: AppState) -> Router {
    let credentials = format!("{}:{}", state.config.web_user, state.config.web_password);
    let auth = Auth::new(&credentials);
    let web = ServeDir::new(&state.config.static_dir)
        .fallback(ServeFile::new(state.config.static_dir.join("index.html")));
    Router::new()
        .route("/api/domains", get(list_domains).post(create_domain))
        .route("/api/domains/{id}", axum::routing::delete(delete_domain))
        .route(
            "/api/collections",
            get(list_collections).post(create_collection),
        )
        .route(
            "/api/collections/{id}",
            axum::routing::delete(delete_collection),
        )
        .route("/api/collections/{id}/feed", put(set_feed).get(list_feed))
        .route("/api/collections/{id}/feed/jobs", post(submit_feed))
        .route("/api/jobs", get(list_jobs).post(submit_job))
        .route("/api/jobs/{id}", get(get_job))
        .route("/api/jobs/{id}/logs", get(get_logs))
        .route("/api/jobs/{id}/retry", post(retry_job))
        .route("/api/jobs/{id}/cancel", post(cancel_job))
        .route(
            "/api/jobs/{id}/files/{*path}",
            get(get_file).layer(middleware::map_response(sandbox)),
        )
        // The wildcard needs a non-empty rest. Without the two exact routes the fallback would
        // serve the page at /api and /api/, under the API's CSP instead of PAGE_POLICY.
        .route("/api", any(no_route))
        .route("/api/", any(no_route))
        .route("/api/{*rest}", any(no_route))
        .fallback_service(web)
        .layer(Extension(FeedRefresh::default()))
        .layer(middleware::from_fn_with_state(auth, basic_auth))
        // Added after the auth layer, so it is the one route without it.
        .route("/api/health", get(health))
        // Outermost, so 401, 403 and health responses carry its headers too.
        .layer(middleware::from_fn(guard))
        .with_state(state)
}

/// Refuses requests a browser makes on behalf of another site or inside a frame, and forbids
/// framing every response. Browsers attach cached Basic credentials to those requests, and a
/// framed page can be clicked through by its parent. LAN http and curl send no `Sec-Fetch-*`
/// headers; for them the response headers and the Origin check on writes remain.
async fn guard(req: Request, next: Next) -> Response {
    let api = is_api(req.uri().path());
    let mut res = if foreign(&req) {
        (StatusCode::FORBIDDEN, "cross-site request refused").into_response()
    } else {
        next.run(req).await
    };
    let headers = res.headers_mut();
    headers.insert(X_FRAME_OPTIONS, HeaderValue::from_static("DENY"));
    // Page addresses name this server and the job; sites that links point to must not see them.
    // A native form POST then carries `Origin: null` and is refused as foreign; the SPA writes
    // with fetch, whose cors mode keeps the real Origin.
    headers.insert(REFERRER_POLICY, HeaderValue::from_static("no-referrer"));
    // The files route sets its own sandbox policy, which already forbids framing. A second CSP
    // header would be enforced as well, so the default only fills in a missing one.
    let policy = if api {
        "frame-ancestors 'none'"
    } else {
        PAGE_POLICY
    };
    let policy = HeaderValue::from_static(policy);
    headers.entry(CONTENT_SECURITY_POLICY).or_insert(policy);
    if api {
        // Basic Auth has no logout: notes and video must not stay in a borrowed device's cache.
        headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    }
    res
}

fn is_api(path: &str) -> bool {
    path == "/api" || path.starts_with("/api/")
}

fn foreign(req: &Request) -> bool {
    let header = |name: &str| req.headers().get(name).and_then(|v| v.to_str().ok());
    let site = header("sec-fetch-site");
    let other_site = matches!(site, Some("cross-site" | "same-site"));
    let dest = header("sec-fetch-dest");
    let framed = matches!(dest, Some("iframe" | "frame" | "embed" | "object"));
    // Only a top-level navigation may come from elsewhere, so job_url links in notes open.
    let navigation = header("sec-fetch-mode") == Some("navigate") && dest == Some("document");
    let api = is_api(req.uri().path());
    let write = !matches!(*req.method(), Method::GET | Method::HEAD);
    let write_site = site.is_some_and(|site| site != "same-origin" && site != "none");
    let write_origin = header(ORIGIN.as_str()).is_some_and(|origin| {
        origin.split_once("://").map(|(_, host)| host) != header(HOST.as_str())
    });
    framed || (other_site && (api || !navigation)) || (write && (write_site || write_origin))
}

/// Digest of the expected `user:password` and the start and size of the current failure window.
/// The window is shared by all clients: every public request arrives from the ECS forwarder's
/// address, so counting or locking out per address would hit the user as well.
#[derive(Clone)]
struct Auth {
    expected: [u8; 32],
    failures: Arc<std::sync::Mutex<Option<(Instant, u32)>>>,
}

impl Auth {
    /// `credentials` is `user:password`, as Basic Auth sends it.
    fn new(credentials: &str) -> Auth {
        Auth {
            expected: Sha256::digest(credentials).into(),
            failures: Arc::default(),
        }
    }

    /// Records one wrong attempt and returns how many the current window holds.
    fn fail(&self) -> u32 {
        let now = Instant::now();
        let mut failures = self.failures.lock().unwrap_or_else(PoisonError::into_inner);
        let (start, count) = failures.get_or_insert((now, 0));
        if now.duration_since(*start) >= FAILURE_WINDOW {
            (*start, *count) = (now, 0);
        }
        *count = count.saturating_add(1);
        *count
    }
}

/// Compares digests of the whole `user:password`, so neither length nor prefix leaks. A request
/// without Authorization is a browser's first try, answered so it asks the user; only wrong
/// credentials are logged and counted. Correct ones never touch the count or wait.
async fn basic_auth(State(auth): State<Auth>, req: Request, next: Next) -> Response {
    let Some(given) = req.headers().get(AUTHORIZATION) else {
        return challenge();
    };
    let digest = given
        .to_str()
        .ok()
        .and_then(|v| v.strip_prefix("Basic "))
        .and_then(|v| BASE64.decode(v.trim()).ok())
        .map(|credentials| <[u8; 32]>::from(Sha256::digest(credentials)));
    let ok = digest.is_some_and(|digest| {
        let diff = digest
            .iter()
            .zip(&auth.expected)
            .fold(0, |acc, (a, b)| acc | (a ^ b));
        std::hint::black_box(diff) == 0
    });
    if ok {
        return next.run(req).await;
    }
    let failures = auth.fail();
    let (line, window) = (refused(&req), FAILURE_WINDOW.as_secs());
    if failures > FAILURE_LIMIT {
        let delay = FAILURE_DELAY.as_secs();
        eprintln!("flori: {line}, {failures} in {window}s, delayed {delay}s");
        tokio::time::sleep(FAILURE_DELAY).await;
    } else {
        eprintln!("flori: {line}, {failures} in {window}s");
    }
    challenge()
}

fn challenge() -> Response {
    let challenge = [(WWW_AUTHENTICATE, "Basic realm=\"flori\", charset=\"UTF-8\"")];
    (StatusCode::UNAUTHORIZED, challenge).into_response()
}

/// What a wrong-credential line records: method, path and Host. Never the Authorization header,
/// whose user name and password may be the real ones mistyped, and never the query.
fn refused(req: &Request) -> String {
    let host = req.headers().get(HOST).and_then(|v| v.to_str().ok());
    format!(
        "wrong credentials: {} {:?} host {:?}",
        clip(req.method().as_str()),
        clip(req.uri().path()),
        clip(host.unwrap_or("-"))
    )
}

fn clip(field: &str) -> &str {
    field
        .char_indices()
        .nth(LOGGED_CHARS)
        .map_or(field, |(end, _)| &field[..end])
}

/// Job files include fetched HTML; it must not run scripts or submit forms as this origin with the
/// user's credentials. `allow-same-origin` keeps its images same-origin, so they still load.
async fn sandbox(mut res: Response) -> Response {
    let headers = res.headers_mut();
    let policy = HeaderValue::from_static("sandbox allow-same-origin; frame-ancestors 'none'");
    headers.insert(CONTENT_SECURITY_POLICY, policy);
    headers.insert(X_CONTENT_TYPE_OPTIONS, HeaderValue::from_static("nosniff"));
    res
}

/// Handler error: a contract error body, or a 500 for broken database or disk.
pub enum Error {
    Api(ApiError),
    Internal(String),
}

impl From<ApiError> for Error {
    fn from(err: ApiError) -> Self {
        Error::Api(err)
    }
}

/// Database and disk failures; contract errors arrive as `ApiError`.
impl<E: std::error::Error> From<E> for Error {
    fn from(err: E) -> Self {
        Error::Internal(err.to_string())
    }
}

impl IntoResponse for Error {
    fn into_response(self) -> Response {
        match self {
            Error::Api(err) => err.into_response(),
            Error::Internal(message) => {
                eprintln!("flori: {message}");
                (StatusCode::INTERNAL_SERVER_ERROR, "internal error").into_response()
            }
        }
    }
}

type Res<T> = Result<T, Error>;

fn invalid(message: impl Into<String>) -> ApiError {
    ApiError::new(ErrorCode::InvalidRequest, message)
}

/// JSON body whose rejections use the contract error body.
pub struct Body<T>(T);

impl<S: Send + Sync, T: DeserializeOwned> FromRequest<S> for Body<T> {
    type Rejection = ApiError;

    async fn from_request(req: Request, state: &S) -> Result<Self, ApiError> {
        match Json::<T>::from_request(req, state).await {
            Ok(Json(value)) => Ok(Body(value)),
            Err(err) => Err(invalid(err.body_text())),
        }
    }
}

/// Query string whose rejections use the contract error body.
pub struct Q<T>(T);

impl<S: Send + Sync, T: DeserializeOwned> FromRequestParts<S> for Q<T> {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, ApiError> {
        match Query::<T>::from_request_parts(parts, state).await {
            Ok(Query(value)) => Ok(Q(value)),
            Err(err) => Err(invalid(err.body_text())),
        }
    }
}

/// `{id}` path parameter; anything but an integer names nothing.
pub struct Id(i64);

impl<S: Send + Sync> FromRequestParts<S> for Id {
    type Rejection = ApiError;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, ApiError> {
        match Path::<i64>::from_request_parts(parts, state).await {
            Ok(Path(id)) => Ok(Id(id)),
            Err(_) => Err(not_found("resource")),
        }
    }
}

async fn no_route() -> ApiError {
    not_found("endpoint")
}

fn check_name(name: &str) -> Result<(), ApiError> {
    let ok = (1..=48).contains(&name.len())
        && name
            .bytes()
            .enumerate()
            .all(|(i, b)| b.is_ascii_lowercase() || b.is_ascii_digit() || (i > 0 && b == b'-'));
    if ok {
        Ok(())
    } else {
        Err(ApiError::new(
            ErrorCode::InvalidName,
            "name must match ^[a-z0-9][a-z0-9-]{0,47}$",
        ))
    }
}

#[utoipa::path(get, path = "/api/domains", responses((status = 200, body = [Domain])))]
pub async fn list_domains(State(s): State<AppState>) -> Res<Json<Vec<Domain>>> {
    Ok(Json(db::list_domains(&s.db).await?))
}

#[utoipa::path(
    post, path = "/api/domains", request_body = NewDomain,
    responses((status = 201, body = Domain), (status = 400, body = ApiError), (status = 409, body = ApiError))
)]
pub async fn create_domain(
    State(s): State<AppState>,
    Body(body): Body<NewDomain>,
) -> Res<(StatusCode, Json<Domain>)> {
    check_name(&body.name)?;
    let domain = db::create_domain(&s.db, &body.name).await??;
    Ok((StatusCode::CREATED, Json(domain)))
}

#[utoipa::path(
    delete, path = "/api/domains/{id}", params(("id" = i64, Path)),
    responses((status = 204), (status = 404, body = ApiError), (status = 409, body = ApiError))
)]
pub async fn delete_domain(State(s): State<AppState>, Id(id): Id) -> Res<StatusCode> {
    db::delete_domain(&s.db, id).await??;
    Ok(StatusCode::NO_CONTENT)
}

#[utoipa::path(get, path = "/api/collections", responses((status = 200, body = [Collection])))]
pub async fn list_collections(State(s): State<AppState>) -> Res<Json<Vec<Collection>>> {
    Ok(Json(db::list_collections(&s.db).await?))
}

#[utoipa::path(
    post, path = "/api/collections", request_body = NewCollection,
    responses(
        (status = 201, body = Collection), (status = 400, body = ApiError),
        (status = 404, body = ApiError), (status = 409, body = ApiError)
    )
)]
pub async fn create_collection(
    State(s): State<AppState>,
    Body(body): Body<NewCollection>,
) -> Res<(StatusCode, Json<Collection>)> {
    check_name(&body.name)?;
    if body.name == "unsorted" {
        let reserved = "unsorted is the vault folder of jobs without a collection";
        return Err(ApiError::new(ErrorCode::InvalidName, reserved).into());
    }
    let collection = db::create_collection(&s.db, body.domain_id, &body.name).await??;
    Ok((StatusCode::CREATED, Json(collection)))
}

#[utoipa::path(
    delete, path = "/api/collections/{id}", params(("id" = i64, Path)),
    responses((status = 204), (status = 404, body = ApiError), (status = 409, body = ApiError))
)]
pub async fn delete_collection(State(s): State<AppState>, Id(id): Id) -> Res<StatusCode> {
    db::delete_collection(&s.db, id).await??;
    Ok(StatusCode::NO_CONTENT)
}

#[utoipa::path(
    put, path = "/api/collections/{id}/feed", params(("id" = i64, Path)), request_body = SetFeed,
    responses((status = 200, body = Collection), (status = 400, body = ApiError), (status = 404, body = ApiError))
)]
pub async fn set_feed(
    State(s): State<AppState>,
    Id(id): Id,
    Body(body): Body<SetFeed>,
) -> Res<Json<Collection>> {
    let url = match body.url.as_deref() {
        None => None,
        Some(url) => {
            let (_, normal) = parse_feed(url)
                .ok_or_else(|| ApiError::new(ErrorCode::InvalidUrl, "not a supported feed URL"))?;
            Some(normal)
        }
    };
    Ok(Json(db::set_feed(&s.db, id, url.as_deref()).await??))
}

#[utoipa::path(
    get, path = "/api/collections/{id}/feed", params(("id" = i64, Path), FeedQuery),
    responses(
        (status = 200, body = FeedList), (status = 400, body = ApiError), (status = 404, body = ApiError),
        (status = 409, body = ApiError), (status = 502, body = ApiError)
    )
)]
pub async fn list_feed(
    State(s): State<AppState>,
    Extension(refresh): Extension<FeedRefresh>,
    Id(id): Id,
    Q(query): Q<FeedQuery>,
) -> Res<Json<FeedList>> {
    let limit = query.limit.unwrap_or(30);
    if !(1..=100).contains(&limit) {
        return Err(invalid("limit must be 1-100").into());
    }
    let collection = db::get_collection(&s.db, id)
        .await?
        .ok_or_else(|| not_found("collection"))?;
    let feed = collection
        .feed
        .ok_or_else(|| invalid("collection has no feed"))?;
    let Ok(mut last) = refresh.0.try_lock() else {
        return Err(conflict("a feed refresh is already running").into());
    };
    if last.is_some_and(|started| started.elapsed() < FEED_INTERVAL) {
        let wait = FEED_INTERVAL.as_secs_f32();
        return Err(conflict(&format!("feed was just refreshed; try again in {wait}s")).into());
    }
    *last = Some(Instant::now());
    let mut list = run_feed(&s.config, feed.kind, &feed.url, limit).await?;
    // Every job is read; a personal library is small enough for that.
    let jobs = db::list_jobs(&s.db, u32::MAX).await?;
    for item in &mut list.items {
        item.job = jobs
            .iter()
            .find(|j| {
                (j.domain_id, j.collection_id) == (collection.domain_id, Some(collection.id))
                    && (j.platform, &j.source_id) == (item.platform, &item.source_id)
            })
            .map(|j| JobRef {
                id: j.id,
                status: j.status,
            });
    }
    Ok(Json(list))
}

async fn run_feed(
    config: &Config,
    kind: FeedKind,
    url: &str,
    limit: u32,
) -> Result<FeedList, ApiError> {
    let mut cmd = Command::new("python3");
    cmd.arg("-I")
        .arg(config.steps_dir.join("feed.py"))
        .args([
            "list",
            feed_kind_arg(kind),
            url,
            "--limit",
            &limit.to_string(),
        ])
        .env_clear()
        .envs(exec::base_env());
    if let Some(proxy) = &config.proxy {
        cmd.env("FLORI_PROXY", proxy);
    }
    if let Some(sessdata) = &config.bili_sessdata {
        cmd.env("FLORI_BILI_SESSDATA", sessdata);
    }
    let (mut out, mut last_err) = (Vec::new(), None);
    let stop = exec::run_child(cmd, FEED_TIMEOUT, None, |stderr, line| {
        if !stderr {
            out.extend_from_slice(line);
            out.push(b'\n');
        } else if !line.trim_ascii().is_empty() {
            last_err = Some(String::from_utf8_lossy(line).into_owned());
        }
    })
    .await;
    let upstream = |message: String| ApiError::new(ErrorCode::Upstream, message);
    match stop {
        Ok(Stop::Exited(Some(0))) => serde_json::from_slice(&out)
            .map_err(|e| upstream(format!("feed.py printed invalid output: {e}"))),
        Ok(Stop::TimedOut) => Err(upstream("feed listing timed out after 60s".into())),
        Ok(_) => Err(upstream(
            last_err.unwrap_or_else(|| "feed.py failed".into()),
        )),
        Err(err) => Err(upstream(format!("cannot start feed.py: {err}"))),
    }
}

#[utoipa::path(
    post, path = "/api/collections/{id}/feed/jobs", params(("id" = i64, Path)), request_body = FeedSubmit,
    responses((status = 200, body = FeedSubmitResult), (status = 400, body = ApiError), (status = 404, body = ApiError))
)]
pub async fn submit_feed(
    State(s): State<AppState>,
    Id(id): Id,
    Body(body): Body<FeedSubmit>,
) -> Res<Json<FeedSubmitResult>> {
    if !(1..=20).contains(&body.items.len()) {
        return Err(invalid("a batch has 1-20 items").into());
    }
    let sources = body
        .items
        .iter()
        .map(|pick| {
            video_source(pick.platform, &pick.source_id)
                .ok_or_else(|| invalid(format!("invalid source_id {:?}", pick.source_id)))
        })
        .collect::<Result<Vec<_>, _>>()?;
    let collection = db::get_collection(&s.db, id)
        .await?
        .ok_or_else(|| not_found("collection"))?;
    let translate = body.translate.unwrap_or(TranslateMode::Auto);
    let (domain_id, cli) = (collection.domain_id, s.exec.cli_version());
    let results = db::create_jobs(
        &s.db,
        &s.config,
        cli,
        domain_id,
        Some(id),
        translate,
        &sources,
    )
    .await??;
    let mut out = FeedSubmitResult {
        created: Vec::new(),
        skipped: Vec::new(),
    };
    for (source, result) in sources.into_iter().zip(results) {
        let (list, job) = match result {
            Ok(job) => (&mut out.created, job),
            Err(job) => (&mut out.skipped, job),
        };
        list.push(FeedPicked {
            platform: source.platform,
            source_id: source.id,
            job,
        });
    }
    s.exec.wake();
    Ok(Json(out))
}

#[utoipa::path(
    post, path = "/api/jobs", request_body = SubmitJob,
    responses(
        (status = 201, body = JobSummary), (status = 400, body = ApiError),
        (status = 404, body = ApiError), (status = 409, body = ApiError)
    )
)]
pub async fn submit_job(
    State(s): State<AppState>,
    Body(body): Body<SubmitJob>,
) -> Res<(StatusCode, Json<JobSummary>)> {
    let source = parse_source(&body.url)
        .ok_or_else(|| ApiError::new(ErrorCode::InvalidUrl, "not a supported link"))?;
    let translate = body.translate.unwrap_or(TranslateMode::Auto);
    let (domain, collection, cli) = (body.domain_id, body.collection_id, s.exec.cli_version());
    let results = db::create_jobs(
        &s.db,
        &s.config,
        cli,
        domain,
        collection,
        translate,
        &[source],
    )
    .await??;
    match results.into_iter().next() {
        Some(Ok(job)) => {
            s.exec.wake();
            let (summary, _) = db::get_job(&s.db, job.id)
                .await?
                .ok_or_else(|| not_found("job"))?;
            Ok((StatusCode::CREATED, Json(summary)))
        }
        Some(Err(existing)) => Err(ApiError::duplicate_job(existing.id).into()),
        None => Err(Error::Internal("no job created".into())),
    }
}

#[utoipa::path(get, path = "/api/jobs", params(JobsQuery), responses((status = 200, body = [JobSummary])))]
pub async fn list_jobs(
    State(s): State<AppState>,
    Q(query): Q<JobsQuery>,
) -> Res<Json<Vec<JobSummary>>> {
    Ok(Json(
        db::list_jobs(&s.db, query.limit.unwrap_or(200)).await?,
    ))
}

#[utoipa::path(
    get, path = "/api/jobs/{id}", params(("id" = i64, Path)),
    responses((status = 200, body = JobDetail), (status = 404, body = ApiError))
)]
pub async fn get_job(State(s): State<AppState>, Id(id): Id) -> Res<Json<JobDetail>> {
    let (job, vault_path) = db::get_job(&s.db, id)
        .await?
        .ok_or_else(|| not_found("job"))?;
    let mut steps = db::steps(&s.db, id).await?;
    let dir = job_dir(&s.config.data_dir, id);
    let (mut files, mut assets, mut note_file) = (BTreeMap::new(), BTreeMap::new(), None);
    let mut calls = ai_calls(&dir);
    // Same merge as publish: in step order, later steps win.
    for step in &mut steps {
        step.ai_calls = calls.remove(step.name.as_str()).unwrap_or_default();
        if step.status != StepStatus::Succeeded {
            continue;
        }
        let rel = format!("steps/{}", step_dir_name(step.seq, step.name));
        let Ok(manifest) = read_manifest(&dir.join(&rel)) else {
            continue;
        };
        for (name, path) in manifest.files {
            let path = format!("{rel}/{path}");
            step.files.push(JobFile {
                name: name.clone(),
                path: path.clone(),
            });
            files.insert(name, path);
        }
        for asset in manifest.assets {
            let path = format!("{rel}/assets/{asset}");
            assets.insert(asset, path);
        }
        let text = |key| match manifest.meta.get(key) {
            Some(MetaValue::Text(value)) => Some(value.clone()),
            _ => None,
        };
        step.skipped_reason = text("skipped_reason");
        let count = |key| match manifest.meta.get(key) {
            Some(MetaValue::Int(n)) => u32::try_from(*n).unwrap_or(0),
            _ => 0,
        };
        if manifest.meta.get("punctuated") == Some(&MetaValue::Bool(true)) {
            step.punctuation = Some(Punctuation {
                sections: count("sections"),
                kept_original: count("kept_original"),
                corrected: count("corrected"),
                reverted: count("reverted"),
            });
        }
        if step.name == StepName::Publish {
            note_file = text("note_file");
        }
        step.meta = manifest.meta;
    }
    let obsidian_uri = vault_path
        .as_deref()
        .zip(note_file)
        .map(|(vault_path, note)| {
            let folder = vault_path.split_once('/').map_or("", |(_, rest)| rest);
            let file = format!("{}/{note}", folder.trim_end_matches('/'));
            format!(
                "obsidian://open?vault={}&file={}",
                encode(&s.config.vault_name),
                encode(file.trim_start_matches('/'))
            )
        });
    Ok(Json(JobDetail {
        steps,
        files: files
            .into_iter()
            .map(|(name, path)| JobFile { name, path })
            .collect(),
        assets,
        preview_base: format!("/api/jobs/{id}/files/"),
        vault_path,
        obsidian_uri,
        job_url: format!("{}/jobs/{id}", s.config.public_url),
        ai: read_job(&dir).ok().map(|spec| spec.ai),
        job,
    }))
}

/// The suffixes of one AI call's files, in the order the API lists them.
const AI_CALL_FILES: [&str; 5] = ["prompt.md", "call.json", "out.json", "out.txt", "err.txt"];

/// The call records in the job's `ai/`, by step name, oldest first. One directory listing; files
/// still being written (`.tmp`) and names that are no record are left out.
fn ai_calls(job_dir: &std::path::Path) -> BTreeMap<String, Vec<AiCall>> {
    let mut found: BTreeMap<(String, u32, u32), AiCall> = BTreeMap::new();
    for entry in std::fs::read_dir(job_dir.join("ai"))
        .into_iter()
        .flatten()
        .flatten()
    {
        let file = entry.file_name().to_string_lossy().into_owned();
        let Some((stem, kind)) = file.split_once('.') else {
            continue;
        };
        let parsed = stem.rsplit_once('-').and_then(|(head, index)| {
            let (step, attempt) = head.rsplit_once("-a")?;
            Some((step, attempt.parse().ok()?, index.parse().ok()?))
        });
        let Some((step, attempt, index)) = parsed.filter(|_| AI_CALL_FILES.contains(&kind)) else {
            continue;
        };
        let call = found
            .entry((step.to_owned(), attempt, index))
            .or_insert_with(|| AiCall {
                name: stem.to_owned(),
                attempt,
                index,
                files: Vec::new(),
            });
        call.files.push(format!("ai/{file}"));
    }
    let mut by_step: BTreeMap<String, Vec<AiCall>> = BTreeMap::new();
    for ((step, _, _), mut call) in found {
        let rank = |path: &String| AI_CALL_FILES.iter().position(|kind| path.ends_with(kind));
        call.files.sort_by_key(rank);
        by_step.entry(step).or_default().push(call);
    }
    by_step
}

/// Percent-encodes everything outside RFC 3986 unreserved characters.
fn encode(text: &str) -> String {
    text.bytes()
        .map(|b| match b {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                char::from(b).to_string()
            }
            _ => format!("%{b:02X}"),
        })
        .collect()
}

#[utoipa::path(
    get, path = "/api/jobs/{id}/logs", params(("id" = i64, Path), LogQuery),
    responses((status = 200, body = LogChunk), (status = 400, body = ApiError), (status = 404, body = ApiError))
)]
pub async fn get_logs(
    State(s): State<AppState>,
    Id(id): Id,
    Q(query): Q<LogQuery>,
) -> Res<Json<LogChunk>> {
    db::get_job(&s.db, id)
        .await?
        .ok_or_else(|| not_found("job"))?;
    let offset = query.offset.unwrap_or(0);
    let file = match tokio::fs::File::open(job_dir(&s.config.data_dir, id).join("job.log")).await {
        Ok(file) => Some(file),
        Err(err) if err.kind() == io::ErrorKind::NotFound => None,
        Err(err) => return Err(err.into()),
    };
    let len = match &file {
        Some(file) => file.metadata().await?.len(),
        None => 0,
    };
    if offset > len {
        return Err(invalid("offset is past the end of job.log").into());
    }
    let mut buf = Vec::new();
    if let Some(mut file) = file {
        file.seek(SeekFrom::Start(offset)).await?;
        file.take(LOG_CHUNK).read_to_end(&mut buf).await?;
    }
    let end = match buf.iter().rposition(|&b| b == b'\n') {
        Some(i) => i + 1,
        // A line longer than a chunk goes out in pieces, cut between characters.
        None if buf.len() as u64 == LOG_CHUNK => char_boundary(&buf),
        None => 0,
    };
    buf.truncate(end);
    Ok(Json(LogChunk {
        text: String::from_utf8_lossy(&buf).into_owned(),
        next_offset: offset + end as u64,
    }))
}

/// Length of `buf` without a trailing incomplete UTF-8 sequence.
fn char_boundary(buf: &[u8]) -> usize {
    let Some(start) = buf.iter().rposition(|b| b & 0xC0 != 0x80) else {
        return buf.len();
    };
    let width = match buf[start] {
        0xF0.. => 4,
        0xE0.. => 3,
        0xC0.. => 2,
        _ => 1,
    };
    if start + width > buf.len() {
        start
    } else {
        buf.len()
    }
}

#[utoipa::path(
    post, path = "/api/jobs/{id}/retry", params(("id" = i64, Path)),
    responses((status = 200, body = JobSummary), (status = 404, body = ApiError), (status = 409, body = ApiError))
)]
pub async fn retry_job(State(s): State<AppState>, Id(id): Id) -> Res<Json<JobSummary>> {
    let job = db::retry_job(&s.db, id).await??;
    s.exec.wake();
    Ok(Json(job))
}

#[utoipa::path(
    post, path = "/api/jobs/{id}/cancel", params(("id" = i64, Path)),
    responses((status = 200, body = JobSummary), (status = 404, body = ApiError), (status = 409, body = ApiError))
)]
pub async fn cancel_job(State(s): State<AppState>, Id(id): Id) -> Res<Json<JobSummary>> {
    let job = db::cancel_job(&s.db, id).await??;
    s.exec.cancel(id);
    Ok(Json(job))
}

/// Raw bytes of a file inside the job directory. Supports `Range` for video playback.
#[utoipa::path(
    get, path = "/api/jobs/{id}/files/{path}",
    params(("id" = i64, Path), ("path" = String, Path, description = "Job-relative path")),
    responses((status = 200, description = "File bytes"), (status = 206, description = "Partial content"), (status = 404, body = ApiError))
)]
pub async fn get_file(
    State(s): State<AppState>,
    params: Result<Path<(i64, String)>, PathRejection>,
    req: Request,
) -> Res<Response> {
    let missing = || not_found("file");
    let Ok(Path((id, path))) = params else {
        return Err(missing().into());
    };
    // Symlinks and `..` resolve first, so the prefix check sees the real location.
    let root = tokio::fs::canonicalize(job_dir(&s.config.data_dir, id))
        .await
        .map_err(|_| missing())?;
    let file = tokio::fs::canonicalize(root.join(&path))
        .await
        .map_err(|_| missing())?;
    if !file.starts_with(&root) || !file.is_file() {
        return Err(missing().into());
    }
    Ok(ServeFile::new(file).try_call(req).await?.into_response())
}

#[utoipa::path(get, path = "/api/health", responses((status = 200, body = Health)))]
pub async fn health() -> Json<Health> {
    Json(Health {
        version: env!("CARGO_PKG_VERSION").to_owned(),
    })
}

#[cfg(test)]
mod tests;
