use std::{
    ffi::OsString,
    fmt,
    path::{Component, PathBuf},
};

#[cfg(any(feature = "codex", feature = "qoder"))]
use flori_core::AiTool;
use flori_runner::RunnerClient;
#[cfg(any(feature = "codex", feature = "qoder"))]
use reqwest::Url;

const SERVER_URL: &str = "FLORI_SERVER_URL";
const TOKEN: &str = "FLORI_RUNNER_TOKEN";
#[cfg(any(feature = "codex", feature = "qoder"))]
const MODEL: &str = "FLORI_RUNNER_MODEL";
#[cfg(any(feature = "codex", feature = "qoder"))]
const EFFORT: &str = "FLORI_RUNNER_EFFORT";
const SPOOL_DIR: &str = "FLORI_RUNNER_SPOOL_DIR";
#[cfg(any(feature = "codex", feature = "qoder"))]
const HOME_DIR: &str = "HOME";
#[cfg(feature = "qoder")]
const QODER_CONFIG_DIR: &str = "QODER_CONFIG_DIR";
#[cfg(feature = "codex")]
const CODEX_CONFIG_DIR: &str = "CODEX_HOME";
#[cfg(feature = "qoder")]
const QODER_PROXY_URL: &str = "FLORI_QODER_PROXY_URL";
#[cfg(feature = "codex")]
const CODEX_PROXY_URL: &str = "FLORI_CODEX_PROXY_URL";

#[cfg(any(feature = "codex", feature = "qoder"))]
pub(crate) struct RuntimeConfig {
    pub(crate) tool: AiTool,
    pub(crate) client: RunnerClient,
    pub(crate) model: String,
    pub(crate) effort: String,
    pub(crate) spool_dir: PathBuf,
    pub(crate) home_dir: PathBuf,
    pub(crate) tool_config_dir: PathBuf,
    pub(crate) proxy_url: Option<Url>,
}

#[cfg(feature = "media")]
pub(crate) struct MediaRuntimeConfig {
    pub(crate) client: RunnerClient,
    pub(crate) spool_dir: PathBuf,
}

#[derive(Debug, Eq, PartialEq)]
pub(crate) enum RuntimeConfigError {
    InvalidArguments,
    MissingEnvironment(&'static str),
    InvalidEnvironment(&'static str),
}

impl fmt::Display for RuntimeConfigError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidArguments => formatter.write_str("expected: run qoder|codex"),
            Self::MissingEnvironment(name) => write!(formatter, "missing environment: {name}"),
            Self::InvalidEnvironment(name) => write!(formatter, "invalid environment: {name}"),
        }
    }
}

impl std::error::Error for RuntimeConfigError {}

#[cfg(any(feature = "codex", feature = "qoder"))]
pub(crate) fn parse(
    args: &[OsString],
    mut environment: impl FnMut(&str) -> Option<OsString>,
) -> Result<RuntimeConfig, RuntimeConfigError> {
    let tool = match args {
        [run, tool] if run == "run" && tool == "qoder" => AiTool::QoderCli,
        [run, tool] if run == "run" && tool == "codex" => AiTool::CodexCli,
        _ => return Err(RuntimeConfigError::InvalidArguments),
    };
    let server_url = required_text(&mut environment, SERVER_URL)?;
    let token = required_text(&mut environment, TOKEN)?;
    let model = required_identifier(&mut environment, MODEL)?;
    let effort = required_identifier(&mut environment, EFFORT)?;
    let spool_dir = required_path(&mut environment, SPOOL_DIR)?;
    let home_dir = required_path(&mut environment, HOME_DIR)?;
    let config_name = match tool {
        #[cfg(feature = "qoder")]
        AiTool::QoderCli => QODER_CONFIG_DIR,
        #[cfg(feature = "codex")]
        AiTool::CodexCli => CODEX_CONFIG_DIR,
        #[allow(unreachable_patterns)]
        _ => return Err(RuntimeConfigError::InvalidArguments),
    };
    let tool_config_dir = required_path(&mut environment, config_name)?;
    let proxy_url = match tool {
        #[cfg(feature = "qoder")]
        AiTool::QoderCli => Some(required_proxy_url(&mut environment, QODER_PROXY_URL)?),
        #[cfg(feature = "codex")]
        AiTool::CodexCli => Some(required_proxy_url(&mut environment, CODEX_PROXY_URL)?),
        #[allow(unreachable_patterns)]
        _ => return Err(RuntimeConfigError::InvalidArguments),
    };

    let client = RunnerClient::new(&server_url, token)
        .map_err(|_| RuntimeConfigError::InvalidEnvironment(SERVER_URL))?;
    let config = RuntimeConfig {
        tool,
        client,
        model,
        effort,
        spool_dir,
        home_dir,
        tool_config_dir,
        proxy_url,
    };
    debug_assert!(config.proxy_url.is_some());
    Ok(config)
}

