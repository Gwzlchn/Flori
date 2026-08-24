#[path = "video_product/mod.rs"]
mod video_product;

#[tokio::test]
async fn local_video_reaches_current_fts_and_time_evidence() {
    video_product::run(video_product::Case::Local, video_product::Mode::Fake).await;
}

#[tokio::test]
async fn youtube_native_subtitle_skips_whisper() {
    video_product::run(video_product::Case::Youtube, video_product::Mode::Fake).await;
}

#[tokio::test]
async fn bilibili_danmaku_uses_whisper_once() {
    video_product::run(video_product::Case::Bilibili, video_product::Mode::Fake).await;
}

#[tokio::test]
async fn external_platform_videos_reach_current_when_explicitly_enabled() {
    if std::env::var_os("FLORI_VIDEO_EXTERNAL").as_deref() != Some(std::ffi::OsStr::new("1")) {
        return;
    }
    for case in [video_product::Case::Youtube, video_product::Case::Bilibili] {
        video_product::run(case, video_product::Mode::External).await;
    }
}
