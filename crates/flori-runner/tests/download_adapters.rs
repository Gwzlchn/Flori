#[path = "../src/download/adapters.rs"]
mod adapters;

use std::{ffi::OsString, path::Path};

use flori_core::{CredentialKind, SecretCredential, SourceKind};

fn text(values: &[OsString]) -> Vec<&str> {
    values
        .iter()
        .map(|value| value.to_str().expect("utf8 argument"))
        .collect()
}

#[test]
fn youtube_uses_only_its_explicit_proxy_and_cookie_path() {
    let proxy = reqwest::Url::parse("http://foreign-proxy.internal:1080").expect("proxy");
    let invocation = adapters::youtube(
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
    assert_eq!(
        arguments.last(),
        Some(&"https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    );
    assert!(!arguments.iter().any(|value| value.contains("REDACTED")));
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
fn bilibili_forces_proxy_off_and_never_places_cookie_in_arguments() {
    let invocation = adapters::bilibili(
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
    assert!(!arguments.iter().any(|value| value.contains("REDACTED")));
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
    assert!(adapters::credential_matches(
        SourceKind::YoutubeVideo,
        &youtube
    ));
    assert!(adapters::credential_matches(
        SourceKind::BilibiliVideo,
        &bilibili
    ));
    assert!(!adapters::credential_matches(
        SourceKind::YoutubeVideo,
        &bilibili
    ));
    assert!(!adapters::credential_matches(
        SourceKind::BilibiliVideo,
        &youtube
    ));
}
