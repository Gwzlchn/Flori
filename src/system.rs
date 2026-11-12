//! `GET /api/system`: a read-only picture of the running instance. Every field is copied by
//! name into `SystemInfo`; `Config` itself is never serialized, and secrets appear only as
//! "configured or not".

use axum::{Json, extract::State};
use time::{Date, Duration, OffsetDateTime, macros::offset};

use crate::{
    api::{SystemInfo, WeekUse},
    db,
    http::{AppState, Error},
    pipeline::AiSpec,
};

/// The usage gate in CLAUDE.md: submissions a week has to reach.
const WEEKLY_GOAL: u32 = 3;
const WEEKS: i64 = 4;

/// Set by `just image`; other builds do not know their commit.
const COMMIT: &str = match option_env!("FLORI_BUILD_COMMIT") {
    Some(commit) => commit,
    None => "unknown",
};

#[utoipa::path(get, path = "/api/system", responses((status = 200, body = SystemInfo)))]
pub async fn get_system(State(s): State<AppState>) -> Result<Json<SystemInfo>, Error> {
    let config = &s.config;
    let today = OffsetDateTime::now_utc().to_offset(offset!(+8)).date();
    let first = week_start(today) - Duration::weeks(WEEKS - 1);
    let days = db::submissions_since(&s.db, &first.to_string()).await?;
    let (domains, collections, notes) = db::counts(&s.db).await?;
    let tools = s.exec.tools();
    // Sizes of the filesystem under the data directory; a failed statvfs only blanks them.
    let disk = rustix::fs::statvfs(&config.data_dir).ok();
    Ok(Json(SystemInfo {
        version: env!("CARGO_PKG_VERSION").to_owned(),
        commit: COMMIT.to_owned(),
        ai: AiSpec {
            tool: config.ai_tool,
            model_note: config.ai_model_note.clone(),
            model_translate: config.ai_model_translate.clone(),
            cli_version: tools.cli_version.clone(),
        },
        bili_login: config.bili_sessdata.is_some(),
        proxy: config.proxy.is_some(),
        ytdlp_version: tools.ytdlp_version.clone(),
        whisper_model: config
            .whisper_model
            .file_name()
            .unwrap_or(config.whisper_model.as_os_str())
            .to_string_lossy()
            .into_owned(),
        data_dir: config.data_dir.display().to_string(),
        vault_name: config.vault_name.clone(),
        disk_free_bytes: disk.as_ref().map(|d| d.f_bavail.saturating_mul(d.f_frsize)),
        disk_total_bytes: disk.as_ref().map(|d| d.f_blocks.saturating_mul(d.f_frsize)),
        domains,
        collections,
        notes,
        weekly_goal: WEEKLY_GOAL,
        weeks: weeks(today, &days),
    }))
}

/// Monday of the week `day` is in.
fn week_start(day: Date) -> Date {
    day - Duration::days(day.weekday().number_days_from_monday().into())
}

/// The week of `today` and the three before it, newest first, from `(YYYY-MM-DD, jobs created
/// that day)` rows. Days are +08:00 dates, like every `created`.
fn weeks(today: Date, days: &[(String, u32)]) -> Vec<WeekUse> {
    (0..WEEKS)
        .map(|back| {
            let start = week_start(today) - Duration::weeks(back);
            let (from, to) = (start.to_string(), (start + Duration::weeks(1)).to_string());
            let submitted = days
                .iter()
                .filter(|(day, _)| *day >= from && *day < to)
                .map(|(_, n)| n)
                .sum();
            WeekUse {
                start: from,
                submitted,
                met: submitted >= WEEKLY_GOAL,
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use time::macros::date;

    use super::*;

    #[test]
    fn weeks_start_on_monday_and_count_their_own_days() {
        let days = [
            ("2026-10-18", 9), // Sunday before the window
            ("2026-10-19", 1), // Monday, first day of the oldest week
            ("2026-10-25", 2), // its Sunday
            ("2026-11-02", 2),
            ("2026-11-09", 1),
            ("2026-11-12", 3),
        ]
        .map(|(day, n)| (day.to_owned(), n));
        // A Thursday, then the Monday and Sunday of the same week.
        for today in [
            date!(2026 - 11 - 12),
            date!(2026 - 11 - 09),
            date!(2026 - 11 - 15),
        ] {
            let got: Vec<(String, u32, bool)> = weeks(today, &days)
                .into_iter()
                .map(|w| (w.start, w.submitted, w.met))
                .collect();
            let want = [
                ("2026-11-09", 4, true),
                ("2026-11-02", 2, false),
                ("2026-10-26", 0, false),
                ("2026-10-19", 3, true),
            ]
            .map(|(start, n, met)| (start.to_owned(), n, met));
            assert_eq!(got, want, "{today}");
        }
    }
}
