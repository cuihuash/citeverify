"""CiteVerify citation parsing diagnostics.

This deliberately stops before database lookup. Its job is to make PDF parsing
uncertainty visible instead of turning a bad parse into a false hallucination.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - exercised by the command-line error path
    fitz = None


REFERENCE_HEADERS = re.compile(
    r"(?im)^\s*(?:references and bibliography|literature cited|references|bibliography)"
    r"(?:\s*(?:\((?:continued|cont\.?|2)\)|[-–—]\s*continued))?\s*:?[ \t]*$"
)
PDF_REFERENCE_START = "@@REF_START@@"
NEXT_SECTION = re.compile(
    r"(?im)^\s*(?:@@REF_START@@)?\s*(?:appendix|appendices|acknowledg(?:e)?ments|supplementary material|supplementary file|author contributions|author biographies|author biography|conflict of interest)\b.*$"
)
IEEE_START = re.compile(r"(?m)^\s*\[(\d{1,4})\]\s+")
NUMBERED_START = re.compile(r"(?m)^\s*(\d{1,3})[.)]\s+")
AUTHOR_YEAR_START = re.compile(
    # Some references have a long author list that wraps across several PDF
    # lines. Allow enough room for those lists so a repeated lead author does
    # not get merged into the preceding reference. Include lowercase name
    # particles such as `dos Santos` and `van der Waals`.
    r"(?m)^(?=(?:(?:[A-Z]|(?:[a-z]{1,4}\s+){1,3}[A-Z])[^\n]{0,500}\((?:18|19|20)\d{2}[a-z]?\)(?:\.)?\s)"
    r"|(?:(?:[A-Z]|(?:[a-z]{1,4}\s+){1,3}[A-Z])[^\n]{0,500}\n\s*\((?:18|19|20)\d{2}[a-z]?\)(?:\.)?\s))"
)
YEAR = re.compile(r"\b(?:18|19|20)\d{2}\b")
DOI = re.compile(r"\b10\.\d{4,9}/(?:[^\s<>\"']|(?<=-)\s+)+", re.I)
ARXIV = re.compile(r"\barXiv\s*:\s*([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)\b", re.I)
ISBN = re.compile(
    r"\bISBN(?:-1[03])?\s*[:#]?\s*((?:\d[ -]?){9,16}[\dXx])\b"
    r"|\b(97[89](?:[ -]?\d){10})\b",
    re.I,
)
QUOTED_TITLE = re.compile(r'["“”„‟](.+?)["“”„‟]', re.S)

# These patterns are intentionally conservative. We remove explicit labels,
# not every number, because numbers can be part of real titles (1984, 3D, etc.).
ARTIFACT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "page_or_line_label",
        re.compile(
            r"\b(?:p|pp|page|pages|line|lines|para|paragraph|paragraphs)\.?\s*\d+(?:\s*[-–—]\s*\d+)?\b",
            re.I,
        ),
    ),
    (
        "page_range",
        # Require the range not to be embedded in an identifier such as an
        # ISBN (978-1-234-56789-7). Explicit `pp.`/`p.` ranges are handled by
        # the label pattern above.
        re.compile(r"(?<![\w./-])\d{1,4}\s*[-–—]\s*\d{1,4}(?![\w./-])"),
    ),
    (
        "line_marker",
        re.compile(r"\b(?:l|ll)\.?\s*(?!18|19|20)\d+(?:\s*[-–—]\s*\d+)?\b", re.I),
    ),
)


@dataclass
class TitleCandidate:
    title: str
    method: str
    confidence: float


@dataclass
class ReferenceDiagnostic:
    number: int | None
    raw_citation: str
    cleaned_citation: str
    title_candidates: list[TitleCandidate] = field(default_factory=list)
    doi: str | None = None
    isbn: str | None = None
    arxiv_id: str | None = None
    citation_type: str = "unknown"
    artifact_flags: list[str] = field(default_factory=list)
    parser_confidence: str = "low"
    parser_notes: list[str] = field(default_factory=list)


@dataclass
class DocumentDiagnostics:
    source: str
    references_header: str | None
    reference_count: int
    diagnostics: list[ReferenceDiagnostic]
    warnings: list[str] = field(default_factory=list)


def extract_pdf_text(path: Path) -> str:
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed. Install it with: python -m pip install PyMuPDF")
    with fitz.open(path) as document:
        pages: list[str] = []
        for page_number, page in enumerate(document, start=1):
            # Keep a small amount of layout information from the PDF. In many
            # reference lists, a wrapped line is indented while a new record
            # starts at the left body margin. This prevents long repeated
            # author lists from being merged with the previous reference.
            # Store both the line's own x-position and its containing text
            # block's x-position. Italic text and hanging indents can move a
            # line within a block, while the block position remains a stable
            # indicator of the left or right column.
            lines: list[tuple[float, float, str, float]] = []
            page_height = page.rect.height
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    text = "".join(span.get("text", "") for span in line.get("spans", [])).strip()
                    if text:
                        lines.append(
                            (
                                line["bbox"][1],
                                line["bbox"][0],
                                text,
                                block["bbox"][0],
                            )
                        )

            # Some publishers store a reference number in a separate text
            # object from the citation itself. Join those objects when they
            # share a baseline; otherwise `1.` and its authors can be split
            # into separate pseudo-references.
            lines.sort(key=lambda entry: (entry[0], entry[1]))
            number_pairs: dict[int, int] = {}
            paired_indices: set[int] = set()
            for index, (y, x, text, _block_x) in enumerate(lines):
                if not re.fullmatch(r"(?:\[\d{1,4}\]|\d{1,4}[.)])", text):
                    continue
                nearby = [
                    candidate
                    for candidate in range(max(0, index - 3), min(len(lines), index + 4))
                    if candidate != index
                    and candidate not in paired_indices
                    and not re.fullmatch(
                        r"(?:\[\d{1,4}\]|\d{1,4}[.)])", lines[candidate][2]
                    )
                    and abs(lines[candidate][0] - y) <= 16
                    and lines[candidate][1] > x
                    and lines[candidate][1] - x < 40
                ]
                if nearby:
                    candidate = min(nearby, key=lambda item: abs(lines[item][0] - y))
                    number_pairs[index] = candidate
                    paired_indices.update({index, candidate})

            merged_lines: list[tuple[float, float, str, float]] = []
            index = 0
            while index < len(lines):
                y, x, text, block_x = lines[index]
                if index in number_pairs:
                    candidate = number_pairs[index]
                    candidate_y, candidate_x, candidate_text, candidate_block_x = lines[candidate]
                    merged_lines.append(
                        (
                            min(y, candidate_y),
                            min(x, candidate_x),
                            f"{text} {candidate_text}",
                            min(block_x, candidate_block_x),
                        )
                    )
                    index += 1
                    continue
                if index in paired_indices:
                    index += 1
                    continue
                merged_lines.append((y, x, text, block_x))
                index += 1
            lines = merged_lines

            # PDF text is often stored in visual order rather than reading
            # order. For a two-column reference list, sorting only by y
            # interleaves the left and right columns and makes the parser see
            # only the first column as reference starts. Detect large x gaps
            # and read each column from top to bottom before moving right.
            x_values = sorted({round(block_x, 1) for _, _x, _text, block_x in lines if block_x > 20})
            column_breaks = [
                index
                for index in range(len(x_values) - 1)
                if x_values[index + 1] - x_values[index] >= 40
            ]
            column_boundaries = [
                (x_values[index] + x_values[index + 1]) / 2
                for index in column_breaks
            ]

            def column_index(x: float) -> int:
                return sum(x > boundary for boundary in column_boundaries)

            column_lefts: dict[int, float] = {}
            for _, _x, _text, block_x in lines:
                if block_x > 20:
                    group = column_index(block_x)
                    column_lefts[group] = min(column_lefts.get(group, block_x), block_x)

            lines.sort(key=lambda entry: (column_index(entry[3]), entry[0], entry[1]))
            page_lines: list[str] = []
            for y, x, text, block_x in lines:
                if text.casefold() == "for peer review":
                    continue
                # Running headers in journal PDFs often contain the journal
                # name, issue year, and page range. They can sit in a separate
                # block between the two reference columns, so remove them
                # before column detection rather than letting them look like
                # a reference start.
                if (
                    y < 60
                    and "/" in text
                    and re.search(r"\b(?:18|19|20)\d{2}\)\s+\d{2,4}[a-z]?\d*\b", text)
                ):
                    continue
                # A DOI can wrap onto a line containing only digits (for
                # example, the final `02` of a DOI). Only discard standalone
                # numbers when they are in the page header/footer area.
                if (
                    re.fullmatch(r"(?:page\s+)?\d+(?:\s+of\s+\d+)?", text, re.I)
                    and (y < 60 or y >= page_height - 65)
                ):
                    continue
                if re.search(r"\bdownloaded from\s+https?://", text, re.I):
                    continue
                if re.search(r"\bvolume\s+\d+\s*,\s*issue\s+\d+\b", text, re.I):
                    continue
                # Keep content near the top of a page. In some publisher
                # templates the first reference line begins there, and
                # dropping it loses the authors. Repeated running headers are
                # removed later by _clean_section.
                group = column_index(block_x)
                left_edge = column_lefts.get(group, block_x)
                marker = (
                    ""
                    if REFERENCE_HEADERS.fullmatch(text)
                    or IEEE_START.match(text)
                    or NUMBERED_START.match(text)
                    else PDF_REFERENCE_START if x <= left_edge + 3 else ""
                )
                page_lines.append(marker + text)
            page_text = "\n".join(page_lines)
            pages.append(f"\n<!-- PAGE {page_number} -->\n{page_text}")
        return "\n".join(pages)


def find_references_section(text: str) -> tuple[str | None, str, list[str]]:
    matches = list(REFERENCE_HEADERS.finditer(text))
    if not matches:
        return None, "", ["No References/Bibliography heading was detected."]

    # Proof PDFs may contain a second References section inside a
    # supplementary file. The first exact heading belongs to the main article;
    # selecting the last heading would silently analyze the wrong bibliography.
    header = matches[0].group(0).strip()
    start = matches[0].end()
    remainder = text[start:]

    # References are commonly the final section, but stop before an explicit
    # appendix or acknowledgements heading when one follows them.
    stop = None
    for match in NEXT_SECTION.finditer(remainder):
        if match.start() > 20:
            stop = match.start()
            break
    section = remainder[:stop] if stop is not None else remainder
    return header, section.strip(), []


def _line_is_page_marker(line: str) -> bool:
    compact = re.sub(r"\s+", " ", line.strip())
    return bool(
        re.fullmatch(r"(?:page\s+)?\d+", compact, re.I)
        or re.fullmatch(r"[-–—]?\s*\d+\s*[-–—]?", compact)
    )


def _clean_section(section: str) -> str:
    raw_lines = [line.strip() for line in section.splitlines() if line.strip()]
    line_counts: dict[str, int] = {}
    for line in raw_lines:
        key = re.sub(r"\s+", " ", line.replace(PDF_REFERENCE_START, " ")).strip().casefold()
        line_counts[key] = line_counts.get(key, 0) + 1

    # Repeated short lines are often running headers/footers in publisher
    # proofs. Do not remove publisher names, which legitimately recur in book
    # references and are useful for classification.
    repeated_layout_lines = {
        key
        for key, count in line_counts.items()
        if count >= 2
        and len(key) <= 100
        and "press" not in key
        and "university" not in key
        and "publisher" not in key
    }

    lines = []
    for line in section.splitlines():
        line = line.strip()
        if not line or line.startswith("<!-- PAGE "):
            continue
        if _line_is_page_marker(line):
            # Keep a numeric line when it is the continuation of a DOI or
            # URL split immediately after an underscore (for example, `_`
            # followed by `02`). Other standalone numbers are page markers.
            previous_is_split_identifier = bool(
                lines
                and re.search(r"https?://\S+[./_?&=-]$", lines[-1], re.I)
                and re.fullmatch(r"\d+", line)
                and lines[-1].endswith("_")
            )
            if not previous_is_split_identifier:
                continue
        if re.fullmatch(r"for peer review", line, re.I):
            continue
        if re.fullmatch(r"page\s+\d+\s+of\s+\d+", line, re.I):
            continue
        if REFERENCE_HEADERS.fullmatch(line):
            # A repeated running header such as "References" can appear at
            # the top of every bibliography page. The first heading already
            # delimited the section, so later copies are layout noise.
            continue
        normalized_line = re.sub(
            r"\s+", " ", line.replace(PDF_REFERENCE_START, " " )
        ).strip().casefold()
        if normalized_line in repeated_layout_lines:
            continue
        if lines and lines[-1].endswith("\xad"):
            # Soft hyphens at the end of a PDF line are layout instructions,
            # not part of the title. Join the word across the line break.
            lines[-1] = lines[-1][:-1] + line
            continue
        if (
            lines
            and re.search(r"[A-Za-z]-$", lines[-1])
            and re.match(r"^[a-z]", line)
        ):
            # Some PDFs expose a visible line-break hyphen instead of a soft
            # hyphen. Rejoin it when the next line clearly continues a word.
            lines[-1] = lines[-1][:-1] + line
            continue
        if (
            lines
            and re.search(r"https?://\S+[./_?&=-]$", lines[-1], re.I)
            and re.match(r"^[a-z0-9]", line)
            and (not lines[-1].endswith("_") or re.match(r"^\d", line))
        ):
            # Preserve URLs split at a line boundary, including DOI suffixes
            # split after an underscore, such as `...mcs0301_` followed by
            # `02`.
            lines[-1] += line
            continue
        lines.append(line)
    return "\n".join(lines)


def _numbered_markers_are_plausible(markers: list[re.Match[str]]) -> bool:
    """Reject page/footer numbers that happen to look like `123.` starts."""
    numbers = [int(marker.group(1)) for marker in markers]
    if len(numbers) < 2:
        return False
    sequential = sum(b == a + 1 for a, b in zip(numbers, numbers[1:]))
    increasing = sum(b > a for a, b in zip(numbers, numbers[1:]))
    pair_count = len(numbers) - 1
    return (
        numbers[0] <= 10
        and sequential / pair_count >= 0.6
        or increasing / pair_count >= 0.9
        and sequential / pair_count >= 0.5
    )


def _segment_at_starts(section: str, starts: list[re.Match[str]]) -> list[tuple[int | None, str]]:
    segments: list[tuple[int | None, str]] = []
    for index, match in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(section)
        raw = section[match.start() : end].strip()
        segments.append((None, raw))
    return segments


def segment_references(section: str) -> list[tuple[int | None, str]]:
    section = _clean_section(section)
    if not section:
        return []

    # ``extract_pdf_text`` uses this private marker to preserve a possible
    # left-margin reference start while we are deciding how to segment the
    # list. It must never leak into a user's processed citation.
    plain_section = section.replace(PDF_REFERENCE_START, "")

    # PDF extraction can preserve the left-margin marker inserted by
    # ``extract_pdf_text``. Prefer it over author/year guessing because a
    # wrapped author list can look like a new author/year reference.
    layout_starts = list(
        re.finditer(
            re.escape(PDF_REFERENCE_START)
            + r"(?=[A-Z][^\n]{0,500}\((?:18|19|20)\d{2}(?:[a-z]|,\s*[^)]*)?\)(?:\.)?\s)",
            section,
        )
    )
    author_year = list(AUTHOR_YEAR_START.finditer(plain_section))

    # A PDF may mark only some left-margin starts—for example, after a page
    # break or when the publisher uses slightly different indentation. If we
    # have more author/year starts than layout markers, the sparse markers
    # would merge the remaining references into the last one. In that case,
    # use the author/year starts on the marker-free text instead.
    if len(author_year) >= 2 and len(layout_starts) < len(author_year):
        return _segment_at_starts(plain_section, author_year)

    if len(layout_starts) >= 2:
        segments: list[tuple[int | None, str]] = []
        for index, match in enumerate(layout_starts):
            end = layout_starts[index + 1].start() if index + 1 < len(layout_starts) else len(section)
            raw = section[match.end() : end].replace(PDF_REFERENCE_START, "").strip()
            # A reference list may be followed by figures before the
            # supplementary-file heading. Those later left-margin markers are
            # not references; trim them from the final reference segment.
            if raw:
                segments.append((None, raw))
        if len(segments) >= 2:
            return segments

    numbered = list(IEEE_START.finditer(plain_section))
    if len(numbered) < 2:
        numbered = list(NUMBERED_START.finditer(plain_section))

    if len(numbered) >= 2 and _numbered_markers_are_plausible(numbered):
        segments: list[tuple[int | None, str]] = []
        for index, match in enumerate(numbered):
            end = numbered[index + 1].start() if index + 1 < len(numbered) else len(plain_section)
            raw = plain_section[match.start():end].strip()
            number = int(match.group(1))
            segments.append((number, raw))
        return segments

    if len(author_year) >= 2:
        return _segment_at_starts(plain_section, author_year)

    # If the PDF gave us an explicit layout marker but not enough reliable
    # starts to segment the list, preserve the complete citation. Splitting
    # on blank lines here can turn a wrapped second line into a false
    # reference containing only the citation's tail.
    if PDF_REFERENCE_START in section:
        return [(None, plain_section.strip())]

    # Author-year styles often have blank lines between records. This fallback
    # preserves the whole chunk when a PDF has lost that structure.
    chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n+", plain_section) if chunk.strip()]
    if len(chunks) >= 2 and all(YEAR.search(chunk) or DOI.search(chunk) or ISBN.search(chunk) for chunk in chunks):
        return [(None, chunk) for chunk in chunks]

    # Last resort: one citation per line, but only when lines look citation-like.
    lines = [line.strip() for line in plain_section.splitlines() if line.strip()]
    if len(lines) >= 2:
        return [(None, line) for line in lines]
    return [(None, plain_section.strip())]


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t,;.")


def canonical_isbn(value: str) -> str | None:
    compact = re.sub(r"[^0-9Xx]", "", value).upper()
    if len(compact) in (10, 13):
        return compact
    return None


def extract_identifiers(raw: str) -> tuple[str | None, str | None, str | None]:
    doi = DOI.search(raw)
    doi_value = re.sub(r"\s+", "", doi.group(0)).rstrip(".,;)") if doi else None
    arxiv = ARXIV.search(raw)
    arxiv_value = arxiv.group(1) if arxiv else None

    isbn_value = None
    for match in ISBN.finditer(raw):
        candidate = canonical_isbn(match.group(1) or match.group(2))
        if candidate:
            isbn_value = candidate
            break
    return doi_value, isbn_value, arxiv_value


def remove_artifacts(value: str) -> tuple[str, list[str]]:
    flags: list[str] = []
    cleaned = value
    for name, pattern in ARTIFACT_PATTERNS:
        if pattern.search(cleaned):
            flags.append(name)
            cleaned = pattern.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"(?:,\s*){2,}", ", ", cleaned)
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    cleaned = re.sub(r"([,;])\s*(?=[,.])", r"\1", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    return cleaned.strip(), flags


def strip_leading_reference_number(value: str) -> str:
    value = re.sub(r"^\s*\[\d{1,4}\]\s*", "", value)
    value = re.sub(r"^\s*\d{1,4}[.)]\s*", "", value)
    return value.strip()


def title_candidates(raw: str, cleaned: str) -> list[TitleCandidate]:
    candidates: list[TitleCandidate] = []

    for match in QUOTED_TITLE.finditer(raw):
        title, _ = remove_artifacts(normalize_whitespace(match.group(1)))
        if len(title.split()) >= 2:
            candidates.append(TitleCandidate(title, "quoted", 0.96))

    # In conference citations, the real work title normally appears before
    # `In:`. A year inside the proceedings title can otherwise be mistaken
    # for the author/year boundary and produce the conference name instead.
    in_marker = re.search(r"\bIn:\s+", cleaned, re.I)
    if in_marker:
        before_in = cleaned[: in_marker.start()].strip()
        author_boundary = re.search(r"\.\s+(?=[A-Z])", before_in)
        if author_boundary:
            title = normalize_whitespace(before_in[author_boundary.end() :])
            if len(title.split()) >= 2:
                candidates.append(TitleCandidate(title, "before_in", 0.90))

    # Book, report, and no-year references often place the title after an
    # author list without a publication year before it. Use the first clear
    # author/title boundary instead of stripping text after every occurrence
    # of the word `and` inside the title.
    author_boundary = re.search(r"\.\s+(?=[A-Z])", cleaned)
    if author_boundary:
        after_author = cleaned[author_boundary.end() :]
        sentence = re.split(r"(?<=[.!?])\s+", after_author, maxsplit=1)[0]
        sentence = normalize_whitespace(sentence)
        if len(sentence.split()) >= 2:
            candidates.append(TitleCandidate(sentence, "after_author", 0.74))

    year = YEAR.search(cleaned)
    if year:
        after_year = cleaned[year.end() :]
        # Accompanying publication dates such as `(2025, April).` occur in
        # conference references. Remove the date wrapper before extracting
        # the title; otherwise the parser may fall back to an author fragment.
        after_year = re.sub(
            r"^\s*(?:,\s*[^)]*)?\s*\)\.\s*",
            "",
            after_year,
        )
        after_year = after_year.lstrip(" .,;:()-")
        after_year, _ = remove_artifacts(after_year)
        # Do not treat abbreviations such as `vs.` or `e.g.` inside a title as
        # the end of the title sentence.
        sentence = re.split(
            r"(?<!vs\.)(?<!e\.g\.)(?<!i\.e\.)(?<!etc\.)(?<!U\.S\.)(?<!U\.S\.A\.)(?<=[.!?])\s+",
            after_year,
            maxsplit=1,
            flags=re.I,
        )[0]
        sentence = normalize_whitespace(sentence)
        if len(sentence.split()) >= 2:
            candidates.append(TitleCandidate(sentence, "after_year", 0.80))

    # Book-like references often have the title before publisher/year. Generate
    # a conservative sentence candidate for the lookup layer to evaluate later.
    pieces = [normalize_whitespace(piece) for piece in re.split(r"[.!?]\s+", cleaned)]
    pieces = [piece for piece in pieces if len(piece.split()) >= 3]
    if pieces:
        longest = max(pieces, key=lambda piece: (len(piece.split()), len(piece)))
        longest = re.sub(r"^.*?\b(?:et al\.?|and)\b\s*", "", longest, flags=re.I)
        longest = normalize_whitespace(longest)
        if len(longest.split()) >= 3:
            candidates.append(TitleCandidate(longest, "sentence_heuristic", 0.58))

        # Common book form: `Author, Book Title. Publisher, Year.` Keep the
        # portion after the first author separator as a lower-confidence
        # candidate rather than forcing the author into the title.
        book_piece = pieces[0]
        if "," in book_piece:
            after_author = normalize_whitespace(book_piece.split(",", 1)[1])
            if len(after_author.split()) >= 3:
                candidates.append(TitleCandidate(after_author, "after_author", 0.64))

    unique: list[TitleCandidate] = []
    seen: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: item.confidence, reverse=True):
        key = candidate.title.casefold()
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique[:5]


def classify_citation(raw: str, doi: str | None, isbn: str | None) -> str:
    lower = normalize_whitespace(raw).casefold()
    if isbn or re.search(
        r"\b(?:publisher|press|edition|isbn|monograph|macmillan|guilford publications|"
        r"macarthur foundation|doctoral dissertation|dissertation|springer|reaktion books|"
        r"research center|research centre|report)\b",
        lower,
    ):
        return "book_or_report"
    if re.search(r"\b(?:rfc|iso\s*\d+|nist|ieee\s+std|w3c)\b", lower):
        return "standard_or_web"
    if doi or re.search(r"\b(?:journal|proceedings|conference|vol\.?|doi)\b", lower):
        return "article_or_conference"
    return "unknown"


def diagnose_reference(number: int | None, raw: str) -> ReferenceDiagnostic:
    raw = raw.strip()
    without_number = strip_leading_reference_number(raw)
    cleaned, artifact_flags = remove_artifacts(without_number)
    doi, isbn, arxiv_id = extract_identifiers(raw)
    candidates = title_candidates(raw, cleaned)
    citation_type = classify_citation(raw, doi, isbn)
    notes: list[str] = []

    if not candidates:
        notes.append("No reliable title candidate was extracted.")
    if artifact_flags:
        notes.append("Layout-like page/line markers were removed from the candidate text.")
    if len(candidates) > 1:
        notes.append("Multiple title candidates were retained for later lookup.")
    if citation_type == "book_or_report" and not isbn:
        notes.append("Book/report detected without an ISBN; title and author matching may be weaker.")
    if doi:
        notes.append("A DOI was found; future lookup should treat it as the strongest identifier.")
    if isbn:
        notes.append("An ISBN was found; future lookup should try exact ISBN matching first.")

    layout_artifacts = set(artifact_flags) - {"page_or_line_label"}
    if doi or isbn:
        confidence = "high" if candidates else "medium"
    elif candidates and candidates[0].method == "quoted":
        confidence = "high" if not layout_artifacts else "medium"
    elif candidates and not layout_artifacts:
        confidence = "medium"
    else:
        confidence = "low"

    return ReferenceDiagnostic(
        number=number,
        raw_citation=raw,
        cleaned_citation=cleaned,
        title_candidates=candidates,
        doi=doi,
        isbn=isbn,
        arxiv_id=arxiv_id,
        citation_type=citation_type,
        artifact_flags=artifact_flags,
        parser_confidence=confidence,
        parser_notes=notes,
    )


def diagnose_text(text: str, source: str) -> DocumentDiagnostics:
    header, section, warnings = find_references_section(text)
    segments = segment_references(section)
    diagnostics = [diagnose_reference(number, raw) for number, raw in segments]
    return DocumentDiagnostics(source, header, len(diagnostics), diagnostics, warnings)


def render_text(result: DocumentDiagnostics) -> str:
    lines = [
        f"Source: {result.source}",
        f"References heading: {result.references_header or 'not found'}",
        f"References detected: {result.reference_count}",
    ]
    for warning in result.warnings:
        lines.append(f"Warning: {warning}")

    for index, item in enumerate(result.diagnostics, start=1):
        lines.extend(
            [
                "",
                f"[{item.number or index}] {item.parser_confidence.upper()} confidence | {item.citation_type}",
                f"Raw: {item.raw_citation}",
                f"Cleaned: {item.cleaned_citation}",
                f"DOI: {item.doi or '-'} | ISBN: {item.isbn or '-'} | arXiv: {item.arxiv_id or '-'}",
                f"Artifacts: {', '.join(item.artifact_flags) or '-'}",
                "Title candidates:",
            ]
        )
        if item.title_candidates:
            for candidate in item.title_candidates:
                lines.append(f"  - [{candidate.confidence:.2f}, {candidate.method}] {candidate.title}")
        else:
            lines.append("  - none")
        for note in item.parser_notes:
            lines.append(f"Note: {note}")
    return "\n".join(lines) + "\n"


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show how citations were parsed from a PDF; does not query databases yet."
    )
    parser.add_argument("pdf", type=Path, nargs="?", help="PDF to inspect")
    parser.add_argument("--text-file", type=Path, help="Use a text fixture instead of a PDF")
    parser.add_argument("--json", dest="json_path", type=Path, help="Write diagnostics as JSON")
    args = parser.parse_args(list(argv))
    if bool(args.pdf) == bool(args.text_file):
        parser.error("provide exactly one PDF path or --text-file")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv or sys.argv[1:])
    source_path = args.pdf or args.text_file
    assert source_path is not None
    try:
        text = extract_pdf_text(source_path) if args.pdf else source_path.read_text(encoding="utf-8")
        result = diagnose_text(text, str(source_path))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(render_text(result), end="")
    if args.json_path:
        args.json_path.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")
        print(f"JSON diagnostics written to {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
