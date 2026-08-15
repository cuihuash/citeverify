"""Cautious first-pass citation verification.

This module intentionally reports evidence, not a fake/real verdict:

* DOI-bearing references are checked by resolving DOI metadata through Crossref.
* Books/reports are checked separately through Open Library.
* A failed lookup becomes ``unverified`` or ``unable_to_check``, never fake.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


USER_AGENT = "CiteVerify/0.1.2 (research reference verification)"
DOI_URL = "https://api.crossref.org/works/{}"
OPEN_LIBRARY_URL = "https://openlibrary.org/search.json?{}"
GOOGLE_BOOKS_URL = "https://www.googleapis.com/books/v1/volumes?{}"
OPENALEX_URL = "https://api.openalex.org/works/{}"
SEMANTIC_SCHOLAR_URL = "https://api.semanticscholar.org/graph/v1/paper/{}?{}"


def read_api_key_file(path: Path | None) -> str | None:
    """Read a single API key without ever including it in output data."""
    if path is None:
        return None
    value = path.read_text(encoding="utf-8-sig").strip()
    return value or None


def normalize(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def title_score(left: str, right: str) -> float:
    a, b = normalize(left), normalize(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def title_match_kind(left: str, right: str) -> tuple[str, float]:
    """Compare titles while allowing legitimate subtitle/registry variants."""
    score = title_score(left, right)
    left_norm, right_norm = normalize(left), normalize(right)
    if not left_norm or not right_norm:
        return "none", score

    # DOI registries often omit subtitles while the citation includes them,
    # or vice versa. A normalized containment match is strong evidence.
    if len(left_norm) >= 24 and (left_norm in right_norm or right_norm in left_norm):
        return "strong", score

    left_words, right_words = set(left_norm.split()), set(right_norm.split())
    shorter, longer = (left_words, right_words) if len(left_words) <= len(right_words) else (right_words, left_words)
    overlap = len(shorter & longer) / max(1, len(shorter))
    if score >= 0.82 or overlap >= 0.80:
        return "strong", score
    if overlap >= 0.55:
        return "variant", score
    return "none", score


def first_author_hint(raw: str) -> str | None:
    """Extract a lightweight author hint for book catalog search.

    This is deliberately only a query hint. It is not used to reject a title
    match because author formatting in PDF references varies widely.
    """
    year = re.search(r"\((?:18|19|20)\d{2}[a-z]?\)\.", raw)
    if not year:
        return None
    prefix = raw[: year.start()]
    prefix = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", prefix)
    prefix = re.sub(r"\s+", " ", prefix).strip(" ,.;")
    if not prefix or "http" in prefix.casefold():
        return None
    # The first author is normally before the first comma; preserve compound
    # surnames such as "Delli Carpini" when the citation uses them.
    first = prefix.split(",", 1)[0].strip()
    return first or None


def fetch_json(
    url: str,
    timeout: float = 8.0,
    accept: str = "application/json",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any] | None, str | None]:
    request_headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body), None
    except HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return 0, None, str(exc)


def best_title_candidate(item: dict[str, Any]) -> str | None:
    candidates = item.get("title_candidates") or []
    if not candidates:
        return None

    # The parser emits several hypotheses. For database lookup, prefer a title
    # taken after the author/year boundary. `after_author` is useful for books,
    # but is a poor choice for author-year articles because it can return the
    # tail of the author list (the bug seen in reference 35).
    priority = {
        "before_in": 0,
        "after_year": 1,
        "quoted": 2,
        "after_author": 3,
        "sentence_heuristic": 4,
    }
    ordered = sorted(
        candidates,
        key=lambda candidate: (priority.get(candidate.get("method", ""), 9), -candidate.get("confidence", 0)),
    )
    return ordered[0].get("title")


def _metadata(payload: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]], str | None]:
    """Extract title/authors/url from Crossref or DOI-resolver JSON."""
    record = payload.get("message") if isinstance(payload.get("message"), dict) else payload
    titles = record.get("title") or []
    if isinstance(titles, str):
        title = titles
    elif isinstance(titles, list):
        title = titles[0] if titles else None
    else:
        title = None
    authors = record.get("author") or []
    url = record.get("URL") or record.get("url")
    return title, authors, url


def _doi_result(
    source: str,
    doi: str,
    payload: dict[str, Any],
    title: str | None,
) -> dict[str, Any]:
    matched_title, authors, record_url = _metadata(payload)
    match_kind, score = title_match_kind(title or "", matched_title or "")
    url = record_url or f"https://doi.org/{doi}"

    if matched_title and match_kind == "strong":
        return {
            "status": "verified",
            "source": source,
            "doi": doi,
            "matched_title": matched_title,
            "title_score": round(score, 3),
            "url": url,
            "matched_authors": [
                " ".join(part for part in [a.get("given"), a.get("family")] if part)
                for a in authors
                if isinstance(a, dict) and (a.get("given") or a.get("family"))
            ],
            "note": "DOI metadata resolves and the title matches; author parsing is not used to reject this result.",
        }

    if matched_title and match_kind == "variant":
        return {
            "status": "verified_title_variant",
            "source": source,
            "doi": doi,
            "matched_title": matched_title,
            "title_score": round(score, 3),
            "url": url,
            "note": "DOI metadata resolves and shares substantial title wording; manually review the title variant.",
        }

    return {
        "status": "conflict",
        "source": source,
        "doi": doi,
        "matched_title": matched_title,
        "title_score": round(score, 3),
        "url": url,
        "reason": "doi_resolves_to_different_title",
    }


def _openalex_result(
    doi: str,
    payload: dict[str, Any],
    title: str | None,
) -> dict[str, Any]:
    matched_title = payload.get("display_name") or payload.get("title")
    match_kind, score = title_match_kind(title or "", matched_title or "")
    authors = []
    for authorship in payload.get("authorships") or []:
        author = authorship.get("author") or {}
        if author.get("display_name"):
            authors.append(author["display_name"])
    common = {
        "source": "OpenAlex",
        "doi": doi,
        "matched_title": matched_title,
        "title_score": round(score, 3),
        "url": payload.get("id") or payload.get("doi") or f"https://doi.org/{doi}",
        "matched_authors": authors,
        "is_retracted": payload.get("is_retracted", False),
    }
    if match_kind == "strong":
        return {"status": "verified", **common}
    if match_kind == "variant":
        return {"status": "verified_title_variant", **common}
    return {"status": "conflict", "reason": "doi_resolves_to_different_title", **common}


def _semantic_scholar_result(
    doi: str,
    payload: dict[str, Any],
    title: str | None,
) -> dict[str, Any]:
    matched_title = payload.get("title")
    match_kind, score = title_match_kind(title or "", matched_title or "")
    common = {
        "source": "Semantic Scholar",
        "doi": doi,
        "matched_title": matched_title,
        "title_score": round(score, 3),
        "url": payload.get("url") or f"https://doi.org/{doi}",
        "matched_authors": [
            author.get("name")
            for author in payload.get("authors") or []
            if author.get("name")
        ],
    }
    if match_kind == "strong":
        return {"status": "verified", **common}
    if match_kind == "variant":
        return {"status": "verified_title_variant", **common}
    return {"status": "conflict", "reason": "doi_resolves_to_different_title", **common}


def _crosscheck_doi(
    doi: str,
    title: str | None,
    openalex_key: str | None,
    semantic_scholar_key: str | None,
) -> list[dict[str, Any]]:
    """Query independent scholarly indexes by DOI when configured."""
    results: list[dict[str, Any]] = []
    if openalex_key:
        # OpenAlex accepts an external DOI as the work identifier. Preserve
        # the DOI URL scheme while encoding any DOI characters that belong in
        # the path.
        url = OPENALEX_URL.format(quote(f"https://doi.org/{doi}", safe=":/"))
        url += "?" + urlencode({"api_key": openalex_key})
        status, payload, error = fetch_json(url)
        if payload is not None:
            results.append(_openalex_result(doi, payload, title))
        else:
            results.append({"status": "unavailable", "source": "OpenAlex", "http_status": status, "error": error})

    if semantic_scholar_key:
        params = urlencode({"fields": "title,authors,url,externalIds"})
        url = SEMANTIC_SCHOLAR_URL.format(quote(f"DOI:{doi}", safe=":/"), params)
        status, payload, error = fetch_json(url, headers={"x-api-key": semantic_scholar_key})
        if payload is not None:
            results.append(_semantic_scholar_result(doi, payload, title))
        else:
            results.append({"status": "unavailable", "source": "Semantic Scholar", "http_status": status, "error": error})
    return results


def verify_doi(
    item: dict[str, Any],
    title: str | None,
    openalex_key: str | None = None,
    semantic_scholar_key: str | None = None,
) -> dict[str, Any]:
    doi = item.get("doi")
    if not doi:
        return {"status": "not_checked", "reason": "no_doi"}

    attempts: list[dict[str, Any]] = []
    crosscheck_results: list[dict[str, Any]] = []
    crossref_status, crossref_payload, crossref_error = fetch_json(
        DOI_URL.format(quote(doi, safe="/"))
    )
    if crossref_payload is not None:
        result = _doi_result("Crossref", doi, crossref_payload, title)
        result["attempts"] = [{"source": "Crossref", "status": result["status"]}]
        crosscheck_results = _crosscheck_doi(doi, title, openalex_key, semantic_scholar_key)
        return _combine_doi_evidence(result, crosscheck_results)
    attempts.append(
        {"source": "Crossref", "http_status": crossref_status, "error": crossref_error}
    )

    # Crossref may lag, reject a request, or not carry a record that the DOI
    # registry/publisher can resolve. Retry the DOI itself using CSL JSON.
    resolver_status, resolver_payload, resolver_error = fetch_json(
        f"https://doi.org/{quote(doi, safe='/')}",
        accept="application/vnd.citationstyles.csl+json",
    )
    if resolver_payload is not None:
        result = _doi_result("DOI resolver", doi, resolver_payload, title)
        result["attempts"] = attempts + [{"source": "DOI resolver", "status": result["status"]}]
        crosscheck_results = _crosscheck_doi(doi, title, openalex_key, semantic_scholar_key)
        return _combine_doi_evidence(result, crosscheck_results)
    attempts.append(
        {"source": "DOI resolver", "http_status": resolver_status, "error": resolver_error}
    )

    crosscheck_results = _crosscheck_doi(doi, title, openalex_key, semantic_scholar_key)
    if crossref_status == 404 and resolver_status == 404:
        result = {
            "status": "conflict",
            "source": "Crossref + DOI resolver",
            "reason": "doi_not_found",
            "doi": doi,
            "attempts": attempts,
        }
        return _combine_doi_evidence(result, crosscheck_results)
    result = {
        "status": "unable_to_check",
        "source": "Crossref + DOI resolver",
        "reason": resolver_error or crossref_error or "lookup_failed",
        "doi": doi,
        "attempts": attempts,
    }
    return _combine_doi_evidence(result, crosscheck_results)


def _combine_doi_evidence(
    primary: dict[str, Any],
    crosschecks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Let an independent index rescue a bad/incomplete primary response."""
    available = [result for result in crosschecks if result.get("status") in {"verified", "verified_title_variant", "conflict"}]
    strong = [result for result in available if result.get("status") == "verified"]
    variants = [result for result in available if result.get("status") == "verified_title_variant"]
    if strong:
        sources = [primary.get("source")] + [result.get("source") for result in strong]
        rescued = dict(strong[0])
        rescued["status"] = "verified"
        rescued["evidence_sources"] = [source for source in sources if source]
        rescued["primary_result"] = primary
        rescued["crosschecks"] = crosschecks
        return rescued
    if variants and primary.get("status") not in {"verified", "verified_title_variant"}:
        rescued = dict(variants[0])
        rescued["status"] = "verified_title_variant"
        rescued["evidence_sources"] = [result.get("source") for result in variants if result.get("source")]
        rescued["primary_result"] = primary
        rescued["crosschecks"] = crosschecks
        return rescued
    if crosschecks:
        primary = dict(primary)
        primary["crosschecks"] = crosschecks
    return primary


