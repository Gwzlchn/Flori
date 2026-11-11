//! A shortest edit script between two sequences (Myers), as the runs where they differ.

use std::ops::Range;

/// A run where the sequences differ: `a[a]` became `b[b]`. Either side may be empty.
#[derive(Debug, Clone, PartialEq)]
pub(super) struct Hunk {
    pub a: Range<usize>,
    pub b: Range<usize>,
}

/// The runs of a shortest edit script from `a` to `b` in order, or None when every script needs
/// more than `max` insertions and deletions. Between two runs, and around them, the sequences
/// are equal. Memory grows with the square of the edits, so `max` bounds it.
pub(super) fn diff<T: PartialEq>(a: &[T], b: &[T], max: usize) -> Option<Vec<Hunk>> {
    // A common head and tail are part of some shortest script, and cutting them keeps the search
    // small when only a few units changed.
    let head = a.iter().zip(b).take_while(|(x, y)| x == y).count();
    let (a_mid, b_mid) = (&a[head..], &b[head..]);
    let tail = a_mid
        .iter()
        .rev()
        .zip(b_mid.iter().rev())
        .take_while(|(x, y)| x == y)
        .count();
    let (a_mid, b_mid) = (&a_mid[..a_mid.len() - tail], &b_mid[..b_mid.len() - tail]);
    let mut pairs = matches(a_mid, b_mid, max)?;
    pairs.push((a_mid.len(), b_mid.len()));
    let (mut hunks, mut from) = (Vec::new(), (0, 0));
    for (x, y) in pairs {
        if (x, y) != from {
            hunks.push(Hunk {
                a: head + from.0..head + x,
                b: head + from.1..head + y,
            });
        }
        from = (x + 1, y + 1);
    }
    Some(hunks)
}

/// The index pairs `(x, y)` with `a[x] == b[y]` that a shortest edit script keeps, ascending.
fn matches<T: PartialEq>(a: &[T], b: &[T], max: usize) -> Option<Vec<(usize, usize)>> {
    let (n, m) = (a.len() as isize, b.len() as isize);
    // trace[d][k + d]: how far x gets on diagonal k = x - y with d edits.
    let mut trace: Vec<Vec<isize>> = Vec::new();
    for d in 0..=max.min(a.len() + b.len()) as isize {
        let mut row = vec![0; 2 * d as usize + 1];
        for k in (-d..=d).step_by(2) {
            let mut x = match trace.last() {
                None => 0,
                Some(prev) => {
                    let at = |k: isize| prev[(k + d - 1) as usize];
                    if k == -d || (k != d && at(k - 1) < at(k + 1)) {
                        at(k + 1)
                    } else {
                        at(k - 1) + 1
                    }
                }
            };
            let mut y = x - k;
            while x < n && y < m && a[x as usize] == b[y as usize] {
                (x, y) = (x + 1, y + 1);
            }
            row[(k + d) as usize] = x;
            if x >= n && y >= m {
                return Some(backtrack(&trace, n, m));
            }
        }
        trace.push(row);
    }
    None
}

fn backtrack(trace: &[Vec<isize>], n: isize, m: isize) -> Vec<(usize, usize)> {
    let (mut pairs, mut x, mut y) = (Vec::new(), n, m);
    for d in (1..=trace.len() as isize).rev() {
        let prev = &trace[(d - 1) as usize];
        let at = |k: isize| prev[(k + d - 1) as usize];
        let k = x - y;
        let down = k == -d || (k != d && at(k - 1) < at(k + 1));
        let from_k = if down { k + 1 } else { k - 1 };
        let (from_x, from_y) = (at(from_k), at(from_k) - from_k);
        let (snake_x, snake_y) = if down {
            (from_x, from_y + 1)
        } else {
            (from_x + 1, from_y)
        };
        while x > snake_x && y > snake_y {
            (x, y) = (x - 1, y - 1);
            pairs.push((x as usize, y as usize));
        }
        (x, y) = (from_x, from_y);
    }
    while x > 0 && y > 0 {
        (x, y) = (x - 1, y - 1);
        pairs.push((x as usize, y as usize));
    }
    pairs.reverse();
    pairs
}
