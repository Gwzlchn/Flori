use std::{ffi::OsString, path::Path};

use flori_core::{AiResultEnvelope, Executor, UsageOrigin, UsageUpdate};
use serde::Deserialize;

#[derive(Debug, Eq, PartialEq)]
pub struct CodexCommand {
    pub program: &'static str,
    pub args: Vec<OsString>,
    pub stdin: String,
}

#[derive(Debug, PartialEq)]
pub struct CodexParsedOutput {
    pub result: AiResultEnvelope,
    pub usage: UsageUpdate,
    pub websearch: Vec<CodexWebSearchObservation>,
}

#[derive(Debug, Eq, PartialEq)]
pub struct CodexWebSearchObservation {
    pub query: String,
    pub url: Option<String>,
}

#[derive(Debug, Eq, PartialEq)]
pub enum CodexAdapterError {
    UnsupportedExecutor,
    InvalidConfiguration(&'static str),
    ProcessFailed(Option<i32>),
    InvalidEvent { line: usize },
    InvalidOutput(&'static str),
}

#[allow(clippy::too_many_arguments)]
pub fn build_codex_command(
    executor: Executor,
    model: &str,
    effort: &str,
    workspace: &Path,
    output_schema: &Path,
    output_result: &Path,
    prompt: &str,
) -> Result<CodexCommand, CodexAdapterError> {
    if !valid_identifier(model) || !valid_identifier(effort) {
        return Err(CodexAdapterError::InvalidConfiguration("model/effort"));
    }
    if prompt.is_empty() {
        return Err(CodexAdapterError::InvalidConfiguration("prompt"));
    }

    let websearch_enabled = match executor {
        Executor::AiDocumentTranslate => false,
        Executor::AiDocumentNote | Executor::AiVideoNote => true,
        Executor::DocumentAcquire
        | Executor::DocumentExtract
        | Executor::VideoAcquire
        | Executor::VideoSubscription
        | Executor::VideoTranscribe
        | Executor::VideoFrames
        | Executor::VideoMechanicalNote
        | Executor::CoreValidate
        | Executor::CorePublish => return Err(CodexAdapterError::UnsupportedExecutor),
    };

    let mut args = words("--ask-for-approval never");
    if websearch_enabled {
        args.push("--search".into());
    }
    args.extend(words(
        "exec --strict-config --ignore-user-config --ignore-rules \
         --skip-git-repo-check --sandbox read-only --ephemeral --color never --json --model",
    ));
    args.extend([
        model.into(),
        "-c".into(),
        format!("model_reasoning_effort=\"{effort}\"").into(),
        "-c".into(),
        "history.persistence=\"none\"".into(),
    ]);
    if !websearch_enabled {
        args.extend(["-c".into(), "web_search=\"disabled\"".into()]);
    }
    args.push("-C".into());
    args.push(workspace.into());
    args.push("--output-schema".into());
    args.push(output_schema.into());
    args.push("--output-last-message".into());
    args.push(output_result.into());
    args.push("-".into());

    Ok(CodexCommand {
        program: "codex",
        args,
        stdin: prompt.to_owned(),
    })
}

pub fn parse_codex_output(
    executor: Executor,
    invocation_key: &str,
    exit_code: Option<i32>,
    stdout_jsonl: &str,
    result_json: &str,
) -> Result<CodexParsedOutput, CodexAdapterError> {
    if exit_code != Some(0) {
        return Err(CodexAdapterError::ProcessFailed(exit_code));
    }

    let mut state = 0;
    let mut agent_message = None;
    let mut usage = None;
    let mut websearch = Vec::new();

    for (index, line) in stdout_jsonl.lines().enumerate() {
        let line_number = index + 1;
        if line.trim().is_empty() {
            return Err(CodexAdapterError::InvalidEvent { line: line_number });
        }
        let event: ThreadEvent = serde_json::from_str(line)
            .map_err(|_| CodexAdapterError::InvalidEvent { line: line_number })?;
        match event {
            ThreadEvent::ThreadStarted { thread_id } => {
                if state != 0 || thread_id.is_empty() {
                    return Err(CodexAdapterError::InvalidOutput("event sequence"));
                }
                state = 1;
            }
            ThreadEvent::TurnStarted => {
                if state != 1 {
                    return Err(CodexAdapterError::InvalidOutput("event sequence"));
                }
                state = 2;
            }
            ThreadEvent::ItemStarted { item } | ThreadEvent::ItemUpdated { item } => {
                if state != 2 {
                    return Err(CodexAdapterError::InvalidOutput("event sequence"));
                }
                validate_item(&item, executor)?;
            }
            ThreadEvent::ItemCompleted { item } => {
                if state != 2 {
                    return Err(CodexAdapterError::InvalidOutput("event sequence"));
                }
                validate_item(&item, executor)?;
                match item {
                    ThreadItem::AgentMessage { text, .. }
                        if agent_message.replace(text.clone()).is_some() =>
                    {
                        return Err(CodexAdapterError::InvalidOutput("duplicate agent message"));
                    }
                    ThreadItem::AgentMessage { .. } => {}
                    ThreadItem::WebSearch { query, action, .. } => {
                        websearch.push(CodexWebSearchObservation {
                            query,
                            url: action.url(),
                        })
                    }
                    ThreadItem::Error { .. } => {
                        return Err(CodexAdapterError::InvalidOutput("CLI reported failure"));
                    }
                    _ => {}
                }
            }
            ThreadEvent::TurnCompleted { usage: completed } => {
                if state != 2 {
                    return Err(CodexAdapterError::InvalidOutput("event sequence"));
                }
                if completed.cached_input_tokens > completed.input_tokens
                    || completed.reasoning_output_tokens > completed.output_tokens
                {
                    return Err(CodexAdapterError::InvalidOutput("invalid usage"));
                }
                usage = Some(completed);
                state = 3;
            }
            ThreadEvent::TurnFailed { error: _ } | ThreadEvent::Error { message: _ } => {
                return Err(CodexAdapterError::InvalidOutput("CLI reported failure"));
            }
        }
    }

    if state != 3 {
        return Err(CodexAdapterError::InvalidOutput("event sequence"));
    }
    let agent_result = parse_result(
        &agent_message.ok_or(CodexAdapterError::InvalidOutput("missing agent message"))?,
    )?;
    let file_result = parse_result(result_json)?;
    if agent_result != file_result {
        return Err(CodexAdapterError::InvalidOutput("result mismatch"));
    }
    if result_executor(&file_result) != executor {
        return Err(CodexAdapterError::InvalidOutput("result executor mismatch"));
    }
    let usage = usage.ok_or(CodexAdapterError::InvalidOutput("event sequence"))?;

    Ok(CodexParsedOutput {
        result: file_result,
        websearch,
        usage: UsageUpdate::Final {
            invocation_key: invocation_key.to_owned(),
            origin: UsageOrigin::Observed,
            input_tokens: Some(usage.input_tokens),
            output_tokens: Some(usage.output_tokens),
            cost_micros: None,
            credits_micros: None,
        },
    })
}

fn validate_item(item: &ThreadItem, executor: Executor) -> Result<(), CodexAdapterError> {
    if matches!(item, ThreadItem::WebSearch { .. }) && executor == Executor::AiDocumentTranslate {
        return Err(CodexAdapterError::InvalidOutput("event sequence"));
    }
    Ok(())
}

fn parse_result(value: &str) -> Result<AiResultEnvelope, CodexAdapterError> {
    serde_json::from_str(value).map_err(|_| CodexAdapterError::InvalidOutput("invalid result"))
}

fn valid_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || b"._-".contains(&byte))
}

