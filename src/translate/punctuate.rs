//! The pass translate runs on a Chinese video instead of a translation: the AI adds punctuation
//! and fixes homophones that speech recognition got wrong. Code aligns each reply with its
//! section and keeps only punctuation and replacements that sound the same; every other change
//! goes back to the transcribed text. Rules: contract 1 "translate" in docs/contracts.md.

mod diff;

use pinyin::ToPinyinMulti;

use super::is_han;
use crate::pipeline::Section;

pub(super) const PROMPT: &str = include_str!("../../prompts/punctuate.md");

/// A reply repeats every character of its chunk, while a Chinese translation is far shorter than
/// its English source, so chunks are half the size to stay well inside the per-call timeout.
pub(super) const CHUNK_CHARS: usize = 6_000;

/// Marks the AI may add, drop or replace: Chinese and ASCII sentence punctuation, quotes,
/// brackets, ellipses and dashes. Hyphen-minus, `~`, `/`, `%` and other symbols are content,
/// because they carry meaning in numbers and terms.
const PUNCTUATION: &[char] = &[
    ',', '.', ';', ':', '?', '!', '"', '\'', '(', ')', '[', ']', '{', '}', '，', '。', '、', '；',
    '：', '？', '！', '．', '“', '”', '‘', '’', '＂', '＇', '「', '」', '『', '』', '（', '）',
    '［', '］', '｛', '｝', '《', '》', '〈', '〉', '【', '】', '〔', '〕', '…', '⋯', '—', '–',
    '―', '·', '・',
];

/// Ideographs a reply may neither replace nor bring in, though another sounds the same, because
/// the swap changes what is said: numerals and other number words, as 十一 and 十亿 sound the
/// same; negations, as 不 and 部 do; and 买 and 卖, which differ only in tone.
const FIXED: &str = "〇零一二两三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟萬億兩半兆廿卅仨俩幺\
                     不没沒无無非未别別莫勿否毋甭买卖買賣";

/// One unit of what punctuation and whitespace may not change.
#[derive(Debug, Clone, Copy)]
struct Unit<'a> {
    /// The character, or a space for the break between two ASCII words.
    key: char,
    /// Its source text: the character, or the whitespace and punctuation that form the break.
    text: &'a str,
}

/// A text as units and the punctuation and whitespace around them: `gaps[i]` comes before
/// `units[i]` and the last gap after the last unit. The gaps next to a break are empty.
struct Units<'a> {
    units: Vec<Unit<'a>>,
    gaps: Vec<&'a str>,
}

/// Every character but whitespace and punctuation is a unit, and so is the break between two
/// ASCII letters or digits, which separates two words. An ASCII mark directly between two ASCII
/// letters or digits belongs to a number or term (3.5, 1,000, 10:30, Node.js, don't) and is a
/// unit. So is a `.` that opens a number (.5), unless it ends an ellipsis (...5); the break
/// before it separates two words as well.
fn units(text: &str) -> Units<'_> {
    let chars: Vec<(usize, char)> = text.char_indices().collect();
    let word = |i: usize| chars.get(i).is_some_and(|(_, c)| c.is_ascii_alphanumeric());
    let (mut units, mut gaps, mut end) = (Vec::new(), Vec::new(), 0);
    for (i, &(at, c)) in chars.iter().enumerate() {
        let inner = c.is_ascii() && i > 0 && word(i - 1) && word(i + 1);
        let decimal = c == '.'
            && chars.get(i + 1).is_some_and(|(_, n)| n.is_ascii_digit())
            && (i == 0 || chars[i - 1].1 != '.');
        if c.is_whitespace() || (PUNCTUATION.contains(&c) && !inner && !decimal) {
            continue;
        }
        let gap = &text[end..at];
        let opens_word = c.is_ascii_alphanumeric() || decimal;
        let after_word = units
            .last()
            .is_some_and(|u: &Unit| u.key.is_ascii_alphanumeric());
        if !gap.is_empty() && opens_word && after_word {
            gaps.extend(["", ""]);
            units.push(Unit {
                key: ' ',
                text: gap,
            });
        } else {
            gaps.push(gap);
        }
        end = at + c.len_utf8();
        units.push(Unit {
            key: c,
            text: &text[at..end],
        });
    }
    gaps.push(&text[end..]);
    Units { units, gaps }
}

fn keys(units: &[Unit]) -> Vec<char> {
    units.iter().map(|u| u.key).collect()
}

/// Two ideographs that share a reading, tones aside: any reading of a heteronym counts, and
/// initials and finals must match exactly (c is not ch, n is not l). [`FIXED`] never matches.
fn same_sound(a: char, b: char) -> bool {
    let readings = |c: char| {
        let multi = c
            .to_pinyin_multi()
            .filter(|_| is_han(c) && !FIXED.contains(c));
        multi.map(|m| m.into_iter().map(|p| p.plain()).collect::<Vec<_>>())
    };
    match (readings(a), readings(b)) {
        (Some(a), Some(b)) => a.iter().any(|r| b.contains(r)),
        _ => false,
    }
}

/// A section's text built from its reply.
#[derive(Debug, PartialEq)]
pub(super) struct Merged {
    pub text: String,
    /// Runs of ideographs the reply replaced with same-sounding ones, kept.
    pub corrected: usize,
    /// Other runs the reply inserted, deleted or replaced, put back as transcribed.
    pub reverted: usize,
}

