//! video-frames: keyframes by scene cuts and similarity dedup, at most two per 30s section,
//! sections and transcript.md.

use std::{collections::BTreeMap, fs, path::Path, process::Stdio};

use tokio::process::Command;

use super::{Segment, Transcript, exit_failure, push_text, run_checked};
use crate::pipeline::{
    Manifest, SECTION_MS, Section, StepFailure, StepName, StepResult, clock, escape_markdown,
    read_json, step_file, write_json, write_manifest,
};

pub(super) const THUMB_SIDE: usize = 32;
/// Decode once at low resolution. select keeps the first frame, scene cuts and one frame per
/// 15s without a cut; each selected frame leaves a showinfo line and a 32x32 gray thumbnail.
const SCAN_FILTER: &str = concat!(
    "scale=160:90,",
    "select='isnan(prev_selected_t)+gt(scene,0.3)+gte(t-prev_selected_t,15)',",
    "showinfo,scale=32:32,format=gray",
);
/// Bursts of cuts (fades, flashes) keep only their first usable frame.
const MIN_GAP_MS: u64 = 2_000;
/// Frames follow the timeline like master's transcript did: a long video gets more of them, a
/// section never more than this.
pub(super) const FRAMES_PER_SECTION: usize = 2;
/// Near-uniform frames (black, white, fades) carry nothing.
const FLAT_VARIANCE: f64 = 25.0;
pub(super) const PHASH_DUPLICATE: u32 = 6;
pub(super) const PHASH_SIMILAR: u32 = 10;
pub(super) const SSIM_DUPLICATE: f64 = 0.92;

pub async fn frames(job_dir: &Path, out_dir: &Path) -> StepResult {
    let video = step_file(job_dir, StepName::VideoFetch, "video")?;
    let transcript_json = step_file(job_dir, StepName::VideoTranscribe, "transcript_json")?;
    let transcript: Transcript = read_json(&transcript_json)?;

    let candidates = scan(&video).await?;
    let assets = out_dir.join("assets");
    fs::create_dir(&assets)?;
    let mut frames = Vec::new();
    for ms in pick_keyframes(&candidates) {
        let name = format!("frame-{ms}.jpg");
        let path = assets.join(&name);
        let mut ffmpeg = Command::new("ffmpeg");
        ffmpeg
            .args(["-hide_banner", "-nostdin", "-loglevel", "error", "-ss"])
            .arg(format!("{}.{:03}", ms / 1000, ms % 1000))
            .arg("-i")
            .arg(&video)
            .args(["-frames:v", "1", "-q:v", "3", "-y", "-vf"])
            .arg("scale='min(1280,iw)':-2")
            .arg(&path);
        run_checked(ffmpeg, "ffmpeg").await?;
        if fs::metadata(&path).is_ok_and(|m| m.len() > 0) {
            frames.push((ms, name));
        } else {
            eprintln!("video-frames: no frame decoded at {ms} ms, skipped");
            let _ = fs::remove_file(&path);
        }
    }

    let sections = build_sections(&transcript.segments, &frames);
    write_json(&out_dir.join("sections.json"), &sections)?;
    fs::write(out_dir.join("transcript.md"), render_transcript(&sections))?;
    let manifest = Manifest {
        title: None,
        files: BTreeMap::from([
            ("transcript".into(), "transcript.md".into()),
            ("sections".into(), "sections.json".into()),
        ]),
        assets: frames.into_iter().map(|(_, name)| name).collect(),
        meta: BTreeMap::new(),
    };
    write_manifest(out_dir, &manifest)?;
    Ok(())
}

