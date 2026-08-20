#![forbid(unsafe_code)]

#[path = "common/ai.rs"]
mod runner;
#[allow(dead_code)]
#[path = "../runtime_config.rs"]
mod runtime_config;

#[tokio::main]
async fn main() -> std::process::ExitCode {
    runner::run(flori_core::AiTool::QoderCli, "/usr/local/bin/qodercli").await
}
