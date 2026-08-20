#[path = "pdf_product/mod.rs"]
mod pdf_product;

use std::path::PathBuf;

#[tokio::test]
async fn uploaded_pdf_reaches_current_search_and_evidence_with_real_media_image() {
    let image = match std::env::var("FLORI_RUNNER_MEDIA_IMAGE") {
        Ok(image) => image,
        Err(std::env::VarError::NotPresent) => {
            eprintln!("FLORI_RUNNER_MEDIA_IMAGE is unset; real media-image acceptance skipped");
            return;
        }
        Err(error) => panic!("FLORI_RUNNER_MEDIA_IMAGE is not valid UTF-8: {error}"),
    };
    assert!(
        !image.is_empty() && image.bytes().all(|byte| !byte.is_ascii_whitespace()),
        "FLORI_RUNNER_MEDIA_IMAGE must be one non-empty Docker image reference"
    );
    pdf_product::run(&image).await;
}

#[tokio::test]
async fn uploaded_pdf_reaches_current_with_real_qoder_when_explicitly_authorized() {
    let root = match std::env::var("FLORI_REAL_QODER_ROOT") {
        Ok(root) => root,
        Err(std::env::VarError::NotPresent) => {
            eprintln!("FLORI_REAL_QODER_ROOT is unset; paid real-Qoder acceptance skipped");
            return;
        }
        Err(error) => panic!("FLORI_REAL_QODER_ROOT is not valid UTF-8: {error}"),
    };
    let file = |name: &str| {
        let path = PathBuf::from(required(name));
        assert!(
            path.is_absolute() && path.is_file(),
            "{name} must be an absolute file"
        );
        path
    };
    let directory = |name: &str| {
        let path = PathBuf::from(required(name));
        assert!(
            path.is_absolute() && path.is_dir(),
            "{name} must be an absolute directory"
        );
        path
    };
    pdf_product::run_real(pdf_product::RealConfig {
        image: required("FLORI_RUNNER_MEDIA_IMAGE"),
        root: PathBuf::from(root),
        pdf: file("FLORI_REAL_QODER_PDF"),
        executable: file("FLORI_REAL_QODER_EXECUTABLE"),
        config_home: directory("FLORI_REAL_QODER_CONFIG_HOME"),
        model: required("FLORI_REAL_QODER_MODEL"),
        effort: required("FLORI_REAL_QODER_EFFORT"),
    })
    .await;
}

fn required(name: &str) -> String {
    let value = std::env::var(name).unwrap_or_else(|error| panic!("{name} is required: {error}"));
    assert!(
        !value.is_empty() && !value.bytes().any(|byte| byte.is_ascii_control()),
        "{name} must be non-empty and contain no control characters"
    );
    value
}