/// The original's units with the reply's punctuation. None when the reply is no copy of this
/// section: it brings back fewer than half of the original's units in order, or they are fewer
/// than half of its own, as when the AI moved other sections' text into it. None as well when
/// the reply's own marks between kept units would change how a number reads.
///
/// Where the two differ, a run of the reply replaces the original's only when both are the same
/// length and every pair of units is two ideographs that sound the same; any other run goes back
/// to the original, a break the reply added with its marks. A gap between two units that stay
/// neighbours goes between them; one that touches a run put back goes to the end of that run, or
/// after the unit before it when that unit stays and the unit after it does not. A gap that
/// would change how a number or word reads is dropped, and one equal to the mark already there
/// is not repeated. So without punctuation and whitespace, the result is the original but for
/// the replacements kept. Whitespace runs collapse to one space: a line break or indent from the
/// AI would become Markdown structure, and video-frames' text has none either.
pub(super) fn merge(original: &str, reply: &str) -> Option<Merged> {
    let (ours, theirs) = (units(original), units(reply));
    let (a, b) = (keys(&ours.units), keys(&theirs.units));
    let (n, m) = (a.len(), b.len());
    // edits = n + m - 2 * kept, so a script with more than n.min(m) edits keeps fewer than half
    // of either side. The bound also caps the diff's memory, which grows with the square of the
    // edits, however long the reply.
    let hunks = diff::diff(&a, &b, n.min(m))?;
    // texts[i] renders unit i of the result; want is what its units must be.
    let mut texts: Vec<&str> = ours.units.iter().map(|u| u.text).collect();
    let mut want = a.clone();
    // place[j]: the unit of the result that unit j of the reply became, if it was not put back.
    let mut place: Vec<Option<usize>> = vec![None; m];
    let (mut corrected, mut reverted) = (0, 0);
    let end = diff::Hunk { a: n..n, b: m..m };
    let (mut i, mut j) = (0, 0);
    for hunk in hunks.iter().chain([&end]) {
        while i < hunk.a.start {
            place[j] = Some(i);
            // The reply's break carries the punctuation the AI put between two words.
            if a[i] == ' ' {
                texts[i] = theirs.units[j].text;
            }
            (i, j) = (i + 1, j + 1);
        }
        if hunk.a.is_empty() && hunk.b.is_empty() {
            break;
        }
        let pairs = || hunk.a.clone().zip(hunk.b.clone());
        if hunk.a.len() == hunk.b.len() && pairs().all(|(x, y)| same_sound(a[x], b[y])) {
            for (x, y) in pairs() {
                (place[y], texts[x], want[x]) = (Some(x), theirs.units[y].text, b[y]);
            }
            corrected += 1;
        } else {
            reverted += 1;
        }
        (i, j) = (hunk.a.end, hunk.b.end);
    }

    let mut gaps = vec![String::new(); n + 1];
    let mut moved = Vec::new();
    // A break the reply added is a unit put back, and its text goes with it.
    for (g, &gap) in theirs.gaps.iter().enumerate() {
        if gap.is_empty() {
            continue;
        }
        let after_left = if g == 0 {
            Some(0)
        } else {
            place[g - 1].map(|i| i + 1)
        };
        let before_right = if g == m { Some(n) } else { place[g] };
        match (after_left, before_right) {
            (Some(l), Some(r)) if l == r => gaps[r].push_str(gap),
            (_, Some(r)) => moved.push((r, gap)),
            (Some(l), None) => moved.push((l, gap)),
            (None, None) => {
                let next = place[g..].iter().find_map(|k| *k).unwrap_or(n);
                moved.push((next, gap));
            }
        }
    }
    let render = |gaps: &[String]| {
        let mut out = String::new();
        for (gap, text) in gaps.iter().zip(&texts) {
            out.push_str(gap);
            out.push_str(text);
        }
        out.push_str(&gaps[n]);
        out.split_whitespace().collect::<Vec<_>>().join(" ")
    };
    let fits = |gaps: &[String]| keys(&units(&render(gaps)).units) == want;
    if !fits(&gaps) {
        return None;
    }
    for (at, gap) in moved {
        let mark = gap.trim();
        if !mark.is_empty() && gaps[at].trim_end().ends_with(mark) {
            continue;
        }
        let len = gaps[at].len();
        gaps[at].push_str(gap);
        if !fits(&gaps) {
            gaps[at].truncate(len);
        }
    }
    Some(Merged {
        text: render(&gaps),
        corrected,
        reverted,
    })
}

/// What [`apply`] did over all sections.
#[derive(Debug, Default, PartialEq)]
pub(super) struct Tally {
    /// Sections whose reply could not be used; they keep their transcribed text.
    pub kept: usize,
    pub corrected: usize,
    pub reverted: usize,
}

/// The sections with their replies merged in. Replies belong to the sections with text, in
/// order, as they were sent.
pub(super) fn apply(sections: &[Section], replies: Vec<String>) -> (Vec<Section>, Tally) {
    let (mut replies, mut tally) = (replies.into_iter(), Tally::default());
    let punctuated = sections
        .iter()
        .map(|section| {
            let mut section = section.clone();
            if section.has_text() {
                let reply = replies.next().unwrap_or_default();
                match merge(&section.text, &reply) {
                    Some(merged) => {
                        section.text = merged.text;
                        tally.corrected += merged.corrected;
                        tally.reverted += merged.reverted;
                    }
                    None => tally.kept += 1,
                }
            }
            section
        })
        .collect();
    (punctuated, tally)
}

#[cfg(test)]
mod tests;
