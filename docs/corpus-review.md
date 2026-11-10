# Corpus Review (Phase 1)

Checked on 2026-11-09 with plain HTTP requests (`curl`, descriptive User-Agent). Nothing was fetched into `data/`; snapshots start in Phase 2.

## 1. Groww access

| Check | Result |
|-------|--------|
| `https://groww.in/robots.txt` | `User-agent: *` disallows `/dashboard/`, `/onboarding/`, `/pages/*`, `/v1/api/*`, `/mutual-funds/filter?*`, `/mutual-funds/compare/*`, `/mutual-funds/user/` and others. **`/mutual-funds/<scheme>` pages are not disallowed.** |
| All 19 scheme URLs | HTTP 200 |
| Terms of use | **Not reviewed.** `robots.txt` permitting a path is not the same as the terms permitting automated collection. A person should read Groww's terms before the first scheduled fetch. If they forbid it, use `python -m ingest --from-disk` with manually saved pages (Phase 2). |

The fetcher must keep the 2-second rate limit and skip `/mutual-funds/compare/*` and `/mutual-funds/filter?*` (it only needs scheme pages).

## 2. Scheme identities differ from the URL slugs

The scheme names below come from each live page's `<title>`. Five Groww slugs are legacy names and open a different scheme than the slug suggests, and the sample table in the architecture doc was inferred from slugs.

| Groww URL slug | Scheme the page actually shows | `scheme_id` |
|----------------|--------------------------------|-------------|
| `hdfc-equity-fund` | HDFC Flexi Cap Fund | `hdfc-flexi-cap` |
| `birla-sun-life-equity-fund` | Aditya Birla Sun Life Flexi Cap Fund | `absl-flexi-cap` |
| `birla-sun-life-small-midcap-fund` | Aditya Birla Sun Life **Small Cap** Fund | `absl-small-cap` |
| `birla-sun-life-corporate-bond-fund` | Aditya Birla Sun Life **Credit Risk** Fund | `absl-credit-risk` |
| `birla-sun-life-short-term-fund` | Aditya Birla Sun Life **Corporate Bond** Fund | `absl-corporate-bond` |
| `hdfc-medium-term-opportunities-fund` | HDFC **Corporate Bond** Fund | `hdfc-corporate-bond` |
| `edelweiss-mid-and-small-cap-fund` | Edelweiss **Mid Cap** Fund | `edelweiss-mid-cap` |

The other 12 pages match their slugs. Consequences:

- **There is no "ABSL Short Term Fund" or "HDFC Medium Term" scheme in the corpus.** Users asking for those get "not in my sources" (edge case RES-10). Only the former names the slugs themselves suggest are aliases (for example "Birla Sun Life Short Term Fund" → ABSL Corporate Bond Fund).
- **Open question for a reviewer:** the former-name aliases are inferred from slugs and well-known renames, not from AMC documents. Confirm each against the AMC's records before relying on them (edge cases RES-11, RES-12, PAR-14).
- The category list changes: the corpus now has a Credit Risk fund and no Short Duration fund.

## 3. Official pages

URLs were checked for HTTP 200 and a sensible page title. For the AMFI Mutual Funds Sahi Hai pages the text was also searched for key terms.

| Topic | Page | Covered? |
|-------|------|----------|
| Exit load | `amfi-exit-load` | Yes (mentions "exit load" 16 times, plus capital gains) |
| ELSS lock-in | `amfi-elss` | Yes (lock-in mentioned 9 times) |
| Riskometer / risk | `amfi-risk` | Yes (riskometer mentioned 11 times) |
| SIP | `amfi-sip` | Title confirms; text not searched for specifics |
| CAS | `sebi-cas`, `rta-kfintech-cas`, `rta-cams-cas`, `rta-mfcentral` | Pages exist; see gaps |
| **Expense ratio** | `amfi-investor` | **Weak.** The page mentions "expense ratio" twice. No dedicated official page was found. |
| **Capital gains statement download** | none | **Gap.** None of the four statement pages mentioned "capital gain" in the static HTML. CAMS, KFintech and MFCentral are JavaScript-driven, so their rendered pages may differ. |

**Action for Phase 2** (after rendering with Playwright): check whether the CAMS, KFintech and MFCentral pages explain how to get a capital gains statement. If not, add a page that does, or accept that "How do I download my capital gains statement?" (one of the three UI example questions) returns the no-evidence reply with the CAS page link. Resolve this before Phase 7, because the golden set includes statement questions.

The `amfiindia.com` site is a single-page app, and the guessed `/investor-corner/knowledge-center/...` paths returned 404, so only `/investor` and `/investor/awareness-program` are used.

## 3a. Changes made in Phase 2

Found while fetching and parsing the real pages:

| Change | Reason |
|--------|--------|
| Groww pages switched to `render: static` | All facts are in the server-rendered `__NEXT_DATA__` JSON; no browser needed |
| Added `amfi-riskometer` (`/en/how-riskometer-scheme-derived`) | Dedicated AMFI article on how the riskometer is derived; now the `risk` education link |
| `amfi-glossary` switched to `render: browser` | Glossary definitions load client-side; static HTML has none |
| Added `rta-cams-capital-gains` (CAMS Capital Gain / Loss Statement page) | **Closes the capital-gains gap**: explains what the statement contains and how to request it by email |
| Removed `rta-kfintech-cas` | Rendered page holds only a chat widget and date picker, no instructions |
| Removed `rta-mfcentral` | `robots.txt` returns 403, which means "disallow all" |

~~The expense-ratio gap remains~~ **Resolved 2026-11-09.** The glossary is paginated: 10 terms per page across 3 pages at `?page=N`, 27 terms in total. Only page 1 (A–D) had been fetched. `amfi-glossary` now has a `paginate` block in `sources.yaml`, and the fetcher merges every page into one snapshot. The glossary's "Expense Ratio" and "Exit Load" entries answer general questions; scheme expense ratios still come from the Groww pages. The site has no separate NAV or SIP glossary entries.

## 4. Validation

`python -m ingest.check_sources` passes: 19 scheme pages (8 AMCs), 19 general sources (13 AMFI, 4 SEBI, 2 RTA), unique IDs and URLs, every alias maps to a real scheme, every education topic maps to an allowlisted non-scheme source.

Education links reference source IDs, not raw URLs, so every refusal and PII link is on the allowlist and gets a `fetched_at` date for the footer (resolves the footer-date gap FMT-02 in edge-case.md).

## 5. Still open (manual)

- [ ] Read Groww's terms of use.
- [ ] Open each URL in a browser (the scripted check confirms status and title only).
- [ ] Confirm former-name aliases (section 2).
- [x] Capital-gains page gap resolved (section 3a).
- [ ] Expense-ratio concept page: still no dedicated official source.
