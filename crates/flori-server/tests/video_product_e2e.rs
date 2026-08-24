#[path = "video_product/mod.rs"]
mod video_product;

#[tokio::test]
async fn local_video_reaches_current_fts_and_time_evidence() {
    video_product::run(video_product::Case::Local).await;
}

#[tokio::test]
async fn youtube_native_subtitle_skips_whisper() {
    video_product::run(video_product::Case::Youtube).await;
}

#[tokio::test]
async fn bilibili_danmaku_uses_whisper_once() {
    video_product::run(video_product::Case::Bilibili).await;
}