/// Candidate frames as (timestamp ms, 32x32 gray pixels).
async fn scan(video: &Path) -> Result<Vec<(u64, Vec<u8>)>, StepFailure> {
    let output = Command::new("ffmpeg")
        .args(["-hide_banner", "-nostdin", "-nostats"])
        .args(["-loglevel", "info", "-i"])
        .arg(video)
        .args(["-an", "-sn", "-dn", "-vf", SCAN_FILTER])
        .args(["-fps_mode", "passthrough", "-f", "rawvideo", "pipe:1"])
        .stdin(Stdio::null())
        .output()
        .await
        .map_err(|e| StepFailure::permanent(format!("ffmpeg: {e}")))?;
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        let lines: Vec<&str> = stderr.lines().collect();
        eprintln!("{}", lines[lines.len().saturating_sub(5)..].join("\n"));
        return Err(exit_failure("ffmpeg scan", output.status));
    }
    let times = showinfo_times(&stderr);
    let thumbs = output.stdout.chunks_exact(THUMB_SIDE * THUMB_SIDE);
    if thumbs.len() != times.len() || !thumbs.remainder().is_empty() {
        return Err(StepFailure::permanent(format!(
            "ffmpeg scan returned {} bytes for {} frames",
            output.stdout.len(),
            times.len()
        )));
    }
    Ok(times.into_iter().zip(thumbs.map(<[u8]>::to_vec)).collect())
}

pub(super) fn showinfo_times(stderr: &str) -> Vec<u64> {
    stderr
        .lines()
        .filter(|line| line.contains("Parsed_showinfo"))
        .filter_map(|line| {
            let value = line
                .split_ascii_whitespace()
                .find_map(|field| field.strip_prefix("pts_time:"))?;
            let seconds = value.parse::<f64>().ok().filter(|s| s.is_finite())?;
            Some((seconds.max(0.0) * 1000.0).round() as u64)
        })
        .collect()
}

/// A frame that passed the filters: time, perceptual hash, thumbnail.
type Kept<'a> = (u64, u64, &'a [u8]);

/// Flat filter, gap to the last frame, then dedup against every frame kept so far: a close
/// perceptual hash is a duplicate outright, a moderately close one only when SSIM agrees.
/// Each 30s section then keeps at most [`FRAMES_PER_SECTION`] of its frames, spread over it,
/// before the next section is judged, so a slide thinned out here can still return later.
/// Dropped candidates do not open a gap window, so a black first frame cannot hide the title
/// card a second later. Candidates come in time order.
pub(super) fn pick_keyframes(candidates: &[(u64, Vec<u8>)]) -> Vec<u64> {
    let mut kept: Vec<Kept> = Vec::new();
    let mut section: Vec<Kept> = Vec::new();
    for (ms, pixels) in candidates {
        if section
            .first()
            .is_some_and(|(first, _, _)| first / SECTION_MS != ms / SECTION_MS)
        {
            kept.extend(evenly_bounded(&section, FRAMES_PER_SECTION));
            section.clear();
        }
        if variance(pixels) < FLAT_VARIANCE {
            continue;
        }
        let last = section.last().or(kept.last());
        if last.is_some_and(|(last, _, _)| ms.saturating_sub(*last) < MIN_GAP_MS) {
            continue;
        }
        let Some(hash) = perceptual_hash(pixels) else {
            continue;
        };
        let duplicate = kept.iter().chain(&section).any(|(_, seen_hash, seen)| {
            let distance = (hash ^ seen_hash).count_ones();
            distance <= PHASH_DUPLICATE
                || (distance <= PHASH_SIMILAR && ssim(pixels, seen) >= SSIM_DUPLICATE)
        });
        if !duplicate {
            section.push((*ms, hash, pixels));
        }
    }
    kept.extend(evenly_bounded(&section, FRAMES_PER_SECTION));
    kept.into_iter().map(|(ms, _, _)| ms).collect()
}

/// At most `limit` items spread over the whole list, keeping both ends. `limit` is at least 2.
pub(super) fn evenly_bounded<T: Copy>(items: &[T], limit: usize) -> Vec<T> {
    if items.len() <= limit {
        return items.to_vec();
    }
    (0..limit)
        .map(|i| items[i * (items.len() - 1) / (limit - 1)])
        .collect()
}

fn mean(pixels: &[u8]) -> f64 {
    pixels.iter().map(|&p| f64::from(p)).sum::<f64>() / pixels.len().max(1) as f64
}

