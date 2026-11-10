# Scheme Facts Review (Phase 2)

Ingestion run on 09 Nov 2026 (19:09 IST): 38 of 38 sources fetched and parsed, 0 failures, 19 `scheme_facts` rows. Regenerate the full table with `python -m ingest.facts_report`.

## Key-field completeness

`expense_ratio`, `exit_load`, `min_sip`, `riskometer` and `benchmark` are populated for all 19 schemes, with one explained exception:

| Scheme | Field | Stored | Why |
|--------|-------|--------|-----|
| `absl-bse-total-market` | `expense_ratio` | NULL | The Groww page displays **6.17%**. That is above SEBI's total expense ratio limits for any open-ended scheme, and far above what an index fund can charge. The same page's `base_expense_ratio` is 0.22%, and the scheme launched on 22 Oct 2026, so the figure is very likely a data error on Groww's side. The parser rejects any expense ratio outside 0–3.5% (edge case PAR-06) rather than publish it. Expense-ratio questions for this scheme get the no-evidence reply with the Groww link. Re-check after the next ingestion; the value will be stored automatically once it is plausible. |

## Spot check against the live pages

10 facts were picked at random (seed 20261109) and each stored value was searched for in the visible text of the live Groww page, fetched fresh. **10 of 10 appeared verbatim.**

| Scheme | Field | Value |
|--------|-------|-------|
| hdfc-corporate-bond | riskometer | Moderate |
| hdfc-silver-etf-fof | exit_load | Exit load of 1%, if redeemed within 15 days. |
| nippon-large-cap | riskometer | Very High |
| absl-multi-cap | min_sip | ₹100 |
| bandhan-small-cap | expense_ratio | 0.65% |
| hdfc-nifty-next-50 | min_sip | ₹100 |
| absl-corporate-bond | expense_ratio | 0.34% |
| absl-bse-total-market | riskometer | Very High |
| hdfc-flexi-cap | exit_load | Exit load of 1% if redeemed within 1 year |
| nippon-large-cap | expense_ratio | 0.85% |

This was a scripted comparison with the visible page text, not a reading by a person. A human glance at a few pages is still worthwhile.

## Findings that change how facts are read

- **Riskometer:** Groww's embedded `nfo_risk` field is the launch-time rating and was wrong on 10 of 19 pages (e.g. "Moderately High" where the page shows "Very High"). The parser reads the riskometer from the page's meta description ("… Risk is Very High"), which matched the visible riskometer on all 19 pages, and cross-checks it against the visible label.
- **Expense ratio:** the JSON holds 4 decimals (0.6958); the page shows 2 (0.70%). The stored value is the 2-decimal figure, matching what users see on the cited page.
- **Launch date:** 12 schemes show 01 Jan 2013. That is the start date of Direct plans in India, not the scheme's original launch. Answers should say "Direct plan launch date" if this field is used.
- **Category:** Groww's labels are stored as published, e.g. the HDFC Nifty Next 50 Index Fund is "Equity - Large Cap" and the ABSL BSE Total Market Index Fund is "Equity - Flexi Cap".
- **Everything is extracted (policy change, 09 Nov 2026).** All page data is retained: the full embedded object, 1,001 metrics (NAV, AUM, annualised/absolute/SIP returns, category averages, ranks, risk ratios), 1,910 holdings, 85 similar-fund rows, 48 manager profiles, 24,036 history points, 2,504 other fields, and 89 Groww PROS/CONS statements flagged as judgements. Only interpretation is restricted (architecture §2.4).
- **Return series:** `return_stats` = the page's "Annualised returns" table, `simple_return` = the "Absolute returns" tab, `sip_return` = annualised SIP returns. The `stats` block disagrees with the page (e.g. Kotak 1Y category average 0.93% vs 5.1% shown) and is not used.
- **Verified against the pages:** all 253 values marked `displayed` (NAV, AUM, rating, returns, category averages and ranks in each page's returns table) were found verbatim in the page's visible text. The other 748 metrics exist only in Groww's page data (SIP returns, risk ratios, absolute returns over 1 year which load behind a tab, periods outside the table).
- **ABSL BSE Total Market (2 weeks old):** for the 6M and since-launch category averages, Groww's data (3.94%, 14.26%) disagrees with the page (+4.7%, +181.8%). Both are kept as data, marked not displayed, and noted for review.
- **`category_info` is unreliable:** it labels every equity fund "Contra" and every debt fund "Gilt". Stored with `reliable = False` and kept out of answer text.
