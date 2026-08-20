use super::*;
use std::collections::BTreeSet;

mod validate;
use validate::validate_core_input;

pub(super) fn validate_references(
    tasks: &BTreeMap<String, CompiledTask>,
) -> Result<(), CompileError> {
    for task in tasks.values() {
        for need in &task.needs {
            if !tasks.contains_key(need) {
                return Err(invalid());
            }
        }
        for (field, value) in &task.inputs {
            let reference = input_reference(task.executor, field, value)?;
            validate_reference(tasks, task, field, reference)?;
        }
    }
    Ok(())
}

fn validate_reference(
    tasks: &BTreeMap<String, CompiledTask>,
    task: &CompiledTask,
    field: &str,
    reference: &Reference,
) -> Result<(), CompileError> {
    if task.executor == Executor::CoreValidate
        && matches!(field, "source" | "notes")
        && !matches!(reference, Reference::Need(_))
    {
        return Err(invalid());
    }
    let (producer_key, artifact_name) = match reference {
        Reference::Source
            if field == "source"
                && matches!(
                    task.executor,
                    Executor::DocumentAcquire
                        | Executor::VideoAcquire
                        | Executor::VideoSubscription
                ) =>
        {
            return Ok(());
        }
        Reference::DomainProfile if field == "profile" && is_ai(task.executor) => return Ok(()),
        Reference::Prompt(_) if field == "prompt" && is_ai(task.executor) => return Ok(()),
        Reference::Need(producer)
            if field == "notes"
                || (task.executor == Executor::CoreValidate && field == "source") =>
        {
            (producer, None)
        }
        Reference::NeedArtifact {
            task: producer,
            artifact,
        } => (producer, Some(artifact)),
        _ => return Err(invalid()),
    };
    if !task.needs.iter().any(|need| need == producer_key) {
        return Err(invalid());
    }
    let producer = tasks.get(producer_key).ok_or_else(invalid)?;
    if task.executor == Executor::CoreValidate {
        validate_core_input(field, producer)?;
    }
    if !producer.rules.is_empty() && producer.rules != task.rules {
        return Err(invalid());
    }
    if let Some(name) = artifact_name {
        let output = producer
            .artifacts
            .iter()
            .find(|artifact| artifact.name == *name)
            .ok_or_else(invalid)?;
        if !input_kind_allowed(field, output.kind) {
            return Err(invalid());
        }
    }
    Ok(())
}

pub(super) fn topological_order(
    tasks: &BTreeMap<String, CompiledTask>,
) -> Result<Vec<String>, CompileError> {
    let mut degree = tasks
        .iter()
        .map(|(key, task)| (key.clone(), task.needs.len()))
        .collect::<BTreeMap<_, _>>();
    let mut ready = degree
        .iter()
        .filter(|(_, degree)| **degree == 0)
        .map(|(key, _)| key.clone())
        .collect::<BTreeSet<_>>();
    let mut order = Vec::with_capacity(tasks.len());
    while let Some(key) = ready.pop_first() {
        order.push(key.clone());
        for (child, task) in tasks {
            if task.needs.contains(&key) {
                let child_degree = degree.get_mut(child).expect("all tasks have degree");
                *child_degree -= 1;
                if *child_degree == 0 {
                    ready.insert(child.clone());
                }
            }
        }
    }
    if order.len() != tasks.len() {
        return Err(CompileError::new(ErrorCode::PipelineCycle));
    }
    Ok(order)
}

pub(super) fn validate_rule_outcomes(
    tasks: &BTreeMap<String, CompiledTask>,
) -> Result<(), CompileError> {
    const KINDS: [SourceKind; 8] = [
        SourceKind::Arxiv,
        SourceKind::PdfUrl,
        SourceKind::PdfUpload,
        SourceKind::BilibiliVideo,
        SourceKind::BilibiliChannel,
        SourceKind::YoutubeVideo,
        SourceKind::YoutubeChannel,
        SourceKind::LocalVideo,
    ];
    for kind in KINDS {
        for translate in [false, true] {
            let included = tasks
                .iter()
                .filter(|(_, task)| {
                    task.rules.is_empty()
                        || task
                            .rules
                            .iter()
                            .any(|rule| rule_matches(rule, kind, translate))
                })
                .map(|(key, _)| key)
                .collect::<BTreeSet<_>>();
            let sinks = included
                .iter()
                .filter(|key| {
                    !included
                        .iter()
                        .any(|child| tasks[*child].needs.iter().any(|need| need == **key))
                })
                .copied()
                .collect::<Vec<_>>();
            if sinks.len() != 1 || tasks[sinks[0]].executor != Executor::CorePublish {
                return Err(invalid());
            }
        }
    }
    for (key, task) in tasks {
        if task.executor == Executor::CorePublish
            && tasks.values().any(|other| other.needs.contains(key))
        {
            return Err(invalid());
        }
    }
    Ok(())
}

