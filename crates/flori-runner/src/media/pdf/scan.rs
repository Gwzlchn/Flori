use std::ffi::OsString;
use std::path::Path;
use std::time::Duration;

use flori_core::ErrorCode;

use super::process::run_bounded;

pub(super) async fn require_digital_pdf(
    pdfinfo: &Path,
    pdftotext: &Path,
    input: &Path,
    timeout: Duration,
    max_text_bytes: usize,
) -> Result<u32, ErrorCode> {
    let input_arg = input.as_os_str().to_owned();
    let info = run_bounded(
        pdfinfo,
        std::slice::from_ref(&input_arg),
        timeout,
        64 * 1024,
    )
    .await?;
    if !info.stderr.is_empty() && info.stdout.is_empty() {
        return Err(ErrorCode::ExecutorFailed);
    }
    let pages = parse_page_count(&info.stdout)?;
    let arguments = [
        OsString::from("-enc"),
        OsString::from("UTF-8"),
        input_arg,
        OsString::from("-"),
    ];
    let text = run_bounded(pdftotext, &arguments, timeout, max_text_bytes).await?;
    let text = std::str::from_utf8(&text.stdout).map_err(|_| ErrorCode::ExecutorFailed)?;
    let mut page_text = text.split('\u{000c}').collect::<Vec<_>>();
    if page_text.last() == Some(&"") {
        page_text.pop();
    }
    if page_text.len() != usize::try_from(pages).map_err(|_| ErrorCode::ExecutorFailed)? {
        return Err(ErrorCode::ExecutorFailed);
    }
    if page_text.iter().all(|page| {
        page.chars()
            .filter(|character| !character.is_whitespace())
            .count()
            < 32
    }) {
        return Err(ErrorCode::UnsupportedScannedPdf);
    }
    Ok(pages)
}

fn parse_page_count(output: &[u8]) -> Result<u32, ErrorCode> {
    let output = std::str::from_utf8(output).map_err(|_| ErrorCode::ExecutorFailed)?;
    let mut values = output.lines().filter_map(|line| {
        line.strip_prefix("Pages:")
            .and_then(|value| value.trim().parse::<u32>().ok())
    });
    let pages = values.next().filter(|pages| *pages > 0);
    if pages.is_none() || values.next().is_some() {
        Err(ErrorCode::ExecutorFailed)
    } else {
        pages.ok_or(ErrorCode::ExecutorFailed)
    }
}

#[cfg(test)]
mod tests {
    use std::os::unix::fs::PermissionsExt;

    use flori_core::ArtifactId;

    use super::*;

    fn script(root: &Path, name: &str, body: &str) -> std::path::PathBuf {
        let path = root.join(name);
        std::fs::write(&path, format!("#!/bin/sh\n{body}\n")).expect("write fake tool");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700))
            .expect("make fake tool executable");
        path
    }

    #[tokio::test]
    async fn fake_tools_apply_the_per_page_scan_rule() {
        let root = std::env::temp_dir().join(format!("flori-scan-{}", ArtifactId::generate()));
        std::fs::create_dir(&root).expect("create test directory");
        let info = script(&root, "pdfinfo", "printf 'Pages: 2\\n'");
        let scanned = script(&root, "scanned", "printf 'short\\014tiny\\014'");
        let digital = script(
            &root,
            "digital",
            "printf 'short\\014this-page-has-more-than-thirty-two-visible-characters\\014'",
        );
        let input = root.join("input.pdf");
        std::fs::write(&input, b"%PDF-test").expect("write input");

        assert_eq!(
            require_digital_pdf(&info, &scanned, &input, Duration::from_secs(5), 4096).await,
            Err(ErrorCode::UnsupportedScannedPdf)
        );
        assert_eq!(
            require_digital_pdf(&info, &digital, &input, Duration::from_secs(5), 4096).await,
            Ok(2)
        );
        std::fs::remove_dir_all(root).expect("remove test directory");
    }
}
