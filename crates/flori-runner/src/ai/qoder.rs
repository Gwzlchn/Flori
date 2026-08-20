use std::{collections::HashSet, str};

use flori_core::{AiModelCapability, AiResultEnvelope, Executor, UsageOrigin, UsageUpdate};
use serde::Deserialize;
use serde_json::value::RawValue;

pub const QODERCLI_VERSION: &str = "1.1.26";
pub const QODERCLI_PROGRAM: &str = "qodercli";
const MAX_PROBE_BYTES: usize = 64 * 1024;

pub struct QoderCommand {
    arguments: Vec<String>,
    standard_input: Option<String>,
}

impl QoderCommand {
    #[must_use]
    pub fn arguments(&self) -> &[String] {
        &self.arguments
    }

    #[must_use]
    pub fn standard_input(&self) -> Option<&str> {
        self.standard_input.as_deref()
    }

    #[must_use]
    pub fn redacted_arguments(&self) -> Vec<String> {
        self.arguments.clone()
    }
}

#[derive(Debug, Eq, PartialEq)]
pub enum QoderError {
    InvalidCommand,
    InvalidVersion,
    InvalidModelList,
    ModelUnavailable,
    NonZeroExit(Option<i32>),
    OutputTooLarge,
    InvalidOutput,
    InvalidCredits,
}

pub struct QoderResult {
    pub envelope: AiResultEnvelope,
    pub usage: UsageUpdate,
}

#[must_use]
pub fn version_command() -> QoderCommand {
    QoderCommand {
        arguments: vec!["--version".to_owned()],
        standard_input: None,
    }
}

#[must_use]
pub fn model_list_command() -> QoderCommand {
    QoderCommand {
        arguments: vec!["--list-models".to_owned()],
        standard_input: None,
    }
}

pub fn invocation_command(
    executor: Executor,
    model: &str,
    effort: &str,
    cwd: &str,
    prompt: String,
) -> Result<QoderCommand, QoderError> {
    if !is_identifier(model) || !is_identifier(effort) || cwd.is_empty() || prompt.is_empty() {
        return Err(QoderError::InvalidCommand);
    }
    let tools = match executor {
        Executor::AiDocumentTranslate => "",
        Executor::AiDocumentNote | Executor::AiVideoNote => "WebSearch",
        _ => return Err(QoderError::InvalidCommand),
    };
    let arguments = [
        "--print",
        "--output-format",
        "json",
        "--input-format",
        "text",
        "--no-session-persistence",
        "--permission-mode",
        "dont_ask",
        "--max-model-request-retries",
        "0",
        "--strict-mcp-config",
        "--model",
        model,
        "--reasoning-effort",
        effort,
        "--cwd",
        cwd,
        "--tools",
        tools,
    ]
    .into_iter()
    .map(str::to_owned)
    .collect();
    Ok(QoderCommand {
        arguments,
        standard_input: Some(prompt),
    })
}

pub fn verify_version(stdout: &[u8]) -> Result<(), QoderError> {
    if stdout.len() > MAX_PROBE_BYTES {
        return Err(QoderError::OutputTooLarge);
    }
    let version = str::from_utf8(stdout).map_err(|_| QoderError::InvalidVersion)?;
    let version = version
        .strip_suffix("\r\n")
        .or_else(|| version.strip_suffix('\n'))
        .unwrap_or(version);
    (version == QODERCLI_VERSION)
        .then_some(())
        .ok_or(QoderError::InvalidVersion)
}

pub fn verify_model_allowlist(
    stdout: &[u8],
    allowlist: &[AiModelCapability],
) -> Result<(), QoderError> {
    if stdout.len() > MAX_PROBE_BYTES || allowlist.is_empty() {
        return Err(QoderError::InvalidModelList);
    }
    let output = str::from_utf8(stdout).map_err(|_| QoderError::InvalidModelList)?;
    let mut lines = output.lines();
    if lines.next() != Some("MODEL") {
        return Err(QoderError::InvalidModelList);
    }
    let mut available = HashSet::new();
    for line in lines {
        let line = line.strip_suffix('\r').unwrap_or(line);
        if line.is_empty() || !available.insert(line) {
            return Err(QoderError::InvalidModelList);
        }
    }
    if available.is_empty() {
        return Err(QoderError::InvalidModelList);
    }
    let mut configured = HashSet::new();
    for capability in allowlist {
        if !is_identifier(&capability.model)
            || capability.efforts.is_empty()
            || !configured.insert(capability.model.as_str())
            || capability
                .efforts
                .iter()
                .any(|effort| !is_identifier(effort))
            || capability.efforts.iter().collect::<HashSet<_>>().len() != capability.efforts.len()
        {
            return Err(QoderError::InvalidModelList);
        }
        if !available.contains(capability.model.as_str()) {
            return Err(QoderError::ModelUnavailable);
        }
    }
    Ok(())
}

