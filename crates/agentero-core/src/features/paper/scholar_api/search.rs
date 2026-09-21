//! Title/keyword search orchestration across `scholar_api` sources.
//!
//! Replaces the previous hard-coded source-racing logic with a configurable
//! `SourceScope`: callers pick which sources to query, the module fires them
//! concurrently, then deduplicates, merges, and ranks the results using
//! title similarity, RRF, source priority, and citation count.

use std::time::Duration;

use crate::features::paper::import::api_mapper::merge_api_paper_candidates;
use crate::features::scholar_api::scoring::{
    is_same_paper, title_similarity, title_similarity_key,
};
use crate::features::scholar_api::sources::{
    alphaxiv::AlphaxivApi, arxiv::ArxivApi, crossref::CrossrefApi, openalex::OpenAlexApi,
    pubmed::PubMedApi, semantic_scholar::SemanticScholarApi,
};
use crate::features::scholar_api::traits::AcademicApi;
use crate::features::scholar_api::{normalize_id, ApiError, ApiPaper, ApiQuery};

/// Budget for S2's `/paper/search/match` fast path.
const MATCH_BUDGET: Duration = Duration::from_secs(3);

/// Budget for a single source's title search.
const FETCH_BUDGET: Duration = Duration::from_secs(8);

/// Default title-similarity threshold for accepting a candidate.
const MATCH_THRESHOLD: i32 = 70;

/// RRF constant: `1 / (k + rank)`.
const RRF_K: f64 = 60.0;

/// Weight of title similarity in the final fusion score.
const SCORE_TITLE_WEIGHT: f64 = 0.50;
/// Weight of RRF (multi-source consensus) in the final fusion score.
const SCORE_RRF_WEIGHT: f64 = 0.30;
/// Weight of source priority in the final fusion score.
const SCORE_PRIORITY_WEIGHT: f64 = 0.10;
/// Weight of citation count in the final fusion score.
const SCORE_CITATION_WEIGHT: f64 = 0.10;

/// All sources capable of title search, ordered by descending priority.
///
/// With six sources firing concurrently under the shared
/// `GLOBAL_CONCURRENCY = 4` client semaphore, the tail of the batch queues
/// behind earlier permits — queue time counts against each source's
/// `FETCH_BUDGET`, so worst-case search latency grows slightly with each
/// source added here.
const ALL_TITLE_SOURCES: &[&'static dyn AcademicApi] = &[
    &SemanticScholarApi,
    &CrossrefApi,
    &OpenAlexApi,
    &ArxivApi,
    &PubMedApi,
    &AlphaxivApi,
];

/// Preset scopes for title/keyword search.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub enum SourceScope {
    /// All sources capable of title search.
    #[default]
    All,
    /// General scholarly sources: S2, Crossref, OpenAlex.
    Scholarly,
    /// Open-access-friendly sources: arXiv, S2.
    OpenAccess,
    /// Preprints only: arXiv.
    Preprint,
    /// Biomedical sources: PubMed, S2, Crossref.
    Biomedical,
    /// Explicit source list by machine-readable name, e.g. `["s2", "crossref"]`.
    /// Names are filtered against available sources and then sorted by priority.
    Custom(Vec<String>),
}

impl SourceScope {
    /// Expand the scope to a concrete, priority-ordered list of sources.
    fn sources(&self) -> Vec<&'static dyn AcademicApi> {
        match self {
            SourceScope::All => ALL_TITLE_SOURCES.to_vec(),
            SourceScope::Scholarly => vec![&SemanticScholarApi, &CrossrefApi, &OpenAlexApi],
            SourceScope::OpenAccess => vec![&ArxivApi, &SemanticScholarApi],
            SourceScope::Preprint => vec![&ArxivApi],
            SourceScope::Biomedical => vec![&PubMedApi, &SemanticScholarApi, &CrossrefApi],
            SourceScope::Custom(names) => ALL_TITLE_SOURCES
                .iter()
                .filter(|s| names.contains(&s.name().to_string()))
                .copied()
                .collect(),
        }
    }
}

