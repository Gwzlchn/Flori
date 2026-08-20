//! Git 中 Pipeline YAML 的严格、确定性编译边界。

#![forbid(unsafe_code)]

mod semantic;
mod source;

use std::collections::BTreeMap;
use std::fmt::{self, Write};

use flori_core::{
    ArtifactKind, ArtifactWhen, CONTRACT_REVISION, ErrorCode, Executor, PIPELINE_COMPILER_VERSION,
    SourceKind,
};
use semantic::*;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use source::{RawInput, RawTask, input_reference};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CompileError {
    code: ErrorCode,
}

impl CompileError {
    const fn new(code: ErrorCode) -> Self {
        Self { code }
    }

    pub const fn code(&self) -> ErrorCode {
        self.code
    }
}

impl fmt::Display for CompileError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("pipeline compilation failed")
    }
}

impl std::error::Error for CompileError {}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Compilation {
    pub pipeline: CompiledPipeline,
    pub canonical_json: String,
    pub sha256: String,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct CompiledPipeline {
    pub contract_revision: &'static str,
    pub compiler_version: u8,
    pub pipeline_key: String,
    pub tasks: BTreeMap<String, CompiledTask>,
    pub topological_order: Vec<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CompiledTask {
    pub executor: Executor,
    #[serde(default, rename = "with")]
    pub inputs: BTreeMap<String, InputValue>,
    #[serde(default)]
    pub needs: Vec<String>,
    #[serde(default)]
    pub rules: Vec<RuleCondition>,
    #[serde(default)]
    pub tags: Vec<String>,
    #[serde(default)]
    pub retry: u8,
    pub timeout_ms: u64,
    #[serde(default)]
    pub artifacts: Vec<ArtifactDeclaration>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ArtifactDeclaration {
    pub name: String,
    pub kind: ArtifactKind,
    pub path: String,
    pub required: bool,
    pub when: ArtifactWhen,
    pub max_files: Option<u16>,
    pub max_bytes: u64,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum InputValue {
    Bool(bool),
    Number(String),
    String(String),
    Reference(Reference),
    List(Vec<Self>),
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Reference {
    Source,
    JobTranslate,
    DomainProfile,
    Prompt(String),
    Need(String),
    NeedArtifact { task: String, artifact: String },
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RuleCondition {
    SourceKind { equal: bool, value: SourceKind },
    JobTranslate { equal: bool, value: bool },
}

pub fn compile(pipeline_key: &str, yaml: &[u8]) -> Result<Compilation, CompileError> {
    valid_key(pipeline_key, 48)
        .then_some(())
        .ok_or_else(invalid)?;
    let raw = source::parse(yaml)?;
    if raw.is_empty() {
        return Err(invalid());
    }
    let mut tasks = BTreeMap::new();
    for (key, task) in raw {
        valid_key(&key, 48).then_some(()).ok_or_else(invalid)?;
        tasks.insert(key, normalize_task(task)?);
    }
    validate_references(&tasks)?;
    let topological_order = topological_order(&tasks)?;
    validate_rule_outcomes(&tasks)?;
    let pipeline = CompiledPipeline {
        contract_revision: CONTRACT_REVISION,
        compiler_version: PIPELINE_COMPILER_VERSION,
        pipeline_key: pipeline_key.to_owned(),
        tasks,
        topological_order,
    };
    let canonical_json = serde_json::to_string(&pipeline).map_err(|_| invalid())?;
    let mut sha256 = String::with_capacity(64);
    for byte in Sha256::digest(canonical_json.as_bytes()) {
        write!(&mut sha256, "{byte:02x}").map_err(|_| invalid())?;
    }
    Ok(Compilation {
        pipeline,
        canonical_json,
        sha256,
    })
}

fn normalize_task(raw: RawTask) -> Result<CompiledTask, CompileError> {
    let RawTask {
        executor,
        inputs: raw_inputs,
        mut needs,
        rules,
        mut tags,
        retry,
        timeout,
        mut artifacts,
    } = raw;
    let inputs = raw_inputs
        .into_iter()
        .map(|(key, value)| Ok((key, input_value(value)?)))
        .collect::<Result<BTreeMap<_, _>, CompileError>>()?;
    if retry > 2 || (executor == Executor::CorePublish && retry != 0) {
        return Err(invalid());
    }
    let timeout_ms = parse_timeout(&timeout).ok_or_else(invalid)?;
    sort_unique(&mut needs).then_some(()).ok_or_else(invalid)?;
    sort_unique(&mut tags).then_some(()).ok_or_else(invalid)?;
    if tags.iter().any(|tag| !valid_key(tag, 32))
        || (!is_core(executor) && tags.is_empty())
        || (is_core(executor) && !tags.is_empty())
    {
        return Err(invalid());
    }
    validate_input_keys(executor, inputs.keys().map(String::as_str))?;
    artifacts.sort_by(|a, b| a.name.cmp(&b.name));
    if artifacts
        .windows(2)
        .any(|pair| pair[0].name == pair[1].name)
    {
        return Err(invalid());
    }
    for artifact in &artifacts {
        validate_artifact(executor, artifact)?;
    }
    if is_ai(executor)
        && !artifacts
            .iter()
            .any(|artifact| artifact.kind == ArtifactKind::AiAudit)
    {
        return Err(invalid());
    }
    let rules = rules
        .into_iter()
        .map(|rule| parse_rule(&rule.expression))
        .collect::<Result<_, _>>()?;
    Ok(CompiledTask {
        executor,
        inputs,
        needs,
        rules,
        tags,
        retry,
        timeout_ms,
        artifacts,
    })
}

fn parse_timeout(value: &str) -> Option<u64> {
    let (number, unit) = value.split_at(value.len().checked_sub(1)?);
    let seconds = number.parse::<u64>().ok()?.checked_mul(match unit {
        "s" => 1,
        "m" => 60,
        "h" => 3600,
        _ => return None,
    })?;
    (1..=86_400).contains(&seconds).then_some(seconds * 1_000)
}

fn valid_artifact_path(path: &str) -> Option<bool> {
    if path.is_empty() || path.starts_with('/') || path.contains('\\') {
        return None;
    }
    let parts = path.split('/').collect::<Vec<_>>();
    if parts
        .iter()
        .any(|part| part.is_empty() || *part == "." || *part == ".." || part.starts_with('.'))
    {
        return None;
    }
    let stars = path.matches('*').count();
    (stars <= 1 && (stars == 0 || parts.last()?.contains('*'))).then_some(stars == 1)
}

fn valid_key(value: &str, max: usize) -> bool {
    value.len() <= max
        && value.as_bytes().first().is_some_and(u8::is_ascii_lowercase)
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'_' | b'-')
        })
}

fn sort_unique(values: &mut [String]) -> bool {
    values.sort();
    values.windows(2).all(|pair| pair[0] != pair[1])
}

fn is_ai(executor: Executor) -> bool {
    matches!(
        executor,
        Executor::AiDocumentTranslate | Executor::AiDocumentNote | Executor::AiVideoNote
    )
}

fn input_kind_allowed(executor: Executor, field: &str, kind: ArtifactKind) -> bool {
    match field {
        "pdf" | "video" => kind == ArtifactKind::SourceOriginal,
        "document" => kind == ArtifactKind::DocumentStructure,
        "subtitle" => kind == ArtifactKind::Subtitle,
        "transcript" => kind == ArtifactKind::Transcript,
        "frames" => kind == ArtifactKind::Keyframe,
        "mechanical_note" => kind == ArtifactKind::MechanicalNote,
        "validated" => kind == ArtifactKind::Evidence,
        "source" if executor == Executor::CoreValidate => {
            matches!(
                kind,
                ArtifactKind::DocumentStructure | ArtifactKind::Transcript
            )
        }
        _ => true,
    }
}

fn input_value(value: RawInput) -> Result<InputValue, CompileError> {
    match value {
        RawInput::Bool(value) => Ok(InputValue::Bool(value)),
        RawInput::Number(value) => Ok(InputValue::Number(value)),
        RawInput::String(value) if value.starts_with('$') => parse_reference(&value)
            .map(InputValue::Reference)
            .ok_or_else(invalid),
        RawInput::String(value) => Ok(InputValue::String(value)),
        RawInput::List(values) => values
            .into_iter()
            .map(input_value)
            .collect::<Result<_, _>>()
            .map(InputValue::List),
    }
}

fn parse_reference(value: &str) -> Option<Reference> {
    match value {
        "$source" => Some(Reference::Source),
        "$job.translate" => Some(Reference::JobTranslate),
        "$domain.profile" => Some(Reference::DomainProfile),
        _ if value.starts_with("$prompts.") => {
            let key = &value[9..];
            valid_key(key, 48).then(|| Reference::Prompt(key.to_owned()))
        }
        _ if value.starts_with("$needs.") => {
            let parts = value[7..].split('.').collect::<Vec<_>>();
            if parts.iter().any(|part| !valid_key(part, 48)) {
                return None;
            }
            match parts.as_slice() {
                [task] => Some(Reference::Need((*task).to_owned())),
                [task, artifact] => Some(Reference::NeedArtifact {
                    task: (*task).to_owned(),
                    artifact: (*artifact).to_owned(),
                }),
                _ => None,
            }
        }
        _ => None,
    }
}

fn invalid() -> CompileError {
    CompileError::new(ErrorCode::PipelineInvalid)
}
