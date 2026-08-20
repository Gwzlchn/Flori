use flori_core::{AiModelCapability, ErrorCode, RegisterRunnerRequest, RunnerTool};

use super::super::StoreError;

pub(super) struct NormalizedCapabilities {
    pub tools_json: String,
    pub ai_models_json: String,
    pub ai_models: Vec<AiModelCapability>,
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
