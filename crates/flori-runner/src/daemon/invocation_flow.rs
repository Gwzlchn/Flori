use flori_core::{
    AiResultEnvelope, DocumentStructure, ErrorCode, Executor, TaskClaim, UsageUpdate,
    validate_pdf_evidence,
};
use tokio::sync::watch;

use crate::RunnerClient;

use super::{DaemonConfig, input::PreparedInput, invoke, output};

const PRIMARY: &str = "primary";
const REPAIR: &str = "repair";
const REPAIR_SUMMARY: &str = "evidence_invalid: return the complete corrected result. Every PDF evidence item must use the downloaded document's source_artifact_id, an existing page and bounded bbox, and an exact quote from the enclosing text, figure caption, or table. Every source fact, summary, and term must reference valid evidence IDs, and every candidate must be used.";

pub(super) async fn run(
    client: &RunnerClient,
    config: &DaemonConfig,
    claim: &TaskClaim,
    prepared: PreparedInput,
    cancel: &mut watch::Receiver<bool>,
) -> Result<(), ErrorCode> {
    let mut invocation_keys = Vec::with_capacity(2);
    if !start(client, config, claim, PRIMARY).await? {
        let outcome = invoke::not_invoked(ErrorCode::UsageConflict)?;
        return fail(
            client,
            config,
            claim,
            &invocation_keys,
            &outcome,
            ErrorCode::UsageConflict,
        )
        .await;
    }
    invocation_keys.push(PRIMARY.to_owned());
    let mut outcome = invoke(
        config,
        claim,
        PRIMARY,
        prepared.prompt.clone(),
        &prepared.workspace,
        cancel,
    )
    .await?;
    if let Err(code) = finalize_usage(client, claim, &outcome).await {
        return fail(client, config, claim, &invocation_keys, &outcome, code).await;
    }

    if precheck(claim.executor, prepared.document.as_ref(), &outcome)
        == Err(ErrorCode::EvidenceInvalid)
    {
        if !start(client, config, claim, REPAIR).await? {
            return fail(
                client,
                config,
                claim,
                &invocation_keys,
                &outcome,
                ErrorCode::UsageConflict,
            )
            .await;
        }
        invocation_keys.push(REPAIR.to_owned());
        outcome = invoke(
            config,
            claim,
            REPAIR,
            repair_prompt(&prepared.prompt),
            &prepared.workspace,
            cancel,
        )
        .await?;
        if let Err(code) = finalize_usage(client, claim, &outcome).await {
            return fail(client, config, claim, &invocation_keys, &outcome, code).await;
        }
    }

    let error = outcome
        .result
        .as_ref()
        .err()
        .copied()
        .or_else(|| precheck(claim.executor, prepared.document.as_ref(), &outcome).err());
    if let Some(code) = error {
        return fail(client, config, claim, &invocation_keys, &outcome, code).await;
    }
    match output::success(client, claim, config.tool, &invocation_keys, &outcome).await {
        Ok(()) => Ok(()),
        Err(code) => fail(client, config, claim, &invocation_keys, &outcome, code).await,
    }
}

async fn start(
    client: &RunnerClient,
    config: &DaemonConfig,
    claim: &TaskClaim,
    invocation_key: &str,
) -> Result<bool, ErrorCode> {
    let update = UsageUpdate::Started {
        invocation_key: invocation_key.to_owned(),
        tool: config.tool,
        model: claim.model.clone().ok_or(ErrorCode::CorruptState)?,
        effort: claim.effort.clone().ok_or(ErrorCode::CorruptState)?,
    };
    client
        .update_usage(claim.exec_id, &update)
        .await
        .map(|ack| ack.applied)
        .map_err(|error| error.code())
}

async fn invoke(
    config: &DaemonConfig,
    claim: &TaskClaim,
    invocation_key: &str,
    prompt: String,
    workspace: &std::path::Path,
    cancel: &mut watch::Receiver<bool>,
) -> Result<invoke::InvocationOutcome, ErrorCode> {
    match invoke::run(config, claim, invocation_key, prompt, workspace, cancel).await {
        Ok(outcome) => Ok(outcome),
        Err(error) => {
            let mut outcome = invoke::not_invoked(error)?;
            outcome.usage = Some(invoke::unavailable(invocation_key));
            Ok(outcome)
        }
    }
}

async fn finalize_usage(
    client: &RunnerClient,
    claim: &TaskClaim,
    outcome: &invoke::InvocationOutcome,
) -> Result<(), ErrorCode> {
    if let Some(usage) = &outcome.usage {
        client
            .update_usage(claim.exec_id, usage)
            .await
            .map_err(|error| error.code())?;
    }
    Ok(())
}

fn precheck(
    executor: Executor,
    document: Option<&DocumentStructure>,
    outcome: &invoke::InvocationOutcome,
) -> Result<(), ErrorCode> {
    let Ok(result) = &outcome.result else {
        return Ok(());
    };
    match (executor, document, result) {
        (
            Executor::AiDocumentNote,
            Some(document),
            AiResultEnvelope::DocumentNote {
                smart_note_markdown,
                summary_markdown,
                terms,
                ..
            },
        ) => {
            validate_pdf_evidence(document, terms, smart_note_markdown, summary_markdown).map(drop)
        }
        (Executor::AiDocumentNote, _, _) => Err(ErrorCode::CorruptState),
        _ => Ok(()),
    }
}

fn repair_prompt(prompt: &str) -> String {
    let mut repaired = prompt.to_owned();
    if !repaired.ends_with('\n') {
        repaired.push('\n');
    }
    repaired.push_str("EVIDENCE PRECHECK ERROR ");
    repaired.push_str(&REPAIR_SUMMARY.len().to_string());
    repaired.push('\n');
    repaired.push_str(REPAIR_SUMMARY);
    repaired.push('\n');
    repaired
}

async fn fail(
    client: &RunnerClient,
    config: &DaemonConfig,
    claim: &TaskClaim,
    invocation_keys: &[String],
    outcome: &invoke::InvocationOutcome,
    code: ErrorCode,
) -> Result<(), ErrorCode> {
    output::failure(client, claim, config.tool, invocation_keys, outcome, code).await
}
