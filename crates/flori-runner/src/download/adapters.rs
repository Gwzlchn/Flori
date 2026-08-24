use std::{ffi::OsString, path::Path};

use flori_core::{CredentialKind, ErrorCode, SecretCredential, SourceKind};

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
    let url = format!("https://www.youtube.com/watch?v={id}");
    let template = output.join("download.%(ext)s");
    let mut arguments = strings(&[
        "--ignore-config",
        "--no-plugin-dirs",
        "--no-remote-components",
        "--no-playlist",
        "--max-downloads",
        "1",
        "--no-progress",
        "--no-color",
        "--no-write-info-json",
        "--no-write-thumbnail",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "zh-Hans,zh-Hant,zh.*,en.*,en",
        "--convert-subs",
        "srt",
        "--merge-output-format",
        "mp4",
        "--format",
        "bv*+ba/b",
        "--output",
    ]);
    arguments.push(template.into_os_string());
    arguments.extend(strings(&["--proxy", proxy.as_str()]));
    if let Some(cookie) = cookie {
        arguments.push("--cookies".into());
        arguments.push(cookie.as_os_str().to_owned());
    }
    arguments.push(url.into());
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
