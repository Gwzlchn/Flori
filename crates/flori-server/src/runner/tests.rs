use super::*;

#[test]
fn token_digest_is_canonical_and_stable() {
    let Ok(digest) = token_digest("secret") else {
        panic!("valid digest");
    };
    assert_eq!(
        digest.as_str(),
        "2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b"
    );
}

#[test]
fn remote_download_base_requires_https() {
    assert!(valid_download_base("https://flori.example/api/artifacts"));
    assert!(valid_download_base("http://localhost/artifacts"));
    assert!(!valid_download_base("http://flori.example/artifacts"));
    assert!(!valid_download_base("https://flori.example/artifacts/"));
}

#[test]
fn ndjson_requires_content_type_final_newline_and_strict_frames() {
    let mut headers = HeaderMap::new();
    headers.insert(
        CONTENT_TYPE,
        "application/x-ndjson".parse().expect("header"),
    );
    let line = concat!(
        r#"{"sequence":1,"sha256":"2bb80d537b1da3e38bd30361aa855686bde0eacd7162fef6a25fe97bf527a25b","line":"secret"}"#,
        "\n"
    );
    assert_eq!(
        parse_ndjson(&headers, line.as_bytes()).map_or(0, |v| v.len()),
        1
    );
    assert!(parse_ndjson(&headers, line.trim_end().as_bytes()).is_err());
    assert!(parse_ndjson(&HeaderMap::new(), line.as_bytes()).is_err());
    assert!(parse_ndjson(&headers, line.replace('}', ",\"extra\":1}").as_bytes()).is_err());
}