def verify_book(item: dict[str, Any], title: str | None) -> dict[str, Any]:
    if not title:
        return {"status": "unable_to_check", "reason": "no_title_candidate"}

    author = first_author_hint(item.get("raw_citation", ""))
    attempts: list[dict[str, Any]] = []
    queries = [
        {"title": title, "author": author},
        {"title": title},
    ]
    best: dict[str, Any] | None = None
    best_score = 0.0
    for query in queries:
        params = {
            key: value
            for key, value in {
                **query,
                "limit": "20",
                "fields": "title,author_name,key,first_publish_year,isbn",
            }.items()
            if value
        }
        status, payload, error = fetch_json(OPEN_LIBRARY_URL.format(urlencode(params)))
        attempts.append({"source": "Open Library", "query": query, "http_status": status, "error": error})
        if payload is None:
            continue
        for doc in payload.get("docs") or []:
            found_title = doc.get("title") or ""
            score = title_score(title, found_title)
            if score > best_score:
                best, best_score = doc, score

        if best_score >= 0.82:
            break

    if best is not None and best_score >= 0.82:
        key = best.get("key")
        return {
            "status": "verified",
            "source": "Open Library",
            "matched_title": best.get("title"),
            "title_score": round(best_score, 3),
            "matched_authors": best.get("author_name") or [],
            "year": best.get("first_publish_year"),
            "url": f"https://openlibrary.org{key}" if key else None,
            "note": "Book title found in a book catalog; edition differences may remain.",
            "attempts": attempts,
        }

    # Google Books is a useful second catalog for publisher-issued books that
    # Open Library has not indexed or has indexed under a different edition.
    google_query = f"intitle:{title}"
    if author:
        google_query += f" inauthor:{author}"
    google_status, google_payload, google_error = fetch_json(
        GOOGLE_BOOKS_URL + urlencode({"q": google_query, "maxResults": "20"})
    )
    attempts.append(
        {"source": "Google Books", "query": google_query, "http_status": google_status, "error": google_error}
    )
    if google_payload is not None:
        for volume in google_payload.get("items") or []:
            info = volume.get("volumeInfo") or {}
            found_title = info.get("title") or ""
            score = title_score(title, found_title)
            if score >= 0.82:
                return {
                    "status": "verified",
                    "source": "Google Books",
                    "matched_title": found_title,
                    "title_score": round(score, 3),
                    "matched_authors": info.get("authors") or [],
                    "year": (info.get("publishedDate") or "")[:4] or None,
                    "url": info.get("infoLink"),
                    "note": "Book title found in Google Books; edition differences may remain.",
                    "attempts": attempts,
                }

    return {
        "status": "unverified",
        "source": "Open Library + Google Books",
        "reason": "no_sufficient_title_match",
        "query_title": title,
        "author_hint": author,
        "attempts": attempts,
    }


