# Changelog

## 0.1.2 - Two-column reference parsing fixes

- Joined separately positioned reference numbers and citation text.
- Preserved the correct left-column-then-right-column reading order.
- Removed publisher headers and download footers from reference lists.
- Rejoined soft-hyphenated words split across PDF lines.
- Recognized author-year references whose year is not followed by a period.
- Preserved DOI and web URLs split across PDF line breaks.
- Improved title candidates for books, reports, and conference papers.

## 0.1.1 - PDF extraction fixes

- Preserved reference lines at the top of PDF pages so leading authors are not lost.
- Improved reading order for two-column reference lists.
- Recognized continued-reference headings and avoided stopping at repeated running headers.
- Displayed matched author information when a lookup service provides it.

## 0.1.0 - Initial public release

- Added the CiteVerify HTML interface.
- Added support for uploading multiple PDF and modern Word (`.docx`) files.
- Added HTML reports with DOI links and explanations for unverified results.
- Added document navigation for multi-file reports.
- Added cautious verification using Crossref, DOI resolution, OpenAlex,
  Semantic Scholar, Open Library, and Google Books.
- Added local API-key files so each user can supply their own credentials.
- Added a Windows one-click launcher.