pub(super) fn validate_artifact(
    executor: Executor,
    artifact: &ArtifactDeclaration,
) -> Result<(), CompileError> {
    if !valid_key(&artifact.name, 48) || !allowed_output(executor, artifact.kind) {
        return Err(invalid());
    }
    let wildcard = valid_artifact_path(&artifact.path).ok_or_else(invalid)?;
    if wildcard != artifact.max_files.is_some()
        || artifact
            .max_files
            .is_some_and(|count| !(1..=256).contains(&count))
    {
        return Err(invalid());
    }
    let audit = matches!(artifact.kind, ArtifactKind::TaskLog | ArtifactKind::AiAudit);
    if !audit && artifact.when != ArtifactWhen::OnSuccess {
        return Err(invalid());
    }
    Ok(())
}

fn allowed_output(executor: Executor, kind: ArtifactKind) -> bool {
    kind == ArtifactKind::TaskLog && !is_core(executor)
        || kind == ArtifactKind::AiAudit && is_ai(executor)
        || matches!(
            (executor, kind),
            (Executor::DocumentAcquire, ArtifactKind::SourceOriginal)
                | (
                    Executor::DocumentExtract,
                    ArtifactKind::DocumentStructure
                        | ArtifactKind::Figure
                        | ArtifactKind::TableRegion
                )
                | (Executor::AiDocumentTranslate, ArtifactKind::Translation)
                | (
                    Executor::AiDocumentNote | Executor::AiVideoNote,
                    ArtifactKind::SmartNote | ArtifactKind::Summary | ArtifactKind::Terms
                )
                | (
                    Executor::VideoAcquire,
                    ArtifactKind::SourceOriginal
                        | ArtifactKind::Subtitle
                        | ArtifactKind::Danmaku
                        | ArtifactKind::PartsManifest
                )
                | (
                    Executor::VideoSubscription,
                    ArtifactKind::SubscriptionManifest
                )
                | (Executor::VideoTranscribe, ArtifactKind::Transcript)
                | (Executor::VideoFrames, ArtifactKind::Keyframe)
                | (Executor::VideoMechanicalNote, ArtifactKind::MechanicalNote)
                | (Executor::CoreValidate, ArtifactKind::Evidence)
        )
}

pub(super) fn validate_input_keys<'a>(
    executor: Executor,
    keys: impl Iterator<Item = &'a str>,
) -> Result<(), CompileError> {
    let (required, optional): (&[&str], &[&str]) = match executor {
        Executor::DocumentAcquire | Executor::VideoAcquire | Executor::VideoSubscription => {
            (&["source"], &[])
        }
        Executor::DocumentExtract => (&["pdf"], &[]),
        Executor::AiDocumentTranslate | Executor::AiDocumentNote => {
            (&["document", "prompt"], &["profile"])
        }
        Executor::VideoTranscribe => (&["video"], &["subtitle"]),
        Executor::VideoFrames => (&["video", "transcript"], &[]),
        Executor::VideoMechanicalNote => (&["transcript", "frames"], &[]),
        Executor::AiVideoNote => (
            &["transcript", "mechanical_note", "frames", "prompt"],
            &["profile"],
        ),
        Executor::CoreValidate => (&["source", "notes"], &[]),
        Executor::CorePublish => (&["validated"], &[]),
    };
    let keys = keys.collect::<Vec<_>>();
    (required.iter().all(|key| keys.contains(key))
        && keys
            .iter()
            .all(|key| required.contains(key) || optional.contains(key)))
    .then_some(())
    .ok_or_else(invalid)
}

pub(super) fn parse_rule(expression: &str) -> Result<RuleCondition, CompileError> {
    let parts = expression.split_whitespace().collect::<Vec<_>>();
    if parts.len() != 3 || !matches!(parts[1], "==" | "!=") {
        return Err(invalid());
    }
    let equal = parts[1] == "==";
    match parts[0] {
        "$source.kind" => parse_source_kind(parts[2])
            .map(|value| RuleCondition::SourceKind { equal, value })
            .ok_or_else(invalid),
        "$job.translate" => match parts[2] {
            "true" => Ok(RuleCondition::JobTranslate { equal, value: true }),
            "false" => Ok(RuleCondition::JobTranslate {
                equal,
                value: false,
            }),
            _ => Err(invalid()),
        },
        _ => Err(invalid()),
    }
}

fn parse_source_kind(value: &str) -> Option<SourceKind> {
    match value.strip_prefix('"')?.strip_suffix('"')? {
        "arxiv" => Some(SourceKind::Arxiv),
        "pdf_url" => Some(SourceKind::PdfUrl),
        "pdf_upload" => Some(SourceKind::PdfUpload),
        "bilibili_video" => Some(SourceKind::BilibiliVideo),
        "bilibili_channel" => Some(SourceKind::BilibiliChannel),
        "youtube_video" => Some(SourceKind::YoutubeVideo),
        "youtube_channel" => Some(SourceKind::YoutubeChannel),
        "local_video" => Some(SourceKind::LocalVideo),
        _ => None,
    }
}

fn rule_matches(rule: &RuleCondition, source_kind: SourceKind, translate: bool) -> bool {
    match rule {
        RuleCondition::SourceKind { equal, value } => (*value == source_kind) == *equal,
        RuleCondition::JobTranslate { equal, value } => (*value == translate) == *equal,
    }
}

pub(super) fn is_core(executor: Executor) -> bool {
    matches!(executor, Executor::CoreValidate | Executor::CorePublish)
}
