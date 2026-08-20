use std::{
    ffi::OsString,
    fmt,
    path::{Component, PathBuf},
};

use flori_core::AiTool;
use flori_runner::RunnerClient;

const SERVER_URL: &str = "FLORI_SERVER_URL";
const TOKEN: &str = "FLORI_RUNNER_TOKEN";
const MODEL: &str = "FLORI_RUNNER_MODEL";
const EFFORT: &str = "FLORI_RUNNER_EFFORT";
const SPOOL_DIR: &str = "FLORI_RUNNER_SPOOL_DIR";
const HOME_DIR: &str = "HOME";
const QODER_CONFIG_DIR: &str = "QODER_CONFIG_DIR";
const CODEX_CONFIG_DIR: &str = "CODEX_HOME";

pub(crate) struct RuntimeConfig {
    pub(crate) tool: AiTool,
    pub(crate) client: RunnerClient,
    pub(crate) model: String,
    pub(crate) effort: String,
    pub(crate) spool_dir: PathBuf,
    pub(crate) home_dir: PathBuf,
    pub(crate) tool_config_dir: PathBuf,
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
        AiTool::QoderCli => QODER_CONFIG_DIR,
        AiTool::CodexCli => CODEX_CONFIG_DIR,
    };
    let tool_config_dir = required_path(&mut environment, config_name)?;

    let client = RunnerClient::new(&server_url, token)
        .map_err(|_| RuntimeConfigError::InvalidEnvironment(SERVER_URL))?;
    Ok(RuntimeConfig {
        tool,
        client,
        model,
        effort,
        spool_dir,
        home_dir,
        tool_config_dir,
    })
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