pub fn parse_result(
    exit_code: Option<i32>,
    stdout: &[u8],
    max_output_bytes: usize,
    executor: Executor,
    invocation_key: String,
) -> Result<QoderResult, QoderError> {
    let result = parse_outer(exit_code, stdout, max_output_bytes)?;
    let usage = usage(&result, invocation_key)?;
    let envelope: AiResultEnvelope =
        serde_json::from_str(&result.result).map_err(|_| QoderError::InvalidOutput)?;
    if envelope_executor(&envelope) != executor {
        return Err(QoderError::InvalidOutput);
    }
    Ok(QoderResult { envelope, usage })
}

pub(crate) fn parse_usage(
    exit_code: Option<i32>,
    stdout: &[u8],
    max_output_bytes: usize,
    invocation_key: String,
) -> Result<UsageUpdate, QoderError> {
    usage(
        &parse_outer(exit_code, stdout, max_output_bytes)?,
        invocation_key,
    )
}

fn parse_outer(
    exit_code: Option<i32>,
    stdout: &[u8],
    max_output_bytes: usize,
) -> Result<JsonResult, QoderError> {
    if exit_code != Some(0) {
        return Err(QoderError::NonZeroExit(exit_code));
    }
    if stdout.len() > max_output_bytes {
        return Err(QoderError::OutputTooLarge);
    }
    let result: JsonResult =
        serde_json::from_slice(stdout).map_err(|_| QoderError::InvalidOutput)?;
    if result.kind != "result" || result.subtype != "success" || result.is_error {
        return Err(QoderError::InvalidOutput);
    }
    Ok(result)
}

fn usage(result: &JsonResult, invocation_key: String) -> Result<UsageUpdate, QoderError> {
    Ok(UsageUpdate::Final {
        invocation_key,
        origin: UsageOrigin::Observed,
        input_tokens: None,
        output_tokens: None,
        cost_micros: None,
        credits_micros: Some(credits_to_micros(result.total_credits.get())?),
    })
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
#[allow(dead_code)]
struct JsonResult {
    #[serde(rename = "type")]
    kind: String,
    subtype: String,
    duration_ms: serde::de::IgnoredAny,
    duration_api_ms: serde::de::IgnoredAny,
    is_error: bool,
    num_turns: serde::de::IgnoredAny,
    result: String,
    stop_reason: serde::de::IgnoredAny,
    total_cost_usd: serde::de::IgnoredAny,
    total_credits: Box<RawValue>,
    usage: serde::de::IgnoredAny,
    #[serde(rename = "modelUsage")]
    model_usage: serde::de::IgnoredAny,
    permission_denials: serde::de::IgnoredAny,
    fast_mode_state: serde::de::IgnoredAny,
    origin: Option<serde::de::IgnoredAny>,
    uuid: serde::de::IgnoredAny,
    session_id: serde::de::IgnoredAny,
}

fn credits_to_micros(text: &str) -> Result<u64, QoderError> {
    if text.starts_with('-') {
        return Err(QoderError::InvalidCredits);
    }
    let (mantissa, exponent) =
        text.split_once(['e', 'E'])
            .map_or((text, 0), |(value, exponent)| {
                exponent
                    .parse::<i32>()
                    .map(|exponent| (value, exponent))
                    .unwrap_or(("", i32::MIN))
            });
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    if whole.is_empty()
        || !whole
            .bytes()
            .chain(fraction.bytes())
            .all(|b| b.is_ascii_digit())
    {
        return Err(QoderError::InvalidCredits);
    }
    let digits = format!("{whole}{fraction}");
    let fraction_len = i32::try_from(fraction.len()).map_err(|_| QoderError::InvalidCredits)?;
    let scale = exponent
        .checked_sub(fraction_len)
        .and_then(|value| value.checked_add(6))
        .ok_or(QoderError::InvalidCredits)?;
    let mut value = digits
        .parse::<u64>()
        .map_err(|_| QoderError::InvalidCredits)?;
    if scale < 0 {
        let discarded = usize::try_from(-scale).map_err(|_| QoderError::InvalidCredits)?;
        if discarded > digits.len()
            || !digits[digits.len() - discarded..]
                .bytes()
                .all(|b| b == b'0')
        {
            return Err(QoderError::InvalidCredits);
        }
        value = digits[..digits.len() - discarded]
            .parse::<u64>()
            .unwrap_or(0);
    } else {
        for _ in 0..scale {
            value = value.checked_mul(10).ok_or(QoderError::InvalidCredits)?;
        }
    }
    Ok(value)
}

fn envelope_executor(envelope: &AiResultEnvelope) -> Executor {
    match envelope {
        AiResultEnvelope::DocumentTranslate { .. } => Executor::AiDocumentTranslate,
        AiResultEnvelope::DocumentNote { .. } => Executor::AiDocumentNote,
        AiResultEnvelope::VideoNote { .. } => Executor::AiVideoNote,
    }
}

fn is_identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}