#[cfg(feature = "media")]
pub(crate) fn parse_media(
    args: &[OsString],
    mut environment: impl FnMut(&str) -> Option<OsString>,
) -> Result<MediaRuntimeConfig, RuntimeConfigError> {
    if args != ["run", "media"] {
        return Err(RuntimeConfigError::InvalidArguments);
    }
    let server_url = required_text(&mut environment, SERVER_URL)?;
    let token = required_text(&mut environment, TOKEN)?;
    let spool_dir = required_path(&mut environment, SPOOL_DIR)?;
    let client = RunnerClient::new(&server_url, token)
        .map_err(|_| RuntimeConfigError::InvalidEnvironment(SERVER_URL))?;
    Ok(MediaRuntimeConfig { client, spool_dir })
}

fn required_text(
    environment: &mut impl FnMut(&str) -> Option<OsString>,
    name: &'static str,
) -> Result<String, RuntimeConfigError> {
    let value = environment(name).ok_or(RuntimeConfigError::MissingEnvironment(name))?;
    let value = value
        .into_string()
        .map_err(|_| RuntimeConfigError::InvalidEnvironment(name))?;
    (!value.trim().is_empty())
        .then_some(value)
        .ok_or(RuntimeConfigError::InvalidEnvironment(name))
}

#[cfg(any(feature = "codex", feature = "qoder"))]
fn required_identifier(
    environment: &mut impl FnMut(&str) -> Option<OsString>,
    name: &'static str,
) -> Result<String, RuntimeConfigError> {
    let value = required_text(environment, name)?;
    (value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte)))
    .then_some(value)
    .ok_or(RuntimeConfigError::InvalidEnvironment(name))
}

fn required_path(
    environment: &mut impl FnMut(&str) -> Option<OsString>,
    name: &'static str,
) -> Result<PathBuf, RuntimeConfigError> {
    let value = environment(name).ok_or(RuntimeConfigError::MissingEnvironment(name))?;
    let path = PathBuf::from(value);
    (path.is_absolute()
        && path.parent().is_some()
        && !path
            .components()
            .any(|part| matches!(part, Component::CurDir | Component::ParentDir)))
    .then_some(path)
    .ok_or(RuntimeConfigError::InvalidEnvironment(name))
}

#[cfg(any(feature = "codex", feature = "qoder"))]
fn required_proxy_url(
    environment: &mut impl FnMut(&str) -> Option<OsString>,
    name: &'static str,
) -> Result<Url, RuntimeConfigError> {
    let value = required_text(environment, name)?;
    if value
        .strip_prefix("http://")
        .is_none_or(|authority| authority.is_empty() || authority.starts_with('/'))
        || value.ends_with('/')
    {
        return Err(RuntimeConfigError::InvalidEnvironment(name));
    }
    let url = Url::parse(&value).map_err(|_| RuntimeConfigError::InvalidEnvironment(name))?;
    (url.scheme() == "http"
        && url.host().is_some()
        && url.username().is_empty()
        && url.password().is_none()
        && url.path() == "/"
        && url.query().is_none()
        && url.fragment().is_none())
    .then_some(url)
    .ok_or(RuntimeConfigError::InvalidEnvironment(name))
}
