use std::{
    fmt::Write as _,
    fs,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
};

use flori_core::{
    AiModelCapability, AiResultEnvelope, AiResultSchema, ArtifactId, CreateRunnerSlot,
    EvidenceEntry, EvidenceId, EvidenceLocator, PipelineId, PipelineRevisionId,
    RegisterRunnerRequest, RunnerId, RunnerTool, RunnerToolCapability, Sha256Digest, TermEntry,
    TermsManifest, TermsManifestSchema, VideoKeyframe,
};
use flori_pipeline::compile;
use flori_store::Store;
use sha2::{Digest, Sha256};
use sqlx::SqlitePool;

pub(super) const MODEL: &str = "fake-video-model";
pub(super) const EFFORT: &str = "high";
pub(super) const MEDIA_KEY: &str = "video-product-media";
pub(super) const DOWNLOAD_KEY: &str = "video-product-download";
pub(super) const AI_KEY: &str = "video-product-ai";

pub(super) async fn seed(
    store: &Store,
    pool: &SqlitePool,
) -> (
    flori_core::DomainId,
    PipelineId,
    RunnerId,
    RunnerId,
    RunnerId,
) {
    let domain = flori_core::DomainId::generate();
    sqlx::query("INSERT INTO domains(id,slug,name,profile_text,created_at_ms,updated_at_ms) VALUES(?,?,'Video','Video knowledge.',0,0)")
        .bind(domain.to_string()).bind(format!("video-{domain}")).execute(pool).await.expect("domain");
    let prompt = include_str!("../../../../prompts/video_note.md");
    sqlx::query("INSERT INTO prompts(key,content,sha256,updated_at_ms) VALUES('video_note',?,?,0)")
        .bind(prompt)
        .bind(digest(prompt.as_bytes()).as_str())
        .execute(pool)
        .await
        .expect("prompt");
    let yaml = include_str!("../../../../pipelines/video.yml");
    let compilation = compile("video", yaml.as_bytes()).expect("video Pipeline");
    let pipeline = PipelineId::generate();
    store
        .register_pipeline_revision(
            pipeline,
            PipelineRevisionId::generate(),
            &compilation,
            "wp12-b",
            yaml,
            0,
        )
        .await
        .expect("pipeline revision");
    let download = slot(
        store,
        "video-download",
        "download",
        None,
        None,
        DOWNLOAD_KEY,
    )
    .await;
    let media = slot(store, "video-media", "media", None, None, MEDIA_KEY).await;
    let ai = slot(store, "video-ai", "ai", Some(MODEL), Some(EFFORT), AI_KEY).await;
    (domain, pipeline, download, media, ai)
}

async fn slot(
    store: &Store,
    name: &str,
    tag: &str,
    model: Option<&str>,
    effort: Option<&str>,
    key: &str,
) -> RunnerId {
    store
        .create_runner_slot(
            &CreateRunnerSlot {
                name: name.into(),
                tags: vec![tag.into()],
                max_concurrency: 1,
                default_model: model.map(str::to_owned),
                default_effort: effort.map(str::to_owned),
            },
            &digest(key.as_bytes()),
            i64::MAX,
            1,
        )
        .await
        .expect("runner slot")
}

pub(super) fn download_capabilities() -> RegisterRunnerRequest {
    RegisterRunnerRequest {
        tools: vec![RunnerToolCapability {
            tool: RunnerTool::Ffprobe,
            version: "5.1.9".into(),
        }],
        ai_models: vec![],
    }
}

pub(super) fn media_capabilities() -> RegisterRunnerRequest {
    RegisterRunnerRequest {
        tools: [
            (RunnerTool::Ffmpeg, "5.1.9"),
            (RunnerTool::Ffprobe, "5.1.9"),
            (RunnerTool::FasterWhisper, "1.2.1"),
        ]
        .into_iter()
        .map(|(tool, version)| RunnerToolCapability {
            tool,
            version: version.into(),
        })
        .collect(),
        ai_models: vec![],
    }
}

pub(super) fn ai_capabilities() -> RegisterRunnerRequest {
    RegisterRunnerRequest {
        tools: vec![RunnerToolCapability {
            tool: RunnerTool::QoderCli,
            version: flori_runner::QODERCLI_VERSION.into(),
        }],
        ai_models: vec![AiModelCapability {
            model: MODEL.into(),
            efforts: vec![EFFORT.into()],
        }],
    }
}

pub(super) fn envelope(source: ArtifactId, frame: VideoKeyframe) -> AiResultEnvelope {
    let evidence = EvidenceId::generate();
    let marker = format!("[[evidence:{evidence}]]");
    AiResultEnvelope::VideoNote {
        schema: AiResultSchema::V1,
        smart_note_markdown: format!(
            "# 视频笔记\n\n## 来源事实\n\nHello video {marker}\n\n## AI 分析\n\n该片段展示了可验证的时间证据。\n"
        ),
        summary_markdown: format!("Hello video {marker}\n"),
        terms: TermsManifest {
            schema: TermsManifestSchema::V1,
            terms: vec![TermEntry {
                term: "Video".into(),
                explanation: "时间证据".into(),
                evidence_ids: vec![evidence],
            }],
            evidence_candidates: vec![EvidenceEntry {
                evidence_id: evidence,
                source_artifact_id: source,
                locator: EvidenceLocator::Video {
                    start_ms: 0,
                    end_ms: 1_500,
                    keyframe: Some(frame),
                },
                quote: "Hello video".into(),
            }],
        },
    }
}

pub(super) fn write_media_tools(root: &Path) -> MediaTools {
    let bin = root.join("tools");
    let model = root.join("model/base");
    fs::create_dir_all(&bin).expect("tools");
    fs::create_dir_all(&model).expect("model");
    for name in [
        "config.json",
        "model.bin",
        "tokenizer.json",
        "vocabulary.json",
    ] {
        fs::write(model.join(name), "fixture").expect("model file");
    }
    let ffprobe = script(
        &bin,
        "ffprobe",
        "printf '%s' '{\"streams\":[{\"codec_type\":\"video\",\"width\":320,\"height\":180,\"avg_frame_rate\":\"10/1\"},{\"codec_type\":\"audio\"}],\"format\":{\"duration\":\"3.000000\"}}'\n",
    );
    let ffmpeg = script(
        &bin,
        "ffmpeg",
        r#"case "$*" in
  *showinfo*) exit 0 ;;
  *rawvideo*) head -c 1024 /dev/zero ;;
  *) printf '\377\330\377\340fixture-jpeg' ;;
esac
"#,
    );
    let python = script(
        &bin,
        "python",
        "printf '%s' '{\"language\":\"en\",\"segments\":[{\"start_ms\":0,\"end_ms\":1500,\"text\":\"Hello video\"},{\"start_ms\":1500,\"end_ms\":3000,\"text\":\"Second cue\"}]}'\n",
    );
    MediaTools {
        ffmpeg,
        ffprobe,
        python,
        model,
    }
}

pub(super) struct MediaTools {
    pub ffmpeg: PathBuf,
    pub ffprobe: PathBuf,
    pub python: PathBuf,
    pub model: PathBuf,
}

fn script(root: &Path, name: &str, body: &str) -> PathBuf {
    let path = root.join(name);
    fs::write(&path, format!("#!/bin/sh\n{body}")).expect("script");
    fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).expect("executable");
    path
}

pub(super) fn digest(bytes: &[u8]) -> Sha256Digest {
    let mut value = String::with_capacity(64);
    for byte in Sha256::digest(bytes) {
        write!(&mut value, "{byte:02x}").expect("String");
    }
    Sha256Digest::parse(value).expect("digest")
}
