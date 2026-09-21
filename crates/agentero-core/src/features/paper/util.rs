//! Small metadata helpers shared across the `paper` subtrees (`import`,
//! `analyze/refs`, `scholar_api`): JSON field scraping and whitespace folding.

use serde_json::Value;

/// Trimmed, non-empty string field of a JSON object. `None` when the key is
/// missing, not a string, or blank.
pub(crate) fn str_field(value: &Value, key: &str) -> Option<String> {
    value
        .get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// [`str_field`] addressed by a JSON pointer instead of a top-level key.
pub(crate) fn str_field_at(value: &Value, pointer: &str) -> Option<String> {
    value
        .pointer(pointer)
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

/// Collapse every whitespace run into a single space and trim both ends.
pub(crate) fn collapse_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}