def verify_item(
    item: dict[str, Any],
    pause: float,
    openalex_key: str | None,
    semantic_scholar_key: str | None,
    sequence_number: int,
) -> dict[str, Any]:
    title = best_title_candidate(item)
    item_type = item.get("citation_type")
    if item.get("doi"):
        result = verify_doi(item, title, openalex_key, semantic_scholar_key)
    elif item_type == "book_or_report":
        result = verify_book(item, title)
    else:
        result = {
            "status": "unverified",
            "reason": "no_strong_identifier_or_specialized_lookup",
            "note": "No lookup was attempted; absence of a match is not evidence of fabrication.",
        }
    if pause:
        time.sleep(pause)
    return {
        # Author-year bibliographies usually have no printed numbers. Use the
        # extraction order so every result can be located in the PDF.
        "reference_number": item.get("number") or sequence_number,
        "raw_citation": item.get("raw_citation"),
        "processed_citation": item.get("cleaned_citation") or item.get("raw_citation"),
        "parser_confidence": item.get("parser_confidence"),
        "citation_type": item_type,
        "title_queried": title,
        "doi": item.get("doi"),
        "isbn": item.get("isbn"),
        "verification": result,
    }


def _human_reason(verification: dict[str, Any]) -> str:
    status = verification.get("status")
    reason = verification.get("reason")
    reasons = {
        "no_strong_identifier_or_specialized_lookup": (
            "No DOI or other strong identifier was found, so no specialized lookup was attempted."
        ),
        "no_sufficient_title_match": (
            "No sufficiently close title match was found in the searched book catalogs."
        ),
        "doi_not_found": "The DOI was not found by the configured DOI services.",
        "doi_resolves_to_different_title": (
            "The DOI resolves, but the returned title does not sufficiently match the citation title."
        ),
        "lookup_failed": "The lookup service returned an error or was unavailable.",
        "no_title_candidate": "No usable title was extracted from the reference.",
    }
    if reason in reasons:
        return reasons[reason]
    if status == "verified":
        return "A lookup source returned a strong title match."
    if status == "verified_title_variant":
        return "A lookup source returned a substantial title match with wording differences."
    if status == "unavailable":
        return "The lookup service was unavailable or rate-limited."
    return reason or "No verification explanation was recorded."


