#[path = "../src/download/claim.rs"]
mod claim;

use std::{ffi::OsString, path::Path};

use flori_core::{
    ArtifactDeclaration, ArtifactKind, ArtifactWhen, AttemptId, CredentialKind, Executor, JobId,
    ResolvedSource, ResolvedSourceInput, ResolvedTaskInputs, SecretCredential, SecretInputs,
    Sha256Digest, SourceId, SourceInputId, SourceKind, TaskClaim, TaskId,
};

fn declaration(
    kind: ArtifactKind,
    name: &str,
    path: &str,
    required: bool,
    many: bool,
) -> ArtifactDeclaration {
    ArtifactDeclaration {
        name: name.into(),
        kind,
        path: path.into(),
        required,
        when: if kind == ArtifactKind::TaskLog {
            ArtifactWhen::Always
        } else {
            ArtifactWhen::OnSuccess
        },
        max_files: many.then_some(1),
        max_bytes: 1024 * 1024,
    }
}

fn make_claim(kind: SourceKind, canonical_ref: &str) -> TaskClaim {
    let input = (kind == SourceKind::LocalVideo).then(|| ResolvedSourceInput {
        source_input_id: SourceInputId::generate(),
        name: "original".into(),
        media_type: "video/mp4".into(),
        size_bytes: 3,
        sha256: Sha256Digest::parse("a".repeat(64)).expect("digest"),
        download_url: "https://flori.test/api/v1/source-inputs/input/content".into(),
    });
    TaskClaim {
        job_id: JobId::generate(),
        task_id: TaskId::generate(),
        task_key: "acquire".into(),
        exec_id: AttemptId::generate(),
        attempt_no: 1,
        executor: Executor::VideoAcquire,
        timeout_ms: 10_000,
        lease_expires_at_ms: i64::MAX,
        prompt_snapshot_sha256: Sha256Digest::parse("b".repeat(64)).expect("digest"),
        resolved_inputs: ResolvedTaskInputs::VideoAcquire {
            source: ResolvedSource {
                source_id: SourceId::generate(),
                kind,
                canonical_ref: canonical_ref.into(),
                input,
            },
        },
        output_declarations: vec![
            declaration(
                ArtifactKind::SourceOriginal,
                "videos",
                "output/videos/*",
                true,
                true,
            ),
            declaration(
                ArtifactKind::Subtitle,
                "subtitle",
                "output/subtitle.srt",
                false,
                false,
            ),
            declaration(
                ArtifactKind::Danmaku,
                "danmaku",
                "output/danmaku.xml",
                false,
                false,
            ),
            declaration(
                ArtifactKind::PartsManifest,
                "parts",
                "output/parts.json",
                true,
                false,
            ),
            declaration(
                ArtifactKind::TaskLog,
                "log",
                "logs/task.ndjson",
                true,
                false,
            ),
        ],
        model: None,
        effort: None,
        runner_config_revision: 1,
        secret_inputs: SecretInputs::default(),
    }
}

#[test]
fn accepts_only_strict_single_video_claims() {
    for (kind, reference) in [
        (
            SourceKind::LocalVideo,
            "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        (SourceKind::YoutubeVideo, "youtube:dQw4w9WgXcQ"),
        (SourceKind::BilibiliVideo, "bilibili:BV1GJ411x7h7"),
    ] {
        assert_eq!(
            claim::validate(&make_claim(kind, reference))
                .expect("valid")
                .kind,
            kind
        );
    }
    for (kind, reference) in [
        (SourceKind::YoutubeVideo, "youtube:dQw4w9WgXcQ?list=bad"),
        (SourceKind::BilibiliVideo, "bilibili:av123"),
        (SourceKind::YoutubeChannel, "youtube:dQw4w9WgXcQ"),
    ] {
        assert!(claim::validate(&make_claim(kind, reference)).is_err());
    }
}

#[test]
fn credential_and_artifact_drift_fail_closed() {
    let mut value = make_claim(SourceKind::YoutubeVideo, "youtube:dQw4w9WgXcQ");
    value.secret_inputs.credential = Some(SecretCredential {
        kind: CredentialKind::BilibiliCookie,
        value: "REDACTED".into(),
    });
    assert!(claim::validate(&value).is_err());
    value.output_declarations[0].path = "output/escape/*".into();
    assert!(claim::validate(&value).is_err());
}

fn text(values: &[OsString]) -> Vec<&str> {
    values
        .iter()
        .map(|value| value.to_str().expect("utf8 argument"))
        .collect()
}

#[test]
fn youtube_uses_only_its_explicit_proxy_and_cookie_path() {
    let proxy = reqwest::Url::parse("http://foreign-proxy.internal:1080").expect("proxy");
    let invocation = claim::youtube(
        "youtube:dQw4w9WgXcQ",
        Path::new("/work/output"),
        &proxy,
        Some(Path::new("/work/private/cookie")),
        Path::new("/work/private"),
    )
    .expect("invocation");
    let arguments = text(&invocation.arguments);
    assert!(
        arguments
            .windows(2)
            .any(|pair| pair == ["--proxy", proxy.as_str()])
    );
    assert!(
        arguments
            .windows(2)
            .any(|pair| pair == ["--cookies", "/work/private/cookie"])
    );
    assert!(
        arguments
            .windows(2)
            .any(|pair| pair == ["--sub-langs", "zh-Hans,zh-Hant,zh,en"])
    );
    assert!(!arguments.contains(&"--write-auto-subs"));
    assert!(!arguments.contains(&"--max-downloads"));
    assert_eq!(
        arguments.last(),
        Some(&"https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    );
    let environment = invocation
        .environment
        .iter()
        .map(|(name, _)| name.to_string_lossy())
        .collect::<Vec<_>>();
    for forbidden in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"] {
        assert!(!environment.iter().any(|name| name == forbidden));
    }
}

#[test]
fn bilibili_forces_proxy_off_and_keeps_cookie_in_private_path() {
    let invocation = claim::bilibili(
        "bilibili:BV1GJ411x7h7",
        Path::new("/work/output"),
        Path::new("/work/temporary"),
        Some(Path::new("/work/private/cookie")),
        Path::new("/work/private"),
    )
    .expect("invocation");
    let arguments = text(&invocation.arguments);
    assert!(arguments.windows(2).any(|pair| pair == ["--proxy", "no"]));
    assert!(
        arguments
            .windows(2)
            .any(|pair| pair == ["--auth-file", "/work/private/cookie"])
    );
    assert_eq!(
        arguments.last(),
        Some(&"https://www.bilibili.com/video/BV1GJ411x7h7")
    );
}

#[test]
fn credential_kinds_never_cross_platforms() {
    let youtube = SecretCredential {
        kind: CredentialKind::YoutubeCookie,
        value: "REDACTED".into(),
    };
    let bilibili = SecretCredential {
        kind: CredentialKind::BilibiliCookie,
        value: "REDACTED".into(),
    };
    assert!(claim::credential_matches(
        SourceKind::YoutubeVideo,
        &youtube
    ));
    assert!(claim::credential_matches(
        SourceKind::BilibiliVideo,
        &bilibili
    ));
    assert!(!claim::credential_matches(
        SourceKind::YoutubeVideo,
        &bilibili
    ));
    assert!(!claim::credential_matches(
        SourceKind::BilibiliVideo,
        &youtube
    ));
}
