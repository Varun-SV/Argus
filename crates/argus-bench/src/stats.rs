//! Summary statistics over runs (spec §11: "reports median of 5 runs").

use serde::{Deserialize, Serialize};

/// Median, minimum and maximum of a set of integer measurements.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Summary {
    /// Median. For an even count, the mean of the two middle values, rounded down.
    pub median: u64,
    /// Smallest value.
    pub min: u64,
    /// Largest value.
    pub max: u64,
}

/// Summarizes `values`; `None` when there are none.
pub fn summarize(values: &[u64]) -> Option<Summary> {
    if values.is_empty() {
        return None;
    }
    let mut sorted = values.to_vec();
    sorted.sort_unstable();
    let n = sorted.len();
    let median = if n % 2 == 1 {
        sorted[n / 2]
    } else {
        let (a, b) = (sorted[n / 2 - 1], sorted[n / 2]);
        // Overflow-free floor((a + b) / 2).
        a / 2 + b / 2 + (a % 2 + b % 2) / 2
    };
    Some(Summary {
        median,
        min: sorted[0],
        max: sorted[n - 1],
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_has_no_summary() {
        assert_eq!(summarize(&[]), None);
    }

    #[test]
    fn odd_count_takes_middle() {
        let s = summarize(&[50, 10, 40, 20, 30]).unwrap();
        assert_eq!((s.median, s.min, s.max), (30, 10, 50));
    }

    #[test]
    fn even_count_averages_middle_pair() {
        let s = summarize(&[4, 1, 3, 2]).unwrap();
        assert_eq!((s.median, s.min, s.max), (2, 1, 4));
        let s = summarize(&[u64::MAX, u64::MAX - 2]).unwrap();
        assert_eq!(s.median, u64::MAX - 1);
    }

    #[test]
    fn single_value() {
        let s = summarize(&[7]).unwrap();
        assert_eq!((s.median, s.min, s.max), (7, 7, 7));
    }
}
