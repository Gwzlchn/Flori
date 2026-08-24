#[path = "video_product/mod.rs"]
mod video_product;

#[tokio::test]
async fn local_video_reaches_current_fts_and_time_evidence() {
    video_product::run().await;
}