/// Why one source's title search failed (kept `ApiError`-free so failure
/// policies are comparable in tests).
#[derive(Debug, Clone, PartialEq, Eq)]
enum FailureKind {
    RateLimited,
    Timeout,
    Other(String),
}

/// One source's failure, retained for end-of-search summarization.
#[derive(Debug, Clone)]
struct SearchFailure {
    source: &'static str,
    kind: FailureKind,
}

fn failure_kind(e: &ApiError) -> FailureKind {
    match e {
        ApiError::RateLimited => FailureKind::RateLimited,
        other => FailureKind::Other(other.to_string()),
    }
}

/// Decide whether a zero-hit title search should surface an error instead of
/// an empty result. Pure so the policy is unit-testable without network.
///
/// - `None` when any source answered (even with zero hits), when there are
///   hits, or when nothing was attempted — keep the historical `Ok(vec![])`.
///   A single authoritative "no such paper" beats guessing rate limiting.
/// - `Some(RateLimited)` when every attempted source failed and at least one
///   was rate limited (otherwise the caller would misreport "no search
///   results" while the sources were merely throttled — #524).
/// - `Some(Network(summary))` when every source failed without a rate limit.
fn search_failure_error(
    failures: &[SearchFailure],
    attempted: usize,
    hit_count: usize,
) -> Option<ApiError> {
    if attempted == 0 || hit_count > 0 || failures.len() < attempted {
        return None;
    }
    if failures.iter().any(|f| f.kind == FailureKind::RateLimited) {
        return Some(ApiError::RateLimited);
    }
    let names = failures
        .iter()
        .map(|f| f.source)
        .collect::<Vec<_>>()
        .join(", ");
    Some(ApiError::Network(format!(
        "all {attempted} title-search sources failed ({names})"
    )))
}

/// One scoped source's title-search outcome for [`search_papers_by_title`].
enum SourceOutcome {
    Hits(Vec<RankedHit>),
    Failed(SearchFailure),
}

/// A single search hit together with its rank inside the source that produced it.
struct RankedHit {
    paper: ApiPaper,
    source_name: &'static str,
    source_rank: usize,
}

