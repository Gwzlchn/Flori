#[path = "../src/media/video.rs"]
#[allow(dead_code, unreachable_pub, unused_imports)]
mod video;

use std::{fs, os::unix::fs::PermissionsExt, path::PathBuf, time::Duration};

use flori_core::ArtifactId;
use video::{
    VideoMediaError,
    transcribe::{FasterWhisperConfig, transcribe_video},
};

#[tokio::test]
async fn transcriber_is_offline_fixed_and_strict() {
    let fixture = Fixture::new();
    let transcript = transcribe_video(
        &fixture.config(),
        &fixture.input,
        &fixture.workspace,
        ArtifactId::generate(),
        3_000,
    )
    .await
    .expect("transcript");
    assert_eq!(transcript.language, "en");
    assert_eq!(transcript.cues.len(), 2);
    assert_eq!(
        (transcript.cues[1].start_ms, transcript.cues[1].end_ms),
        (1_000, 3_000)
    );
    let arguments = fs::read_to_string(fixture.root.join("arguments")).expect("arguments");
    assert!(arguments.contains("-I"));
    assert!(arguments.contains("/base"));
    let environment = fs::read_to_string(fixture.root.join("environment")).expect("environment");
    assert_eq!(environment, "offline=1 transformers=1 python=1 hash=0\n");
}

#[tokio::test]
async fn rejects_model_and_transcript_drift() {
    let fixture = Fixture::new();
    let mut config = fixture.config();
    config.model_name = "large-v3".into();
    assert_eq!(
        transcribe_video(
            &config,
            &fixture.input,
            &fixture.workspace,
            ArtifactId::generate(),
            3_000,
        )
        .await,
        Err(VideoMediaError::InvalidTranscriber)
    );

    fs::write(
        &fixture.python,
        "#!/bin/sh\nprintf '%s' '{\"language\":\"en\",\"segments\":[{\"start_ms\":0,\"end_ms\":3001,\"text\":\"bad\"}]}'\n",
    )
    .expect("rewrite fake Python");
    assert_eq!(
        transcribe_video(
            &fixture.config(),
            &fixture.input,
            &fixture.workspace,
            ArtifactId::generate(),
            3_000,
        )
        .await,
        Err(VideoMediaError::InvalidTranscriber)
    );
}

struct Fixture {
    root: PathBuf,
    workspace: PathBuf,
    input: PathBuf,
    python: PathBuf,
    model: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let root =
            std::env::temp_dir().join(format!("flori-transcribe-{}", ArtifactId::generate()));
        let workspace = root.join("workspace");
        let model = root.join("base");
        fs::create_dir_all(&workspace).expect("workspace");
        fs::create_dir(&model).expect("model");
        for name in [
            "config.json",
            "model.bin",
            "tokenizer.json",
            "vocabulary.json",
        ] {
            fs::write(model.join(name), b"model").expect("model file");
        }
        let input = root.join("input.mp4");
        fs::write(&input, b"video").expect("input");
        let python = root.join("python3");
        let body = format!(
            concat!(
                "#!/bin/sh\n",
                "printf '%s\\n' \"$*\" > '{}'\n",
                "printf 'offline=%s transformers=%s python=%s hash=%s\\n' ",
                "\"$HF_HUB_OFFLINE\" \"$TRANSFORMERS_OFFLINE\" ",
                "\"$PYTHONDONTWRITEBYTECODE\" \"$PYTHONHASHSEED\" > '{}'\n",
                "printf '%s' '{{\"language\":\"en\",\"segments\":[",
                "{{\"start_ms\":0,\"end_ms\":1000,\"text\":\"one\"}},",
                "{{\"start_ms\":1000,\"end_ms\":3000,\"text\":\"two\"}}]}}'\n"
            ),
            root.join("arguments").display(),
            root.join("environment").display(),
        );
        fs::write(&python, body).expect("fake Python");
        fs::set_permissions(&python, fs::Permissions::from_mode(0o700)).expect("executable");
        Self {
            root,
            workspace,
            input,
            python,
            model,
        }
    }

    fn config(&self) -> FasterWhisperConfig {
        FasterWhisperConfig {
            python: self.python.clone(),
            model_dir: self.model.clone(),
            model_name: "base".into(),
            timeout: Duration::from_secs(2),
            max_output_bytes: 4096,
        }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).expect("cleanup");
    }
}
