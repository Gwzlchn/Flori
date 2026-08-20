#[path = "pdf_product/mod.rs"]
mod pdf_product;

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