/// A group of duplicate hits from one or more sources.
struct DuplicateGroup {
    papers: Vec<ApiPaper>,
    source_ranks: Vec<(&'static str, usize)>,
}

/// Search papers by title/keyword across the sources selected by `scope`.
///
/// The search is concurrent per source with independent timeouts. S2's
/// `/paper/search/match` fast path is used when the scope includes S2. The
/// returned list is unranked and may still contain duplicates; use
/// [`fuse_and_rank_candidates`] (or the convenience [`rank_candidates`]) to
/// order and trim it.
pub async fn search_papers_by_title(
    query: &str,
    scope: SourceScope,
    _limit: usize,
) -> Result<Vec<ApiPaper>, ApiError> {
    let query = query.trim();
    if query.is_empty() {
        return Ok(Vec::new());
    }

    let sources = scope.sources();
    if sources.is_empty() {
        return Ok(Vec::new());
    }

    let mut ranked_hits: Vec<RankedHit> = Vec::new();

    // S2 match fast path: when scope includes S2, try the single-best-match
    // endpoint concurrently with the broader title searches.
    let s2_in_scope = sources.iter().any(|s| s.name() == "s2");
    let match_future = async {
        if !s2_in_scope {
            return Vec::new();
        }
        match tokio::time::timeout(MATCH_BUDGET, SemanticScholarApi.search_match(query)).await {
            Ok(Ok(Some(paper))) => vec![RankedHit {
                paper,
                source_name: "s2",
                source_rank: 0,
            }],
            Ok(Ok(None)) => Vec::new(),
            Ok(Err(e)) => {
                log::warn!("title search: S2 match failed ({e}); continuing with title search");
                Vec::new()
            }
            Err(_) => {
                log::warn!(
                    "title search: S2 match exceeded {}s budget",
                    MATCH_BUDGET.as_secs()
                );
                Vec::new()
            }
        }
    };

    // Concurrent title search across all scoped sources.
    let fetch_futures = sources.iter().map(|source| {
        let api_query = ApiQuery::Title(query.to_string());
        let source: &'static dyn AcademicApi = *source;
        async move {
            let name = source.name();
            match tokio::time::timeout(FETCH_BUDGET, source.fetch(&api_query)).await {
                Ok(Ok(hits)) => SourceOutcome::Hits(
                    hits.into_iter()
                        .enumerate()
                        .map(|(rank, paper)| RankedHit {
                            paper,
                            source_name: name,
                            source_rank: rank,
                        })
                        .collect::<Vec<_>>(),
                ),
                Ok(Err(e)) => {
                    log::warn!("title search: {name} failed ({e})");
                    SourceOutcome::Failed(SearchFailure {
                        source: name,
                        kind: failure_kind(&e),
                    })
                }
                Err(_) => {
                    log::warn!(
                        "title search: {name} exceeded {}s budget",
                        FETCH_BUDGET.as_secs()
                    );
                    SourceOutcome::Failed(SearchFailure {
                        source: name,
                        kind: FailureKind::Timeout,
                    })
                }
            }
        }
    });

    let (match_hits, fetch_results) =
        tokio::join!(match_future, futures_util::future::join_all(fetch_futures));

    let attempted = sources.len();
    let mut failures: Vec<SearchFailure> = Vec::new();
    ranked_hits.extend(match_hits);
    for outcome in fetch_results {
        match outcome {
            SourceOutcome::Hits(mut hits) => ranked_hits.append(&mut hits),
            SourceOutcome::Failed(failure) => failures.push(failure),
        }
    }
    // Every scoped source failed: report why instead of an empty result so a
    // fully rate-limited search is not misreported as "no search results".
    if let Some(e) = search_failure_error(&failures, attempted, ranked_hits.len()) {
        return Err(e);
    }

    // Sort by source priority descending so deduplication keeps the highest
    // priority source as the merge base.
    ranked_hits.sort_by_key(|b| std::cmp::Reverse(source_priority(b.source_name)));

    Ok(ranked_hits.into_iter().map(|h| h.paper).collect())
}

/// Resolve the best single candidate for a title query.
///
/// Searches across `scope`, fuses the results, and returns the top-scoring
/// candidate if it clears the default match threshold.
pub async fn resolve_best_by_title(query: &str, scope: SourceScope) -> Result<ApiPaper, ApiError> {
    let hits = search_papers_by_title(query, scope, 5).await?;
    let ranked = fuse_and_rank_candidates(hits, query, 1);
    ranked.into_iter().next().ok_or(ApiError::NotFound)
}

/// Fuse, deduplicate, merge, and rank raw search hits.
///
/// 1. Groups duplicates by identifier or fuzzy `is_same_paper`.
/// 2. Merges each group using source-specific field preferences.
/// 3. Scores each merged candidate with title similarity + RRF + source priority
///    + citation count.
/// 4. Filters out candidates below [`MATCH_THRESHOLD`] and returns the top
///    `limit` results.
pub fn fuse_and_rank_candidates(hits: Vec<ApiPaper>, query: &str, limit: usize) -> Vec<ApiPaper> {
    if hits.is_empty() || query.trim().is_empty() {
        return Vec::new();
    }

    // Re-attach per-source ranks so RRF can be computed after merge.
    // `search_papers_by_title` appends results source-by-source in priority
    // order, so the position within each source name is the intra-source rank.
    let mut per_source_counts: std::collections::HashMap<&'static str, usize> =
        std::collections::HashMap::new();
    let ranked_hits: Vec<RankedHit> = hits
        .into_iter()
        .map(|paper| {
            let source = paper.source;
            let rank = *per_source_counts.get(source).unwrap_or(&0);
            per_source_counts.insert(source, rank + 1);
            RankedHit {
                paper,
                source_name: source,
                source_rank: rank,
            }
        })
        .collect();

    let groups = group_duplicates(ranked_hits);
    let merged: Vec<MergedCandidate> = groups
        .into_iter()
        .map(|g| {
            let paper = merge_api_paper_candidates(&g.papers);
            let rrf = g
                .source_ranks
                .iter()
                .map(|(_, rank)| 1.0 / (RRF_K + *rank as f64))
                .sum::<f64>();
            MergedCandidate { paper, rrf }
        })
        .collect();

    let norm_query = title_similarity_key(query);
    let mut scored: Vec<ScoredCandidate> = merged
        .into_iter()
        .map(|m| {
            let title_sim =
                title_similarity(&norm_query, &title_similarity_key(&m.paper.title)) as f64 / 100.0;
            let priority_boost = source_priority(m.paper.source) as f64 / 100.0;
            let citation_boost = m
                .paper
                .citation_count
                .map(|c| ((c as f64 + 1.0).log10()) / 10.0)
                .unwrap_or(0.0);
            let score = title_sim * SCORE_TITLE_WEIGHT
                + m.rrf * SCORE_RRF_WEIGHT
                + priority_boost * SCORE_PRIORITY_WEIGHT
                + citation_boost * SCORE_CITATION_WEIGHT;
            ScoredCandidate {
                paper: m.paper,
                score,
                title_sim: (title_sim * 100.0) as i32,
            }
        })
        .filter(|s| s.title_sim >= MATCH_THRESHOLD)
        .collect();

    let target = title_similarity_key(query);
    scored.sort_by(|a, b| {
        let exact_a = title_similarity_key(&a.paper.title) == target;
        let exact_b = title_similarity_key(&b.paper.title) == target;
        exact_a
            .cmp(&exact_b)
            .reverse()
            .then_with(|| a.score.total_cmp(&b.score).reverse())
            .then_with(|| {
                source_priority(a.paper.source)
                    .cmp(&source_priority(b.paper.source))
                    .reverse()
            })
            .then_with(|| {
                a.paper
                    .citation_count
                    .cmp(&b.paper.citation_count)
                    .reverse()
            })
    });

    scored.truncate(limit.max(1));
    scored.into_iter().map(|s| s.paper).collect()
}

/// Rank and truncate raw search hits (backward-compatible alias).
///
/// Equivalent to `fuse_and_rank_candidates` with the default match threshold.
pub fn rank_candidates(hits: Vec<ApiPaper>, query: &str, limit: usize) -> Vec<ApiPaper> {
    fuse_and_rank_candidates(hits, query, limit)
}

fn source_priority(name: &str) -> i32 {
    ALL_TITLE_SOURCES
        .iter()
        .find(|s| s.name() == name)
        .map(|s| s.priority())
        .unwrap_or(0)
}

fn group_duplicates(ranked_hits: Vec<RankedHit>) -> Vec<DuplicateGroup> {
    let mut groups: Vec<DuplicateGroup> = Vec::new();
    for hit in ranked_hits {
        let source_name = hit.source_name;
        let source_rank = hit.source_rank;
        let mut paper = Some(hit.paper);
        for group in &mut groups {
            if let Some(ref p) = paper {
                if group.papers.iter().any(|gp| is_duplicate(gp, p)) {
                    group.papers.push(paper.take().unwrap());
                    group.source_ranks.push((source_name, source_rank));
                    break;
                }
            }
        }
        if let Some(p) = paper {
            groups.push(DuplicateGroup {
                papers: vec![p],
                source_ranks: vec![(source_name, source_rank)],
            });
        }
    }
    groups
}

fn is_duplicate(a: &ApiPaper, b: &ApiPaper) -> bool {
    if identifier_overlap(a, b) {
        return true;
    }
    is_same_paper(a, b, 1)
}

fn identifier_overlap(a: &ApiPaper, b: &ApiPaper) -> bool {
    fn same(a: Option<&str>, b: Option<&str>) -> bool {
        match (a, b) {
            (Some(x), Some(y)) => normalize_id(x) == normalize_id(y),
            _ => false,
        }
    }
    same(a.identifiers.doi.as_deref(), b.identifiers.doi.as_deref())
        || same(
            a.identifiers.arxiv_id.as_deref(),
            b.identifiers.arxiv_id.as_deref(),
        )
        || same(a.identifiers.pmid.as_deref(), b.identifiers.pmid.as_deref())
        || same(a.identifiers.isbn.as_deref(), b.identifiers.isbn.as_deref())
}

struct MergedCandidate {
    paper: ApiPaper,
    rrf: f64,
}

struct ScoredCandidate {
    paper: ApiPaper,
    score: f64,
    title_sim: i32,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::features::scholar_api::{PaperIdentifiers, PaperUrls};