fn words(value: &str) -> Vec<OsString> {
    value.split_ascii_whitespace().map(Into::into).collect()
}

const fn result_executor(result: &AiResultEnvelope) -> Executor {
    match result {
        AiResultEnvelope::DocumentTranslate { .. } => Executor::AiDocumentTranslate,
        AiResultEnvelope::DocumentNote { .. } => Executor::AiDocumentNote,
        AiResultEnvelope::VideoNote { .. } => Executor::AiVideoNote,
    }
}

#[derive(Deserialize)]
#[allow(dead_code)]
#[serde(tag = "type", deny_unknown_fields)]
enum ThreadEvent {
    #[serde(rename = "thread.started")]
    ThreadStarted { thread_id: String },
    #[serde(rename = "turn.started")]
    TurnStarted,
    #[serde(rename = "turn.completed")]
    TurnCompleted { usage: CodexUsage },
    #[serde(rename = "turn.failed")]
    TurnFailed { error: CodexErrorMessage },
    #[serde(rename = "item.started")]
    ItemStarted { item: ThreadItem },
    #[serde(rename = "item.updated")]
    ItemUpdated { item: ThreadItem },
    #[serde(rename = "item.completed")]
    ItemCompleted { item: ThreadItem },
    #[serde(rename = "error")]
    Error { message: String },
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CodexUsage {
    input_tokens: u64,
    cached_input_tokens: u64,
    #[serde(rename = "cache_write_input_tokens")]
    _cache_write_input_tokens: u64,
    output_tokens: u64,
    reasoning_output_tokens: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CodexErrorMessage {
    #[serde(rename = "message")]
    _message: String,
}

#[derive(Deserialize)]
#[allow(dead_code)]
#[rustfmt::skip]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum ThreadItem {
    AgentMessage { id: String, text: String },
    Reasoning { id: String, text: String },
    CommandExecution { id: String, command: String, aggregated_output: String, exit_code: Option<i32>, status: CommandExecutionStatus },
    WebSearch { id: String, query: String, action: WebSearchAction },
    Error { id: String, message: String },
}

#[derive(Deserialize)]
#[rustfmt::skip]
#[serde(rename_all = "snake_case")]
enum CommandExecutionStatus { InProgress, Completed, Failed, Declined }

#[derive(Deserialize)]
#[allow(dead_code)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum WebSearchAction {
    Search {
        query: Option<String>,
        queries: Option<Vec<String>>,
    },
    OpenPage {
        url: Option<String>,
    },
    FindInPage {
        url: Option<String>,
        pattern: Option<String>,
    },
    Other,
}

impl WebSearchAction {
    fn url(self) -> Option<String> {
        match self {
            Self::OpenPage { url } | Self::FindInPage { url, .. } => url,
            Self::Search { .. } | Self::Other => None,
        }
    }
}
