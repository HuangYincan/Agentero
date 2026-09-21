//! Frontmatter/body splitting for Markdown documents.
//!
//! Distinct from [`crate::frontmatter`], which *reads* YAML scalar fields for
//! `SKILL.md`-style documents, and from [`wiki::frontmatter`], which reads and
//! patches `aliases`; this module only locates the leading block.
//!
//! [`wiki::frontmatter`]: crate::features::markdown::wiki::frontmatter

/// Split a document into (frontmatter including fences, rest). When there is
/// no frontmatter the first element is empty.
///
/// A leading BOM is preserved in the returned frontmatter; the body has its
/// leading blank lines trimmed.
pub fn split_off_frontmatter(md: &str) -> (String, String) {
    let trimmed = md.trim_start_matches('\u{feff}');
    let lead = &md[..md.len() - trimmed.len()];
    let Some(rest) = trimmed.strip_prefix("---") else {
        return (String::new(), md.to_string());
    };
    let Some(rest2) = rest.strip_prefix(['\n', '\r']) else {
        return (String::new(), md.to_string());
    };
    let mut search = rest2;
    loop {
        if let Some(after) = search.strip_prefix("---") {
            if after.is_empty() || after.starts_with('\n') || after.starts_with('\r') {
                let split = md.len() - after.len();
                let (front, body) = md.split_at(split);
                return (
                    format!("{lead}{front}"),
                    body.trim_start_matches(['\r', '\n']).to_string(),
                );
            }
        }
        let Some(idx) = search.find("\n---") else {
            return (String::new(), md.to_string());
        };
        let after = &search[idx + 4..];
        if after.is_empty() || after.starts_with('\n') || after.starts_with('\r') {
            let split = md.len() - after.len();
            let (front, body) = md.split_at(split);
            return (
                format!("{lead}{front}"),
                body.trim_start_matches(['\r', '\n']).to_string(),
            );
        }
        search = &search[idx + 1..];
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_bom_in_frontmatter() {
        // The BOM is preserved in the returned front block. Note the front
        // slice already carries it, and the stripped prefix is prepended on
        // top, so it appears twice — pre-existing behavior of both former
        // copies, locked in here so the shared version cannot drift.
        let (front, body) = split_off_frontmatter("\u{feff}---\na: 1\n---\n\nbody\ntext");
        assert_eq!(front, "\u{feff}\u{feff}---\na: 1\n---");
        assert_eq!(body, "body\ntext");
    }

    #[test]
    fn handles_crlf() {
        let (front, body) = split_off_frontmatter("---\r\na: 1\r\n---\r\n\r\nbody\r\n");
        assert_eq!(front, "---\r\na: 1\r\n---");
        assert_eq!(body, "body\r\n");
    }

    #[test]
    fn closing_fence_must_own_its_line() {
        // `---xxx` inside the block is not a fence; the real one closes it.
        let (front, body) = split_off_frontmatter("---\na: 1\n---xxx\n---\nbody");
        assert_eq!(front, "---\na: 1\n---xxx\n---");
        assert_eq!(body, "body");
        // A leading `---not a fence` in the body never opens a block.
        let (front, body) = split_off_frontmatter("---oops\n\n---xxx\n\nbody");
        assert!(front.is_empty());
        assert_eq!(body, "---oops\n\n---xxx\n\nbody");
    }

    #[test]
    fn no_frontmatter_returns_empty_head() {
        for md in ["# Body only\n\ntext", "", "---", "--- no newline"] {
            let (front, body) = split_off_frontmatter(md);
            assert!(front.is_empty(), "md: {md:?}");
            assert_eq!(body, md);
        }
    }

    #[test]
    fn unterminated_block_is_not_frontmatter() {
        let md = "---\na: 1\n\nbody\n\n--- trailing";
        let (front, body) = split_off_frontmatter(md);
        assert!(front.is_empty());
        assert_eq!(body, md);
    }
}
