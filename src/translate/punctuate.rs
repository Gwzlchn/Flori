//! The pass translate runs on a Chinese video instead of a translation: the AI only adds
//! punctuation, and code checks every section so that no character, number or term changes.
//! Rules: contract 1 "translate" in docs/contracts.md.

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

/// What punctuation and whitespace may not change: every other character in order, and the
/// break between two ASCII letters or digits, which separates two words. An ASCII mark directly
/// between two ASCII letters or digits belongs to a number or term (3.5, 1,000, 10:30, Node.js,
/// don't) and counts as content.
fn bare(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let word = |i: usize| chars.get(i).is_some_and(char::is_ascii_alphanumeric);
    let (mut out, mut gap) = (String::with_capacity(text.len()), false);
    for (i, &c) in chars.iter().enumerate() {
        let inner = c.is_ascii() && i > 0 && word(i - 1) && word(i + 1);
        if c.is_whitespace() || (PUNCTUATION.contains(&c) && !inner) {
            gap = true;
            continue;
        }
        if gap && c.is_ascii_alphanumeric() && out.ends_with(|p: char| p.is_ascii_alphanumeric()) {
            out.push(' ');
        }
        gap = false;
        out.push(c);
    }
    out
}

/// The reply when it differs from the original only in punctuation and whitespace, else None.
/// Whitespace runs collapse to one space: a line break or indent from the AI would become
/// Markdown structure, and video-frames' text has none either.
pub(super) fn verify(original: &str, reply: &str) -> Option<String> {
    (bare(reply) == bare(original)).then(|| reply.split_whitespace().collect::<Vec<_>>().join(" "))
}

/// The sections with their checked replies as text, and how many kept their transcribed text
/// because the reply changed more than punctuation. Replies belong to the sections with text,
/// in order, as they were sent.
pub(super) fn apply(sections: &[Section], replies: Vec<String>) -> (Vec<Section>, usize) {
    let (mut replies, mut kept) = (replies.into_iter(), 0);
    let punctuated = sections
        .iter()
        .map(|section| {
            let mut section = section.clone();
            if section.has_text() {
                let reply = replies.next().unwrap_or_default();
                match verify(&section.text, &reply) {
                    Some(text) => section.text = text,
                    None => kept += 1,
                }
            }
            section
        })
        .collect();
    (punctuated, kept)
}

#[cfg(test)]
mod tests;
