//! Flori: one process serves the web UI and runs two fixed pipelines.
//! `flori serve` starts the server and executor. `flori step <name> <job_dir> <out_dir>` runs one
//! built-in step as a child process of the executor (contract 1 in docs/contracts.md).

mod ai;
mod api;
mod db;
mod exec;
mod http;
mod pipeline;
mod publish;
mod system;
mod translate;
mod video;

use std::{env, path::Path, path::PathBuf, process::ExitCode, sync::Arc};

use pipeline::{AiTool, StepFailure, StepName};

/// Runtime configuration. Environment variables are the only source; see `.env.example`.
#[derive(Debug, Clone)]
pub struct Config {
    pub data_dir: PathBuf,
    pub listen: String,
    pub web_user: String,
    pub web_password: String,
    /// Built frontend (`index.html` and assets).
    pub static_dir: PathBuf,
    /// Python steps and `feed.py`.
    pub steps_dir: PathBuf,
    /// Binary started for built-in steps. Tests point it at a fake.
    pub self_exe: PathBuf,
    /// The AI CLIs, and YouTube while `video_proxy` is unset.
    pub proxy: Option<String>,
    /// YouTube only; see [`Config::youtube_proxy`].
    pub video_proxy: Option<String>,
    pub public_url: String,
    pub vault_name: String,
    pub ai_tool: AiTool,
    pub ai_model_note: String,
    pub ai_model_translate: String,
    pub bili_sessdata: Option<String>,
    pub whisper_model: PathBuf,
    pub claude_oauth_token: Option<String>,
    pub codex_home: Option<PathBuf>,
}

impl Config {
    fn from_env() -> Result<Config, String> {
        let data_dir = PathBuf::from(var_or("FLORI_DATA_DIR", "/data"));
        let ai_tool = match var_or("FLORI_AI_TOOL", "claude").as_str() {
            "claude" => AiTool::Claude,
            "codex" => AiTool::Codex,
            other => {
                return Err(format!(
                    "FLORI_AI_TOOL must be claude or codex, got {other:?}"
                ));
            }
        };
        Ok(Config {
            listen: var_or("FLORI_LISTEN", "0.0.0.0:8780"),
            web_user: var_or("FLORI_WEB_USER", "flori"),
            web_password: required("FLORI_WEB_PASSWORD")?,
            static_dir: PathBuf::from(var_or("FLORI_STATIC_DIR", "/app/web")),
            steps_dir: PathBuf::from(var_or("FLORI_STEPS_DIR", "/app/steps")),
            self_exe: env::current_exe().map_err(|e| format!("current_exe: {e}"))?,
            proxy: optional("FLORI_PROXY"),
            video_proxy: optional("FLORI_VIDEO_PROXY"),
            public_url: required("FLORI_PUBLIC_URL")?
                .trim_end_matches('/')
                .to_owned(),
            vault_name: var_or("FLORI_VAULT_NAME", "flori"),
            ai_tool,
            // Always explicit: without --model the CLI inherits the host user's default.
            ai_model_note: required("FLORI_AI_MODEL_NOTE")?,
            ai_model_translate: required("FLORI_AI_MODEL_TRANSLATE")?,
            bili_sessdata: optional("FLORI_BILI_SESSDATA"),
            whisper_model: optional("FLORI_WHISPER_MODEL")
                .map(PathBuf::from)
                .unwrap_or_else(|| data_dir.join("models/whisper-base")),
            claude_oauth_token: optional("CLAUDE_CODE_OAUTH_TOKEN"),
            codex_home: optional("CODEX_HOME").map(PathBuf::from),
            data_dir,
        })
    }

    /// The proxy of every yt-dlp request to YouTube, the download and the feed listing alike:
    /// FLORI_VIDEO_PROXY, or FLORI_PROXY when that is unset. Bilibili never takes a proxy.
    pub fn youtube_proxy(&self) -> Option<&str> {
        self.video_proxy.as_deref().or(self.proxy.as_deref())
    }
}

fn optional(name: &str) -> Option<String> {
    env::var(name).ok().filter(|v| !v.is_empty())
}

fn var_or(name: &str, default: &str) -> String {
    optional(name).unwrap_or_else(|| default.to_owned())
}

fn required(name: &str) -> Result<String, String> {
    optional(name).ok_or_else(|| format!("{name} is required"))
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().skip(1).collect();
    match args
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        .as_slice()
    {
        ["serve"] => serve(),
        ["step", name, job_dir, out_dir] => step(name, Path::new(job_dir), Path::new(out_dir)),
        _ => {
            eprintln!("usage: flori serve | flori step <name> <job_dir> <out_dir>");
            ExitCode::from(2)
        }
    }
}

fn serve() -> ExitCode {
    let config = match Config::from_env() {
        Ok(config) => Arc::new(config),
        Err(err) => {
            eprintln!("flori: {err}");
            return ExitCode::FAILURE;
        }
    };
    let runtime = match tokio::runtime::Runtime::new() {
        Ok(runtime) => runtime,
        Err(err) => {
            eprintln!("flori: tokio runtime: {err}");
            return ExitCode::FAILURE;
        }
    };
    match runtime.block_on(run_server(config)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("flori: {err}");
            ExitCode::FAILURE
        }
    }
}

async fn run_server(config: Arc<Config>) -> Result<(), String> {
    let db = db::open(&config.data_dir.join("flori.sqlite"))
        .await
        .map_err(|e| format!("open database: {e}"))?;
    let exec = exec::start(db.clone(), config.clone()).await?;
    let listener = tokio::net::TcpListener::bind(&config.listen)
        .await
        .map_err(|e| format!("bind {}: {e}", config.listen))?;
    eprintln!(
        "flori {} listening on {}",
        env!("CARGO_PKG_VERSION"),
        config.listen
    );
    let app = http::router(http::AppState { db, exec, config });
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await
        .map_err(|e| format!("serve: {e}"))
}

async fn shutdown_signal() {
    use tokio::signal::unix::{SignalKind, signal};
    match signal(SignalKind::terminate()) {
        Ok(mut term) => {
            tokio::select! {
                _ = term.recv() => {}
                _ = tokio::signal::ctrl_c() => {}
            }
        }
        Err(_) => {
            let _ = tokio::signal::ctrl_c().await;
        }
    }
}

fn step(name: &str, job_dir: &Path, out_dir: &Path) -> ExitCode {
    let result = match name.parse::<StepName>() {
        Ok(name) => run_step(name, job_dir, out_dir),
        Err(err) => Err(StepFailure::permanent(err)),
    };
    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(failure) => {
            eprintln!("{}", failure.message);
            ExitCode::from(failure.exit as u8)
        }
    }
}

fn run_step(name: StepName, job_dir: &Path, out_dir: &Path) -> pipeline::StepResult {
    let runtime = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|e| StepFailure::retry(format!("tokio runtime: {e}")))?;
    runtime.block_on(async {
        match name {
            StepName::VideoFetch => video::fetch(job_dir, out_dir).await,
            StepName::VideoTranscribe => video::transcribe(job_dir, out_dir).await,
            StepName::VideoFrames => video::frames(job_dir, out_dir).await,
            StepName::Note => ai::note(job_dir, out_dir).await,
            StepName::Translate => translate::run(job_dir, out_dir).await,
            StepName::Publish => publish::run(job_dir, out_dir).await,
            StepName::ArxivFetch | StepName::ArxivConvert => Err(StepFailure::permanent(format!(
                "{name} runs as steps/arxiv.py, not as a built-in step"
            ))),
        }
    })
}