fn variance(pixels: &[u8]) -> f64 {
    let m = mean(pixels);
    let squares = pixels.iter().map(|&p| (f64::from(p) - m).powi(2));
    squares.sum::<f64>() / pixels.len().max(1) as f64
}

/// Global SSIM over two equally sized gray images.
pub(super) fn ssim(left: &[u8], right: &[u8]) -> f64 {
    let (mean_l, mean_r) = (mean(left), mean(right));
    let (mut var_l, mut var_r, mut cov) = (0.0, 0.0, 0.0);
    for (&l, &r) in left.iter().zip(right) {
        let (l, r) = (f64::from(l) - mean_l, f64::from(r) - mean_r);
        var_l += l * l;
        var_r += r * r;
        cov += l * r;
    }
    let n = (left.len().min(right.len()) as f64 - 1.0).max(1.0);
    let (var_l, var_r, cov) = (var_l / n, var_r / n, cov / n);
    let c1 = (0.01_f64 * 255.0).powi(2);
    let c2 = (0.03_f64 * 255.0).powi(2);
    ((2.0 * mean_l * mean_r + c1) * (2.0 * cov + c2))
        / ((mean_l.powi(2) + mean_r.powi(2) + c1) * (var_l + var_r + c2))
}

/// 64-bit DCT hash of a 32x32 gray image: low 8x8 coefficients against their median.
pub(super) fn perceptual_hash(pixels: &[u8]) -> Option<u64> {
    const HASH: usize = 8;
    if pixels.len() != THUMB_SIDE * THUMB_SIDE {
        return None;
    }
    let basis = |k: usize, x: usize| {
        (((2 * x + 1) * k) as f64 * std::f64::consts::PI / (2 * THUMB_SIDE) as f64).cos()
    };
    let mut coefficients = [0.0; HASH * HASH];
    for v in 0..HASH {
        for u in 0..HASH {
            let mut sum = 0.0;
            for y in 0..THUMB_SIDE {
                for x in 0..THUMB_SIDE {
                    sum += f64::from(pixels[y * THUMB_SIDE + x]) * basis(u, x) * basis(v, y);
                }
            }
            coefficients[v * HASH + u] = sum;
        }
    }
    let mut ac = coefficients[1..].to_vec();
    ac.sort_by(f64::total_cmp);
    let median = ac[ac.len() / 2];
    Some(
        coefficients
            .iter()
            .enumerate()
            .fold(0, |hash, (i, &c)| hash | (u64::from(c > median) << i)),
    )
}

/// 30s sections that have text or frames. A segment belongs to the section of its start.
pub(super) fn build_sections(segments: &[Segment], frames: &[(u64, String)]) -> Vec<Section> {
    let mut sections: BTreeMap<u64, Section> = BTreeMap::new();
    fn section(sections: &mut BTreeMap<u64, Section>, ms: u64) -> &mut Section {
        let start_ms = ms / SECTION_MS * SECTION_MS;
        sections.entry(start_ms).or_insert_with(|| Section {
            start_ms,
            frames: Vec::new(),
            text: String::new(),
        })
    }
    for segment in segments {
        push_text(
            &mut section(&mut sections, segment.start_ms).text,
            &segment.text,
        );
    }
    for (ms, name) in frames {
        section(&mut sections, *ms).frames.push(name.clone());
    }
    sections.into_values().collect()
}

/// `## mm:ss` per section, then its keyframes, then its text. translate writes a Chinese video's
/// punctuated transcript with it too, so both layouts stay one.
pub(crate) fn render_transcript(sections: &[Section]) -> String {
    let mut md = String::new();
    for section in sections {
        md.push_str(&format!("## {}\n\n", clock(section.start_ms)));
        for frame in &section.frames {
            md.push_str(&format!("![](assets/{frame})\n\n"));
        }
        if !section.text.is_empty() {
            md.push_str(&escape_markdown(&section.text));
            md.push_str("\n\n");
        }
    }
    md.truncate(md.trim_end().len());
    md.push('\n');
    md
}