    fn paper(
        source: &'static str,
        title: &str,
        authors: &[&str],
        year: Option<i32>,
        identifiers: PaperIdentifiers,
    ) -> ApiPaper {
        ApiPaper {
            title: title.into(),
            authors: authors.iter().map(|s| (*s).to_string()).collect(),
            year,
            date: year.map(|y| y.to_string()),
            venue: None,
            volume: None,
            issue: None,
            pages: None,
            publisher: None,
            abstract_text: None,
            language: None,
            citation_count: None,
            identifiers,
            urls: PaperUrls::default(),
            source,
            raw: None,
        }
    }

    #[test]
    fn source_scope_expands_and_sorts_by_priority() {
        let scholarly = SourceScope::Scholarly.sources();
        assert_eq!(scholarly.len(), 3);
        assert_eq!(scholarly[0].name(), "s2");
        assert_eq!(scholarly[1].name(), "crossref");
        assert_eq!(scholarly[2].name(), "openalex");

        let open_access = SourceScope::OpenAccess.sources();
        assert_eq!(open_access.len(), 2);
        assert_eq!(open_access[0].name(), "arxiv");
        assert_eq!(open_access[1].name(), "s2");
    }

    #[test]
    fn custom_scope_filters_and_sorts_by_priority() {
        let custom = SourceScope::Custom(vec!["crossref".into(), "arxiv".into()]).sources();
        assert_eq!(custom.len(), 2);
        assert_eq!(custom[0].name(), "crossref");
        assert_eq!(custom[1].name(), "arxiv");

        let unknown = SourceScope::Custom(vec!["not-a-source".into()]).sources();
        assert!(unknown.is_empty());
    }

