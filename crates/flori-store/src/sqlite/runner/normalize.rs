use flori_core::{
    AiModelCapability, AiModels, ErrorCode, Executor, RegisterRunnerRequest, RunnerTags,
    RunnerTool, RunnerTools, SourceKind,
};
use sqlx::Row;

use super::super::StoreError;

pub(super) struct NormalizedCapabilities {
    pub tools_json: String,
    pub ai_models_json: String,
    pub ai_models: Vec<AiModelCapability>,
}

pub(super) struct RunnerInventory {
    pub config_revision: u64,
    pub max_concurrency: i64,
    pub tags: RunnerTags,
    pub tools: RunnerTools,
    pub ai_models: AiModels,
    pub default_model: Option<String>,
    pub default_effort: Option<String>,
}

pub(super) async fn load_inventory(
    transaction: &mut sqlx::Transaction<'_, sqlx::Sqlite>,
    runner_id: &str,
) -> Result<RunnerInventory, StoreError> {
    let row = sqlx::query(
        "SELECT state,config_revision,max_concurrency,tags_json,tools_json,ai_models_json, \
         default_model,default_effort FROM runners WHERE id=?",
    )
    .bind(runner_id)
    .fetch_optional(&mut **transaction)
    .await?
    .ok_or_else(|| StoreError::new(ErrorCode::RunnerUnavailable))?;
    if row.try_get::<String, _>("state")? != "enabled" {
        return Err(StoreError::new(ErrorCode::RunnerDisabled));
    }
    let tags_raw: String = row.try_get("tags_json")?;
    let tools_raw: String = row.try_get("tools_json")?;
    let models_raw: String = row.try_get("ai_models_json")?;
    let tags: RunnerTags = serde_json::from_str(&tags_raw).map_err(|_| corrupt())?;
    let tools: RunnerTools = serde_json::from_str(&tools_raw).map_err(|_| corrupt())?;
    let ai_models: AiModels = serde_json::from_str(&models_raw).map_err(|_| corrupt())?;
    let normalized = capabilities(&RegisterRunnerRequest {
        tools: tools.clone(),
        ai_models: ai_models.clone(),
    })?;
    if tags_json(&tags)? != tags_raw
        || normalized.tools_json != tools_raw
        || normalized.ai_models_json != models_raw
    {
        return Err(corrupt());
    }
    Ok(RunnerInventory {
        config_revision: u64::try_from(row.try_get::<i64, _>("config_revision")?)
            .map_err(|_| corrupt())?,
        max_concurrency: row.try_get("max_concurrency")?,
        tags,
        tools,
        ai_models,
        default_model: row.try_get("default_model")?,
        default_effort: row.try_get("default_effort")?,
    })
}

pub(super) fn supports_executor(
    executor: Executor,
    source_kind: SourceKind,
    inventory: &RunnerInventory,
) -> bool {
    let has = |tool| inventory.tools.iter().any(|entry| entry.tool == tool);
    match executor {
        Executor::DocumentAcquire | Executor::VideoMechanicalNote => true,
        Executor::DocumentExtract => has(RunnerTool::PdfExtractor),
        Executor::VideoAcquire | Executor::VideoSubscription => match source_kind {
            SourceKind::BilibiliVideo | SourceKind::BilibiliChannel => has(RunnerTool::Yutto),
            SourceKind::YoutubeVideo | SourceKind::YoutubeChannel => has(RunnerTool::YtDlp),
            SourceKind::LocalVideo => executor == Executor::VideoAcquire,
            SourceKind::Arxiv | SourceKind::PdfUrl | SourceKind::PdfUpload => false,
        },
        Executor::VideoTranscribe => has(RunnerTool::WhisperCpp) || has(RunnerTool::FasterWhisper),
        Executor::VideoFrames => has(RunnerTool::Ffmpeg) && has(RunnerTool::Ffprobe),
        Executor::AiDocumentTranslate | Executor::AiDocumentNote | Executor::AiVideoNote => {
            has(RunnerTool::QoderCli) || has(RunnerTool::CodexCli)
        }
        Executor::CoreValidate | Executor::CorePublish => false,
    }
}

pub(super) fn tags_json(tags: &[String]) -> Result<String, StoreError> {
    let mut tags = tags.to_vec();
    tags.sort();
    if tags.iter().any(|tag| !identifier(tag)) || tags.windows(2).any(|pair| pair[0] == pair[1]) {
        return Err(StoreError::new(ErrorCode::InvalidRequest));
    }
    serde_json::to_string(&tags).map_err(|_| StoreError::new(ErrorCode::InvalidRequest))
}

pub(super) fn capabilities(
    request: &RegisterRunnerRequest,
) -> Result<NormalizedCapabilities, StoreError> {
    let mut tools = request.tools.clone();
    tools.sort_by(|left, right| tool_name(left.tool).cmp(tool_name(right.tool)));
    if tools.iter().any(|tool| !identifier(&tool.version))
        || tools.windows(2).any(|pair| pair[0].tool == pair[1].tool)
    {
        return Err(StoreError::new(ErrorCode::InvalidRequest));
    }
    let mut ai_models = request.ai_models.clone();
    for capability in &mut ai_models {
        capability.efforts.sort();
        if !identifier(&capability.model)
            || capability.efforts.is_empty()
            || capability.efforts.iter().any(|effort| !identifier(effort))
            || capability.efforts.windows(2).any(|pair| pair[0] == pair[1])
        {
            return Err(StoreError::new(ErrorCode::InvalidRequest));
        }
    }
    ai_models.sort_by(|left, right| left.model.cmp(&right.model));
    if ai_models
        .windows(2)
        .any(|pair| pair[0].model == pair[1].model)
    {
        return Err(StoreError::new(ErrorCode::InvalidRequest));
    }
    Ok(NormalizedCapabilities {
        tools_json: serde_json::to_string(&tools)
            .map_err(|_| StoreError::new(ErrorCode::InvalidRequest))?,
        ai_models_json: serde_json::to_string(&ai_models)
            .map_err(|_| StoreError::new(ErrorCode::InvalidRequest))?,
        ai_models,
    })
}

pub(super) fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-'))
}

const fn tool_name(tool: RunnerTool) -> &'static str {
    match tool {
        RunnerTool::PdfExtractor => "pdf_extractor",
        RunnerTool::YtDlp => "yt_dlp",
        RunnerTool::Yutto => "yutto",
        RunnerTool::Ffmpeg => "ffmpeg",
        RunnerTool::Ffprobe => "ffprobe",
        RunnerTool::WhisperCpp => "whisper_cpp",
        RunnerTool::FasterWhisper => "faster_whisper",
        RunnerTool::QoderCli => "qoder_cli",
        RunnerTool::CodexCli => "codex_cli",
    }
}

fn corrupt() -> StoreError {
    StoreError::new(ErrorCode::CorruptState)
}