def render_html_report(
    source: str | None,
    results: list[dict[str, Any]],
) -> str:
    # Put items needing attention first while retaining their original PDF
    # reference numbers for easy cross-checking.
    status_order = {
        "conflict": 0,
        "unverified": 1,
        "unable_to_check": 2,
        "verified_title_variant": 3,
        "verified": 4,
    }
    results = sorted(
        results,
        key=lambda result: (
            status_order.get(result["verification"].get("status", "unknown"), 5),
            result.get("reference_number") or 0,
        ),
    )
    counts: dict[str, int] = {}
    for result in results:
        status = result["verification"].get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1

    summary = " ".join(
        f'<span class="summary-item {html.escape(status)}">'
        f"{html.escape(status.replace('_', ' ').title())}: {count}</span>"
        for status, count in sorted(counts.items())
    )
    cards: list[str] = []
    for result in results:
        verification = result["verification"]
        status = verification.get("status", "unknown")
        status_class = html.escape(status.replace("_", "-"))
        status_label = html.escape(status.replace("_", " ").title())
        doi = result.get("doi")
        if doi:
            doi_url = f"https://doi.org/{quote(doi, safe='/')}"
            doi_html = (
                f'<a href="{html.escape(doi_url, quote=True)}" target="_blank" '
                f'rel="noopener">{html.escape(doi)}</a>'
            )
        else:
            doi_html = "No DOI found in the parsed reference."

        raw = html.escape(result.get("raw_citation") or "")
        processed = html.escape(result.get("processed_citation") or "")
        matched_title = verification.get("matched_title")
        evidence = verification.get("evidence_sources") or ([verification.get("source")] if verification.get("source") else [])
        evidence_text = ", ".join(str(value) for value in evidence if value) or "Not available"
        matched_authors = verification.get("matched_authors") or []
        crosschecks = verification.get("crosschecks") or []
        crosscheck_html = ""
        if crosschecks:
            crosscheck_items = []
            for crosscheck in crosschecks:
                source = html.escape(str(crosscheck.get("source") or "Unknown source"))
                cross_status = html.escape(str(crosscheck.get("status") or "unknown").replace("_", " ").title())
                detail = crosscheck.get("matched_title") or crosscheck.get("error") or crosscheck.get("reason") or ""
                crosscheck_items.append(
                    f"<li><strong>{source}:</strong> {cross_status}"
                    + (f" — {html.escape(str(detail))}" if detail else "")
                    + "</li>"
                )
            crosscheck_html = "<p><strong>Independent scholarly cross-checks:</strong></p><ul>" + "".join(crosscheck_items) + "</ul>"
        reason = html.escape(_human_reason(verification))
        matched_html = (
            f'<p><strong>Matched record:</strong> {html.escape(str(matched_title))}</p>'
            if matched_title
            else ""
        )
        authors_html = (
            f'<p><strong>Matched authors:</strong> {html.escape("; ".join(str(author) for author in matched_authors))}</p>'
            if matched_authors
            else ""
        )
        cards.append(
            f"""<article class="reference-card">
<div class="reference-heading"><h2>Reference {result.get('reference_number')}</h2>
<span class="status {status_class}">{status_label}</span></div>
<p><strong>Full processed reference:</strong></p>
<div class="citation">{processed}</div>
<details><summary>Show text exactly as extracted from the PDF</summary>
<div class="citation">{raw}</div></details>
<p><strong>DOI:</strong> {doi_html}</p>
<p><strong>Evidence source:</strong> {html.escape(evidence_text)}</p>
{crosscheck_html}
{matched_html}
{authors_html}
<p><strong>Explanation:</strong> {reason}</p>
</article>"""
        )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CiteVerify report</title>
