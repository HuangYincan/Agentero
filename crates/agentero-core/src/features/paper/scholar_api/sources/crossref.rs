//! Crossref REST API source.

use async_trait::async_trait;
use serde_json::Value;

use crate::features::scholar_api::client;
use crate::features::scholar_api::traits::AcademicApi;
use crate::features::scholar_api::{
    ApiCapability, ApiError, ApiPaper, ApiQuery, PaperIdentifiers, PaperUrls,
};

const SOURCE: &str = "crossref";
const API_BASE: &str = "https://api.crossref.org/works";

/// Crossref metadata source.
#[derive(Debug, Clone, Default)]
pub struct CrossrefApi;

#[async_trait]
impl AcademicApi for CrossrefApi {
    fn name(&self) -> &'static str {
        SOURCE
    }

    fn capabilities(&self) -> ApiCapability {
        ApiCapability::SEARCH_BY_TITLE
            | ApiCapability::FETCH_BY_DOI
            | ApiCapability::PROVIDE_ABSTRACT
            | ApiCapability::PROVIDE_CITATION_COUNT
            | ApiCapability::PROVIDE_VENUE
            | ApiCapability::FETCH_REFERENCES
    }

    fn priority(&self) -> i32 {
        90
    }

    async fn fetch(&self, query: &ApiQuery) -> Result<Vec<ApiPaper>, ApiError> {
        match query {
            ApiQuery::Doi(doi) => fetch_by_doi(doi).await.map(|p| vec![p]),
            ApiQuery::Title(title) => search_by_title(title, 5).await,
            _ => Err(ApiError::UnsupportedQuery(query.clone())),
        }
    }

    async fn fetch_references(
        &self,
        doi: Option<&str>,
        _arxiv_id: Option<&str>,
    ) -> Result<Vec<ApiPaper>, ApiError> {
        let Some(doi) = doi.filter(|d| !d.trim().is_empty()) else {
            return Ok(Vec::new());
        };
        let url = format!(
            "{API_BASE}/{}?mailto={CROSSREF_MAILTO}",
            urlencoding::encode(doi.trim())
        );
        let value = client::get_json(&url).await?;
        let Some(items) = value
            .pointer("/message/reference")
            .and_then(|v| v.as_array())
        else {
            return Ok(Vec::new());
        };
        let mut out = Vec::new();
        for item in items {
            if let Some(paper) = map_reference(item) {
                out.push(paper);
            }
        }
        Ok(out)
    }
}

const CROSSREF_MAILTO: &str = "agentero@users.noreply.github.com";

/// Crossref `date-parts[0]` → `YYYY` / `YYYY-MM` / `YYYY-MM-DD`; parts the
/// publisher did not disclose are omitted.
fn date_from_parts(parts: &[Value]) -> Option<String> {
    let year = parts.first()?.as_i64()?;
    if !(1000..=2100).contains(&year) {
        return None;
    }
    let mut date = format!("{year:04}");
    let month = parts
        .get(1)
        .and_then(|v| v.as_i64())
        .filter(|m| (1..=12).contains(m));
    if let Some(month) = month {
        date.push_str(&format!("-{month:02}"));
        let day = parts
            .get(2)
            .and_then(|v| v.as_i64())
            .filter(|d| (1..=31).contains(d));
        if let Some(day) = day {
            date.push_str(&format!("-{day:02}"));
        }
    }
    Some(date)
}

fn map_reference(item: &Value) -> Option<ApiPaper> {
    let title = str_field(item, "article-title").or_else(|| str_field(item, "volume-title"));
    let doi = str_field(item, "DOI");
    let raw = str_field(item, "unstructured");
    if title.is_none() && doi.is_none() && raw.is_none() {
        return None;
    }
    let year = str_field(item, "year").and_then(|y| y.trim().parse::<i32>().ok());
    Some(ApiPaper {
        identifiers: PaperIdentifiers {
            doi,
            arxiv_id: None,
            isbn: None,
            pmid: None,
        },
        title: title.unwrap_or_default(),
        authors: str_field(item, "author")
            .map(|a| vec![a])
            .unwrap_or_default(),
        year,
        date: year.map(|y| y.to_string()),
        venue: str_field(item, "journal-title"),
        volume: None,
        issue: None,
        pages: None,
        publisher: None,
        abstract_text: None,
        urls: PaperUrls {
            pdf: None,
            html: None,
            landing: None,
        },
        citation_count: None,
        language: None,
        source: SOURCE,
        raw,
    })
}

async fn fetch_by_doi(doi: &str) -> Result<ApiPaper, ApiError> {
    let url = format!("{API_BASE}/{}", urlencoding::encode(doi.trim()));
    let value = client::get_json(&url).await?;
    let message = value.pointer("/message").unwrap_or(&Value::Null);
    map_work(message, Some(doi.trim())).ok_or(ApiError::NotFound)
}

async fn search_by_title(title: &str, limit: usize) -> Result<Vec<ApiPaper>, ApiError> {
    let url = format!(
        "{API_BASE}?query.title={}&rows={}&select=title,author,issued,published-print,published-online,container-title,volume,issue,page,DOI,publisher,URL,type,abstract,is-referenced-by-count",
        urlencoding::encode(title),
        limit
    );
    let value = client::get_json(&url).await?;
    let Some(items) = value.pointer("/message/items").and_then(|v| v.as_array()) else {
        return Ok(Vec::new());
    };

    let mut out = Vec::new();
    for item in items {
        if let Some(paper) = map_work(item, None) {
            out.push(paper);
        }
    }
    Ok(out)
}

