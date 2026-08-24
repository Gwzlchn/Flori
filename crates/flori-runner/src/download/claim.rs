use std::{ffi::OsString, path::Path};

use flori_core::{
    ArtifactDeclaration, ArtifactKind, ArtifactWhen, CredentialKind, ErrorCode, Executor,
    ResolvedSource, ResolvedTaskInputs, SecretCredential, SourceKind, TaskClaim,
};

pub(super) struct Invocation {
    pub(super) arguments: Vec<OsString>,
    pub(super) environment: Vec<(OsString, OsString)>,
}

pub(super) fn youtube(
    canonical_ref: &str,
    output: &Path,
    proxy: &reqwest::Url,
    cookie: Option<&Path>,
    home: &Path,
) -> Result<Invocation, ErrorCode> {
    let id = canonical_ref
        .strip_prefix("youtube:")
        .ok_or(ErrorCode::CorruptState)?;
    let mut arguments = strings(&[
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-playlist",
        "--no-progress",
        "--no-color",
        "--no-write-info-json",
        "--no-write-thumbnail",
        "--write-subs",
        "--sub-langs",
        "zh-Hans,zh-Hant,zh,en",
        "--convert-subs",
        "srt",
        "--merge-output-format",
        "mp4",
        "--format",
        "bv*+ba/b",
        "--output",
    ]);
    arguments.push(output.join("download.%(ext)s").into_os_string());
    arguments.extend(strings(&["--proxy", proxy.as_str()]));
    if let Some(cookie) = cookie {
        arguments.push("--cookies".into());
        arguments.push(cookie.as_os_str().to_owned());
    }
    arguments.push(format!("https://www.youtube.com/watch?v={id}").into());
    Ok(Invocation {
        arguments,
        environment: base_environment(home),
    })
}

pub(super) fn bilibili(
    canonical_ref: &str,
    output: &Path,
    temporary: &Path,
    cookie: Option<&Path>,
    home: &Path,
) -> Result<Invocation, ErrorCode> {
    let id = canonical_ref
        .strip_prefix("bilibili:")
        .ok_or(ErrorCode::CorruptState)?;
    let mut arguments = strings(&[
        "--proxy",
        "no",
        "--no-progress",
        "--no-color",
        "--no-cover",
        "--no-chapter-info",
        "--output-format",
        "mp4",
        "--danmaku-format",
        "xml",
        "--subpath-template",
        "part-1",
        "--dir",
    ]);
    arguments.push(output.as_os_str().to_owned());
    arguments.push("--tmp-dir".into());
    arguments.push(temporary.as_os_str().to_owned());
    if let Some(cookie) = cookie {
        arguments.push("--auth-file".into());
        arguments.push(cookie.as_os_str().to_owned());
    }
    arguments.push(format!("https://www.bilibili.com/video/{id}").into());
    Ok(Invocation {
        arguments,
        environment: base_environment(home),
    })
}

pub(super) fn credential_matches(source: SourceKind, credential: &SecretCredential) -> bool {
    matches!(
        (source, credential.kind),
        (SourceKind::YoutubeVideo, CredentialKind::YoutubeCookie)
            | (SourceKind::BilibiliVideo, CredentialKind::BilibiliCookie)
    )
}

fn strings(values: &[&str]) -> Vec<OsString> {
    values.iter().map(OsString::from).collect()
}

fn base_environment(home: &Path) -> Vec<(OsString, OsString)> {
    vec![
        ("HOME".into(), home.as_os_str().to_owned()),
        ("LANG".into(), "C.UTF-8".into()),
        ("PATH".into(), "/usr/local/bin:/usr/bin:/bin".into()),
        ("PYTHONDONTWRITEBYTECODE".into(), "1".into()),
        ("PYTHONHASHSEED".into(), "0".into()),
    ]
}

pub(super) fn validate(claim: &TaskClaim) -> Result<&ResolvedSource, ErrorCode> {
    if claim.executor != Executor::VideoAcquire
        || claim.timeout_ms == 0
        || claim.attempt_no == 0
        || claim.task_key.is_empty()
        || claim.model.is_some()
        || claim.effort.is_some()
    {
        return Err(ErrorCode::CorruptState);
    }
    let ResolvedTaskInputs::VideoAcquire { source } = &claim.resolved_inputs else {
        return Err(ErrorCode::CorruptState);
    };
    validate_source(claim, source)?;
    shape(claim)?;
    Ok(source)
}

