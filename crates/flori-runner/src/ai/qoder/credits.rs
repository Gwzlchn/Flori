use super::QoderError;

pub(super) fn credits_to_micros(text: &str) -> Result<u64, QoderError> {
    if text.starts_with('-') {
        return Err(QoderError::InvalidCredits);
    }
    let (mantissa, exponent) =
        text.split_once(['e', 'E'])
            .map_or((text, 0), |(value, exponent)| {
                exponent
                    .parse::<i32>()
                    .map(|exponent| (value, exponent))
                    .unwrap_or(("", i32::MIN))
            });
    let (whole, fraction) = mantissa.split_once('.').unwrap_or((mantissa, ""));
    if whole.is_empty()
        || !whole
            .bytes()
            .chain(fraction.bytes())
            .all(|byte| byte.is_ascii_digit())
    {
        return Err(QoderError::InvalidCredits);
    }
    let digits = format!("{whole}{fraction}");
    let fraction_len = i32::try_from(fraction.len()).map_err(|_| QoderError::InvalidCredits)?;
    let scale = exponent
        .checked_sub(fraction_len)
        .and_then(|value| value.checked_add(6))
        .ok_or(QoderError::InvalidCredits)?;
    if scale >= 0 {
        let value = digits
            .parse::<u64>()
            .map_err(|_| QoderError::InvalidCredits)?;
        let power = u32::try_from(scale)
            .ok()
            .and_then(|power| 10_u64.checked_pow(power))
            .ok_or(QoderError::InvalidCredits)?;
        return value.checked_mul(power).ok_or(QoderError::InvalidCredits);
    }

    let discarded =
        usize::try_from(scale.unsigned_abs()).map_err(|_| QoderError::InvalidCredits)?;
    if discarded > digits.len() {
        return Ok(0);
    }
    let kept_len = digits.len() - discarded;
    let kept = digits[..kept_len].trim_start_matches('0');
    let value = if kept.is_empty() {
        0
    } else {
        kept.parse::<u64>()
            .map_err(|_| QoderError::InvalidCredits)?
    };
    if digits.as_bytes()[kept_len] >= b'5' {
        value.checked_add(1).ok_or(QoderError::InvalidCredits)
    } else {
        Ok(value)
    }
}
