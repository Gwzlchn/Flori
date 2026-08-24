const HASH_SIZE: usize = 8;
const SAMPLE_SIZE: usize = 32;

pub(super) fn ssim(left: &[u8], right: &[u8]) -> Option<f64> {
    if left.len() != right.len() || left.is_empty() {
        return None;
    }
    let count = left.len() as f64;
    let mean_left = left.iter().map(|value| f64::from(*value)).sum::<f64>() / count;
    let mean_right = right.iter().map(|value| f64::from(*value)).sum::<f64>() / count;
    let mut variance_left = 0.0;
    let mut variance_right = 0.0;
    let mut covariance = 0.0;
    for (&left, &right) in left.iter().zip(right) {
        let left = f64::from(left) - mean_left;
        let right = f64::from(right) - mean_right;
        variance_left += left * left;
        variance_right += right * right;
        covariance += left * right;
    }
    let divisor = (count - 1.0).max(1.0);
    variance_left /= divisor;
    variance_right /= divisor;
    covariance /= divisor;
    let c1 = (0.01_f64 * 255.0).powi(2);
    let c2 = (0.03_f64 * 255.0).powi(2);
    Some(
        ((2.0 * mean_left * mean_right + c1) * (2.0 * covariance + c2))
            / ((mean_left.powi(2) + mean_right.powi(2) + c1)
                * (variance_left + variance_right + c2)),
    )
}

pub(super) fn perceptual_hash(pixels: &[u8]) -> Option<u64> {
    if pixels.len() != SAMPLE_SIZE * SAMPLE_SIZE {
        return None;
    }
    let mut coefficients = [0.0; HASH_SIZE * HASH_SIZE];
    for v in 0..HASH_SIZE {
        for u in 0..HASH_SIZE {
            let mut sum = 0.0;
            for y in 0..SAMPLE_SIZE {
                for x in 0..SAMPLE_SIZE {
                    let pixel = f64::from(pixels[y * SAMPLE_SIZE + x]);
                    let x_angle =
                        ((2 * x + 1) * u) as f64 * std::f64::consts::PI / (2 * SAMPLE_SIZE) as f64;
                    let y_angle =
                        ((2 * y + 1) * v) as f64 * std::f64::consts::PI / (2 * SAMPLE_SIZE) as f64;
                    sum += pixel * x_angle.cos() * y_angle.cos();
                }
            }
            coefficients[v * HASH_SIZE + u] = sum;
        }
    }
    let mut values = coefficients[1..].to_vec();
    values.sort_by(f64::total_cmp);
    let median = values[values.len() / 2];
    Some(
        coefficients
            .iter()
            .enumerate()
            .fold(0_u64, |hash, (index, value)| {
                hash | (u64::from(*value > median) << index)
            }),
    )
}

#[cfg(test)]
mod tests {
    use super::{perceptual_hash, ssim};

    #[test]
    fn similarity_distinguishes_equal_and_opposite_frames() {
        let black = vec![0; 1_024];
        let white = vec![255; 1_024];
        assert_eq!(ssim(&black, &black), Some(1.0));
        assert!(ssim(&black, &white).expect("SSIM") < 0.01);
        assert_eq!(perceptual_hash(&black), perceptual_hash(&black));
    }
}