<style>
body {{ font-family: Arial, sans-serif; line-height: 1.45; color: #1f2937; max-width: 1100px; margin: 0 auto; padding: 24px; background: #f6f7f9; }}
h1 {{ margin-bottom: 6px; }}
.source {{ color: #4b5563; overflow-wrap: anywhere; }}
.summary {{ display: flex; flex-wrap: wrap; gap: 8px; margin: 20px 0; }}
.summary-item, .status {{ display: inline-block; border-radius: 999px; padding: 5px 10px; font-weight: 700; font-size: 0.9rem; }}
.verified {{ background: #dcfce7; color: #166534; }}
.unverified {{ background: #fef3c7; color: #92400e; }}
.conflict, .unable-to-check {{ background: #fee2e2; color: #991b1b; }}
.verified-title-variant {{ background: #dbeafe; color: #1e40af; }}
.reference-card {{ background: white; border: 1px solid #d1d5db; border-radius: 10px; padding: 18px; margin: 16px 0; box-shadow: 0 1px 2px #0000000d; }}
.reference-heading {{ display: flex; align-items: center; justify-content: space-between; gap: 12px; }}
h2 {{ font-size: 1.1rem; margin: 0; }}
.citation {{ white-space: pre-wrap; overflow-wrap: anywhere; background: #f9fafb; border-left: 3px solid #9ca3af; padding: 10px 12px; }}
details {{ margin: 12px 0; }}
a {{ color: #075985; }}
</style></head><body>
<h1>CiteVerify report</h1>
<p class="source"><strong>Source PDF:</strong> {html.escape(source or "Unknown")}</p>
<p>This report presents lookup evidence. “Unverified” does not mean fabricated.</p>
<div class="summary">{summary}</div>
{''.join(cards)}
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cautiously verify citation diagnostics.")
    parser.add_argument("diagnostics", type=Path, help="JSON created by citeverify_parser.py")
    parser.add_argument("--output", type=Path, help="Write verification results as JSON")
    parser.add_argument("--report", type=Path, help="Write a human-readable HTML report")
    parser.add_argument("--pause", type=float, default=0.2, help="Seconds between requests (default: 0.2)")
    parser.add_argument("--limit", type=int, help="Only check the first N references (useful for a quick test)")
    parser.add_argument("--openalex-key", default=os.environ.get("OPENALEX_KEY"), help="OpenAlex API key (or OPENALEX_KEY)")
    parser.add_argument("--s2-api-key", default=os.environ.get("S2_API_KEY"), help="Semantic Scholar API key (or S2_API_KEY)")
    parser.add_argument("--openalex-key-file", type=Path, help="Read the OpenAlex API key from a local file")
    parser.add_argument("--s2-api-key-file", type=Path, help="Read the Semantic Scholar API key from a local file")
    args = parser.parse_args(argv)

    try:
        document = json.loads(args.diagnostics.read_text(encoding="utf-8"))
        openalex_key = args.openalex_key or read_api_key_file(args.openalex_key_file)
        semantic_scholar_key = args.s2_api_key or read_api_key_file(args.s2_api_key_file)
        items = document.get("diagnostics", [])
        if args.limit is not None:
            items = items[: max(0, args.limit)]
        results = [
            verify_item(item, max(0.0, args.pause), openalex_key, semantic_scholar_key, index)
            for index, item in enumerate(items, start=1)
        ]
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output = {
        "source": document.get("source"),
        "reference_count": len(results),
        "warning": "Statuses describe verification evidence, not whether a citation is fake.",
        "results": results,
    }
    destination = args.output or args.diagnostics.with_name("citation-verification.json")
    destination.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    report_destination = args.report or destination.with_suffix(".html")
    report_destination.write_text(
        render_html_report(output.get("source"), results),
        encoding="utf-8",
    )

    counts: dict[str, int] = {}
    for result in results:
        status = result["verification"]["status"]
        counts[status] = counts.get(status, 0) + 1
    print(f"Checked {len(results)} references from {document.get('source')}")
    for status, count in sorted(counts.items()):
        print(f"  {status}: {count}")
    print(f"Saved: {destination}")
    print(f"Saved report: {report_destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
