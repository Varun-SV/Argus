//! UTC timestamps for result documents (RFC 3339, whole seconds).

use std::time::SystemTime;

/// Formats the current time as `YYYY-MM-DDTHH:MM:SSZ`.
pub fn now_utc() -> String {
    humantime::format_rfc3339_seconds(SystemTime::now()).to_string()
}

/// Validates a user-supplied RFC 3339 UTC timestamp and normalizes it to whole seconds.
pub fn normalize(value: &str) -> Result<String, String> {
    let parsed = humantime::parse_rfc3339_weak(value.trim()).map_err(|e| {
        format!("`{value}` is not an RFC 3339 UTC timestamp like 2026-10-08T12:00:00Z: {e}")
    })?;
    Ok(humantime::format_rfc3339_seconds(parsed).to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizes_and_rejects() {
        assert_eq!(
            normalize("2026-10-08T12:34:56Z").unwrap(),
            "2026-10-08T12:34:56Z"
        );
        assert_eq!(
            normalize("2026-10-08T12:34:56.789Z").unwrap(),
            "2026-10-08T12:34:56Z"
        );
        assert!(normalize("yesterday").is_err());
        let now = now_utc();
        assert!(now.ends_with('Z') && now.len() == 20, "{now}");
    }
}