    #[test]
    fn fuse_prefers_exact_title_match() {
        let hits = vec![
            paper(
                "s2",
                "Not Attention",
                &[],
                None,
                PaperIdentifiers::default(),
            ),
            paper(
                "s2",
                "Attention Is All You Need",
                &["Vaswani"],
                Some(2017),
                PaperIdentifiers::default(),
            ),
        ];
        let ranked = fuse_and_rank_candidates(hits, "attention is all you need", 2);
        assert_eq!(ranked[0].title, "Attention Is All You Need");
    }

    #[test]
    fn fuse_filters_below_threshold() {
        let hits = vec![paper(
            "s2",
            "Completely Unrelated",
            &[],
            None,
            PaperIdentifiers::default(),
        )];
        let ranked = fuse_and_rank_candidates(hits, "attention is all you need", 5);
        assert!(ranked.is_empty());
    }

    #[test]
    fn fuse_deduplicates_by_identifier_and_keeps_higher_priority() {
        let hits = vec![
            paper(
                "s2",
                "Attention Is All You Need",
                &["Vaswani"],
                Some(2017),
                PaperIdentifiers {
                    arxiv_id: Some("1706.03762".into()),
                    ..Default::default()
                },
            ),
            paper(
                "crossref",
                "Attention is all you need",
                &["Vaswani, Ashish"],
                Some(2017),
                PaperIdentifiers {
                    doi: Some("10.1/attention".into()),
                    ..Default::default()
                },
            ),
        ];
        let ranked = fuse_and_rank_candidates(hits, "attention is all you need", 5);
        assert_eq!(ranked.len(), 1);
        // S2 has higher priority, so it becomes the base source.
        assert_eq!(ranked[0].source, "s2");
        // Crossref's DOI is merged in.
        assert_eq!(ranked[0].identifiers.doi.as_deref(), Some("10.1/attention"));
    }