fn validate_source(claim: &TaskClaim, source: &ResolvedSource) -> Result<(), ErrorCode> {
    let credential = claim.secret_inputs.credential.as_ref();
    match source.kind {
        SourceKind::LocalVideo
            if valid_upload(&source.canonical_ref)
                && source.input.as_ref().is_some_and(|input| {
                    input.name == "original" && input.media_type == "video/mp4"
                })
                && credential.is_none() =>
        {
            Ok(())
        }
        SourceKind::YoutubeVideo
            if valid_youtube(&source.canonical_ref)
                && source.input.is_none()
                && credential.is_none_or(|value| {
                    value.kind == CredentialKind::YoutubeCookie && valid_secret(&value.value)
                }) =>
        {
            Ok(())
        }
        SourceKind::BilibiliVideo
            if valid_bilibili(&source.canonical_ref)
                && source.input.is_none()
                && credential.is_none_or(|value| {
                    value.kind == CredentialKind::BilibiliCookie && valid_secret(&value.value)
                }) =>
        {
            Ok(())
        }
        SourceKind::BilibiliVideo | SourceKind::YoutubeVideo => {
            Err(ErrorCode::CredentialUnavailable)
        }
        _ => Err(ErrorCode::UnsupportedSource),
    }
}

fn valid_upload(value: &str) -> bool {
    value.strip_prefix("sha256:").is_some_and(|digest| {
        digest.len() == 64
            && digest
                .bytes()
                .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
    })
}

fn valid_secret(value: &str) -> bool {
    !value.trim().is_empty() && value.len() <= 1024 * 1024 && !value.contains('\0')
}

fn valid_youtube(value: &str) -> bool {
    value.strip_prefix("youtube:").is_some_and(|id| {
        id.len() == 11
            && id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    })
}

fn valid_bilibili(value: &str) -> bool {
    value.strip_prefix("bilibili:").is_some_and(|id| {
        id.len() == 12
            && id.starts_with("BV")
            && id.bytes().all(|byte| byte.is_ascii_alphanumeric())
    })
}

fn shape(claim: &TaskClaim) -> Result<(), ErrorCode> {
    let expected = [
        (
            ArtifactKind::SourceOriginal,
            "videos",
            "output/videos/*",
            true,
            true,
        ),
        (
            ArtifactKind::Subtitle,
            "subtitle",
            "output/subtitle.srt",
            false,
            false,
        ),
        (
            ArtifactKind::Danmaku,
            "danmaku",
            "output/danmaku.xml",
            false,
            false,
        ),
        (
            ArtifactKind::PartsManifest,
            "parts",
            "output/parts.json",
            true,
            false,
        ),
        (
            ArtifactKind::TaskLog,
            "log",
            "logs/task.ndjson",
            true,
            false,
        ),
    ];
    if claim.output_declarations.len() != expected.len() {
        return Err(ErrorCode::CorruptState);
    }
    for (kind, name, path, required, many) in expected {
        let declaration = exact(claim, kind)?;
        if declaration.name != name
            || declaration.path != path
            || declaration.required != required
            || declaration.when
                != if kind == ArtifactKind::TaskLog {
                    ArtifactWhen::Always
                } else {
                    ArtifactWhen::OnSuccess
                }
            || declaration.max_bytes == 0
            || declaration.max_files.is_some() != many
            || declaration.max_files == Some(0)
        {
            return Err(ErrorCode::CorruptState);
        }
    }
    Ok(())
}

pub(super) fn exact(
    claim: &TaskClaim,
    kind: ArtifactKind,
) -> Result<&ArtifactDeclaration, ErrorCode> {
    let mut values = claim
        .output_declarations
        .iter()
        .filter(|item| item.kind == kind);
    let value = values.next().ok_or(ErrorCode::CorruptState)?;
    values
        .next()
        .is_none()
        .then_some(value)
        .ok_or(ErrorCode::CorruptState)
}
