//! Home Core 进程入口。

#![forbid(unsafe_code)]

use std::env;
use std::fs;
use std::path::Path;
use std::process::ExitCode;

fn main() -> ExitCode {
    let mut args = env::args_os().skip(1);
    let Some(command) = args.next() else {
        eprintln!("usage: flori-server export-openapi <output>");
        return ExitCode::from(2);
    };
    let Some(output) = args.next() else {
        eprintln!("usage: flori-server export-openapi <output>");
        return ExitCode::from(2);
    };
    if command != "export-openapi" || args.next().is_some() {
        eprintln!("usage: flori-server export-openapi <output>");
        return ExitCode::from(2);
    }

    match export_openapi(Path::new(&output)) {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("failed to export OpenAPI: {error}");
            ExitCode::FAILURE
        }
    }
}

fn export_openapi(output: &Path) -> Result<(), Box<dyn std::error::Error>> {
    if let Some(parent) = output.parent() {
        fs::create_dir_all(parent)?;
    }
    fs::write(output, flori_core::openapi_json()?)?;
    Ok(())
}