    #[test]
    fn rank_candidates_alias_floats_exact_match() {
        let hits = vec![
            paper(
                "s2",
                "Not Attention",
                &[],
                None,
                PaperIdentifiers::default(),
            ),
            paper(
                "s2",
                "Attention Is All You Need",
                &["Vaswani"],
                Some(2017),
                PaperIdentifiers::default(),
            ),
        ];
        let ranked = rank_candidates(hits, "attention is all you need", 2);
        assert_eq!(ranked[0].title, "Attention Is All You Need");
    }

    #[test]
    fn empty_query_returns_empty() {
        let ranked = fuse_and_rank_candidates(vec![], "query", 5);
        assert!(ranked.is_empty());
    }

    fn failure(source: &'static str, kind: FailureKind) -> SearchFailure {
        SearchFailure { source, kind }
    }

    #[test]
    fn failure_kind_classifies_rate_limit() {
        assert_eq!(
            failure_kind(&ApiError::RateLimited),
            FailureKind::RateLimited
        );
        assert_eq!(
            failure_kind(&ApiError::Network("boom".into())),
            FailureKind::Other("network: boom".into())
        );
    }

    #[test]
    fn all_sources_rate_limited_yields_rate_limited_error() {
        let failures = vec![
            failure("s2", FailureKind::RateLimited),
            failure("crossref", FailureKind::RateLimited),
            failure("arxiv", FailureKind::RateLimited),
        ];
        assert!(matches!(
            search_failure_error(&failures, 3, 0),
            Some(ApiError::RateLimited)
        ));
    }

    #[test]
    fn mixed_rate_limited_and_network_prefers_rate_limited() {
        let failures = vec![
            failure("s2", FailureKind::Timeout),
            failure("crossref", FailureKind::RateLimited),
        ];
        assert!(matches!(
            search_failure_error(&failures, 2, 0),
            Some(ApiError::RateLimited)
        ));
    }

    #[test]
    fn all_sources_failed_without_rate_limit_yields_network_summary() {
        let failures = vec![
            failure("s2", FailureKind::Timeout),
            failure("crossref", FailureKind::Other("network: boom".into())),
        ];
        match search_failure_error(&failures, 2, 0) {
            Some(ApiError::Network(msg)) => {
                assert!(msg.contains("2"), "summary names the count: {msg}");
                assert!(msg.contains("s2") && msg.contains("crossref"), "{msg}");
            }
            other => panic!("expected network summary, got {other:?}"),
        }
    }

    #[test]
    fn one_source_answering_empty_suppresses_error() {
        // Two of three sources failed; one answered with zero hits — an
        // authoritative "no such paper" wins over a rate-limit guess.
        let failures = vec![
            failure("s2", FailureKind::RateLimited),
            failure("crossref", FailureKind::RateLimited),
        ];
        assert!(search_failure_error(&failures, 3, 0).is_none());
    }

    #[test]
    fn hits_present_suppresses_error() {
        let failures = vec![
            failure("s2", FailureKind::RateLimited),
            failure("crossref", FailureKind::RateLimited),
        ];
        assert!(search_failure_error(&failures, 2, 1).is_none());
    }

    #[test]
    fn nothing_attempted_suppresses_error() {
        assert!(search_failure_error(&[], 0, 0).is_none());
    }
}