fn map_work(message: &Value, known_doi: Option<&str>) -> Option<ApiPaper> {
    let title = message
        .get("title")
        .and_then(|v| v.as_array())
        .and_then(|a| a.first())
        .and_then(|v| v.as_str())
        .map(collapse_ws)
        .filter(|s| !s.is_empty())?;

    let doi = known_doi
        .map(str::to_string)
        .or_else(|| str_field(message, "DOI"))?;

    let mut authors = Vec::new();
    if let Some(arr) = message.get("author").and_then(|v| v.as_array()) {
        for a in arr {
            if let Some(name) = a.get("name").and_then(|v| v.as_str()) {
                let n = collapse_ws(name);
                if !n.is_empty() {
                    authors.push(n);
                }
                continue;
            }
            let given = a.get("given").and_then(|v| v.as_str()).unwrap_or("");
            let family = a.get("family").and_then(|v| v.as_str()).unwrap_or("");
            let n = format!("{given} {family}").trim().to_string();
            if !n.is_empty() {
                authors.push(n);
            }
        }
    }

    let date = ["issued", "published-online", "published-print"]
        .iter()
        .find_map(|key| {
            message
                .pointer(&format!("/{key}/date-parts/0"))
                .and_then(|v| v.as_array())
                .and_then(|parts| date_from_parts(parts))
        });
    let year = date
        .as_ref()
        .and_then(|d| d.get(..4).and_then(|y| y.parse::<i32>().ok()));

    // Crossref abstracts are JATS XML; strip tags so downstream text stays plain.
    let abstract_text = str_or_first(message, "abstract").map(|s| {
        let mut plain = String::with_capacity(s.len());
        let mut in_tag = false;
        for c in s.chars() {
            match c {
                '<' => in_tag = true,
                '>' => in_tag = false,
                _ if !in_tag => plain.push(c),
                _ => {}
            }
        }
        collapse_ws(&plain)
    });

    Some(ApiPaper {
        identifiers: PaperIdentifiers {
            doi: Some(doi.clone()),
            arxiv_id: None,
            isbn: None,
            pmid: None,
        },
        title,
        authors,
        year,
        date,
        venue: str_or_first(message, "container-title"),
        volume: str_field(message, "volume"),
        issue: str_field(message, "issue"),
        pages: str_field(message, "page"),
        publisher: str_or_first(message, "publisher"),
        abstract_text,
        urls: PaperUrls {
            pdf: None,
            html: Some(format!("https://doi.org/{doi}")),
            landing: Some(format!("https://doi.org/{doi}")),
        },
        citation_count: message
            .get("is-referenced-by-count")
            .and_then(|v| v.as_i64()),
        language: str_or_first(message, "language"),
        source: SOURCE,
        raw: None,
    })
}

fn str_field(message: &Value, key: &str) -> Option<String> {
    message
        .get(key)
        .and_then(|v| v.as_str())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn str_or_first(message: &Value, key: &str) -> Option<String> {
    let v = message.get(key)?;
    let s = match v {
        Value::String(s) => Some(s.clone()),
        Value::Array(a) => a.first().and_then(|x| x.as_str()).map(String::from),
        _ => None,
    }?;
    let s = s.trim().to_string();
    if s.is_empty() {
        None
    } else {
        Some(s)
    }
}

fn collapse_ws(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn maps_crossref_work() {
        let message = json!({
            "title": ["Attention Is All You Need"],
            "author": [
                { "given": "Ashish", "family": "Vaswani" },
                { "given": "Noam", "family": "Shazeer" }
            ],
            "issued": { "date-parts": [[2017]] },
            "DOI": "10.48550/arXiv.1706.03762",
            "container-title": ["NeurIPS"],
            "volume": ["30"],
            "page": ["5998-6008"],
            "publisher": ["NeurIPS"],
            "abstract": "<p>The dominant sequence transduction models.</p>"
        });
        let paper = map_work(&message, None).expect("mapped");
        assert_eq!(paper.title, "Attention Is All You Need");
        assert_eq!(
            paper.identifiers.doi.as_deref(),
            Some("10.48550/arXiv.1706.03762")
        );
        assert_eq!(paper.year, Some(2017));
        assert_eq!(paper.date.as_deref(), Some("2017"));
        assert_eq!(paper.venue.as_deref(), Some("NeurIPS"));
        assert_eq!(
            paper.abstract_text.as_deref(),
            Some("The dominant sequence transduction models.")
        );
        assert_eq!(paper.authors, vec!["Ashish Vaswani", "Noam Shazeer"]);
    }

    #[test]
    fn maps_publication_date_precision() {
        let day = json!({
            "title": ["T"], "DOI": "10.1/x",
            "issued": { "date-parts": [[2017, 6, 12]] }
        });
        let paper = map_work(&day, None).expect("mapped");
        assert_eq!(paper.date.as_deref(), Some("2017-06-12"));
        assert_eq!(paper.year, Some(2017));

        // Title search selects print/online instead of `issued`.
        let online = json!({
            "title": ["T"], "DOI": "10.1/x",
            "published-online": { "date-parts": [[2023, 8]] }
        });
        let paper = map_work(&online, None).expect("mapped");
        assert_eq!(paper.date.as_deref(), Some("2023-08"));
        assert_eq!(paper.year, Some(2023));
    }

    #[test]
    fn maps_citation_count() {
        let message = json!({
            "title": ["Attention Is All You Need"],
            "DOI": "10.48550/arXiv.1706.03762",
            "type": "journal-article",
            "container-title": ["NeurIPS"],
            "is-referenced-by-count": 121363
        });
        let paper = map_work(&message, None).expect("mapped");
        assert_eq!(paper.citation_count, Some(121_363));

        let without = json!({
            "title": ["Attention Is All You Need"],
            "DOI": "10.48550/arXiv.1706.03762",
            "type": "journal-article"
        });
        assert_eq!(
            map_work(&without, None).expect("mapped").citation_count,
            None
        );
    }
}
