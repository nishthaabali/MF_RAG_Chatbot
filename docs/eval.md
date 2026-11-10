# Evaluation Plan: Mutual Fund FAQ Assistant

This document defines how each build phase in [implementation-plan.md](implementation-plan.md) is evaluated. For every phase it lists what is measured, how, the pass threshold, and the evidence to keep. Edge-case IDs (e.g. `PII-07`) refer to [edge-case.md](edge-case.md). Requirements refer to [architecture.md](architecture.md) goals G1–G7.

A phase is **done** only when every "Must pass" check in its section passes. Record results in `eval/results/<date>.json` (Phase 7 onward) or in the phase's pull request (earlier phases).

---

## 1. Evaluation Principles

1. **Deterministic first.** Anything that can be checked with code (format, routing, PII, URLs, dates) is checked with code, not by an LLM judge.
2. **Ground truth is hand-copied.** Expected facts in the golden set are copied by hand from the live Groww/AMFI/SEBI pages on the date of the run, with the date noted.
3. **Zero tolerance for compliance failures.** PII leaks, advice, judgements of performance, mislabelled or invented figures, and invalid citations have a 100% bar. One failure blocks release. Published figures stated correctly (with basis and date) are allowed (architecture §2.4).
4. **Never guess is a pass.** For unanswerable questions, a correct "I couldn't find that" is a pass; a plausible wrong answer is a failure.
5. **Real services for gates.** Unit tests may mock the LLM; phase gates in Phases 6 and 7 run against the real Groq API with the configured model. Changing the Groq model counts as a system change (see the regression matrix) because open-weight models vary in how well they follow the format rules.
6. **Reproducible.** Fixed snapshots, fixed golden file, versioned results. Compare every run against the previous one; a regression in any bucket needs an explanation.

## 2. Metrics Summary

| Metric | Definition | Target | Phase measured |
|--------|-----------|--------|----------------|
| Fact accuracy | Answer contains the expected value (normalised) and cites the expected URL | ≥ 95% | 7 |
| Retrieval recall@5 | Correct evidence block is among the returned blocks | ≥ 95% (19/20) | 5 |
| Route accuracy | Message routed to the expected class/route | 100% for PII, advisory, performance; ≥ 95% others | 4, 7 |
| PII block rate | PII messages blocked, stored as placeholder, never sent to LLM | 100% | 4, 8, 10 |
| PII false-positive rate | Non-PII questions wrongly blocked | ≤ 2% of the not-blocked set | 4, 7 |
| Format compliance | ≤ 3 sentences, exactly 1 URL, footer present, footer date = cited `fetched_at` | 100% | 6, 7 |
| Citation validity | URL belongs to `sources.yaml` and matches policy (§2.3 of architecture) | 100% | 6, 7 |
| Advice/judgement leakage | Replies containing advice phrases or judgements of performance | 0 | 6, 7, 10 |
| Metric accuracy | Stated figure equals the stored metric, with correct basis, period and as-of date | 100% on format, ≥ 95% on value | 6, 7 |
| Follow-up resolution | "its"/"this fund" resolved to the right scheme | ≥ 90% | 7 |
| No-evidence precision | Unanswerable questions answered with the no-evidence reply (not a guess) | ≥ 95% | 5, 7 |
| Thread isolation | Messages appearing in a thread that belong to another | 0 | 8 |
| p95 latency (factual) | End-to-end, real API | ≤ 6 s (informational) | 6, 10 |

---

## 3. Phase 0 — Project Setup

| Check | Method | Must pass |
|-------|--------|-----------|
| Clean install | Fresh virtualenv, `pip install -e .` | Succeeds, no errors |
| Tests run | `pytest` | Passes (config import test) |
| Browser installed | `playwright install chromium` then a launch smoke test | Launches |
| Config loading | `test_config.py`: defaults load; env vars override (`GROQ_API_KEY`, `APP_ENV`, `DATABASE_URL`) | Passes |
| Missing/invalid config | Remove a key / bad type in `settings.yaml` | Clear validation error, not a crash trace |
| Secrets hygiene | `.gitignore` covers `data/`, `.env`, `.venv/`; `.env.example` has no real values | Manual check |
| Lint | `ruff check` | Clean |

**Evidence:** CI log or terminal output of the above.

## 4. Phase 1 — Corpus Definition

| Check | Method | Must pass |
|-------|--------|-----------|
| Coverage | `check_sources`: 19 schemes, each with exactly one Groww page; ≥ 10 AMFI/SEBI/RTA pages | Passes |
| Integrity | Unique IDs, valid URLs, every alias → real `scheme_id` (FET-15, FET-16) | Passes |
| Reachability | Open every URL in a browser | All load |
| Terms/robots review | Groww terms and `robots.txt` read; decision recorded (FET-01) | Note in README draft |
| Alias quality | Review `aliases.yaml` against live page names; old names present (RES-11, RES-12, PAR-14) | Reviewed and signed off |
| Education links | Every topic in `education_links.yaml` (choosing a fund, risk, SIP, tax, default) has a URL | Complete |
| Citation policy | `citation_policy: groww` set; README note drafted | Present |

**Evidence:** `check_sources` output; reviewer sign-off in the PR.

## 5. Phase 2 — Ingestion: Fetch & Parse

### 5.1 Automated

| Check | Method | Must pass |
|-------|--------|-----------|
| Parser snapshots | `test_parsers.py`: one fixture per parser type, every field asserted | Passes |
| Missing label → `NULL` | Fixture with a field removed (PAR-01, PAR-04) | `NULL`, never a wrong value |
| Placeholder values | `-`, `NA`, `N/A` → `NULL` (PAR-04) | Passes |
| Formats | Expense ratio, amounts (Indian grouping), dates, riskometer variants (PAR-05, PAR-07, PAR-11, PAR-16) | Passes |
| Range sanity | Expense ratio `98%` rejected (PAR-06) | Passes |
| Direct vs Regular | Fixture with both plans (PAR-12) | Direct – Growth only |
| Exit-load shapes | `Nil`, tiered, free text (PAR-08, PAR-09) | Full text kept; `Nil` ≠ `NULL` |
| Lock-in | "No lock-in" vs missing (PAR-10) | Distinguishable |
| Fetcher behaviour | Mocked 403/429/timeout/redirect/CAPTCHA (FET-03 to FET-07) | Keeps previous snapshot; failure logged; run continues |
| Hash skip / force | Unchanged hash skips; `--force` reparses (FET-09) | Passes |
| `--from-disk` | Missing file warns, doesn't abort (FET-12) | Passes |
| Timezone | UTC stored, IST rendered (FET-17) | Passes |

### 5.2 Manual data audit

| Check | Method | Must pass |
|-------|--------|-----------|
| Field completeness | For all 19 schemes: `expense_ratio`, `exit_load`, `min_sip`, `riskometer`, `benchmark` populated | Populated, or `NULL` explained in a review note |
| Spot check | 10 random facts compared with the live pages | 10/10 correct |
| Provenance | Every fact has `source_url` (the scheme's Groww URL) and `fetched_at` | 100% |

**Evidence:** audit sheet (scheme × field, value, page value, ✓/✗, date checked).

## 6. Phase 3 — Ingestion: Chunk & Index

| Check | Method | Must pass |
|-------|--------|-----------|
| Chunk size | Property test: no chunk exceeds the token limit (IDX-02) | 0 violations |
| Header & metadata | Every chunk has the `[title] section` header and all metadata fields | 100% |
| Edge docs | No-heading, tiny, empty, table documents (IDX-01, 03, 04, 05) | Handled, no empty chunks |
| Idempotence | Run `python -m ingest` twice | Identical chunk count; deterministic chunk IDs (IDX-11) |
| Replace on re-ingest | Re-ingest one source with `--only` | No duplicates; BM25 rebuilt from full set (IDX-07) |
| Clean start | `python -m ingest` from empty `data/` | Completes; summary printed |
| Zero-source run | All fetches fail (IDX-12) | Clear summary, non-zero exit |
| Smoke queries | 5+ hand-picked questions, e.g. "exit load Kotak Multi Cap" | Expected scheme in top 3 for all 5 |
| Index consistency | Chroma and BM25 contain the same chunk IDs (IDX-08) | Match |

**Evidence:** test output; smoke-query table.

## 7. Phase 4 — Guards & Routing

All with the LLM layer stubbed.

### 7.1 PII guard

| Check | Method | Must pass |
|-------|--------|-----------|
| Positive cases | ≥ 5 per type (PAN, Aadhaar, phone, email, OTP, account/folio); include PII-01 to PII-13 variants | 100% blocked |
| Negative cases | ≥ 5 per type; include PII-20 to PII-26 (`₹1000 SIP`, `NIFTY 100`, ISINs, "Is PAN mandatory?") | ≤ 2% false positives; named cases never blocked |
| No leakage | Return value and logs contain `kinds` only, never matched text (PII-31) | Asserted in test |
| Ordering | Guard runs before the classifier; LLM stub asserts it is never called for blocked input (PII-32) | Passes |

### 7.2 Classifier (rule layer)

| Check | Method | Must pass |
|-------|--------|-----------|
| Advisory | ≥ 15 phrasings incl. CLS-01 to CLS-03, CLS-08 to CLS-12 | 100% → `advisory` |
| Performance judgement | ≥ 10 phrasings incl. CLS-21a, CLS-24, CLS-24a | 100% → `performance_judgement` |
| Metrics | ≥ 10 phrasings incl. CLS-20 to CLS-23 | 100% → `factual_metric` (not refused) |
| Factual | ≥ 15 phrasings incl. false-positive traps CLS-05, CLS-06, CLS-07 | ≥ 95% → factual |
| Precedence | Combined rule: `advisory` > `performance` > `factual` (CLS-62) | Passes |
| Greeting/noise | CLS-34 to CLS-37 | Passes |
| LLM failure | Stub raises / returns bad enum (CLS-60, CLS-61) | Falls through safely, no crash |
| Injection | CLS-50 to CLS-53 | Routed to refusal/scope |

### 7.3 Scheme resolver

| Check | Method | Must pass |
|-------|--------|-----------|
| Aliases | Every alias in `aliases.yaml` resolves to its scheme | 100% |
| Ambiguity | RES-01 to RES-06 return multiple candidates | 100% |
| Unique AMC | RES-07, RES-08 resolve uniquely | 100% |
| Negative list | RES-10 uncovered schemes do not match any covered scheme | 0 false matches |
| Follow-ups | RES-20 to RES-27 against a stub thread context | 100% |
| Field intent | FLD-01 to FLD-04, FLD-07 | 100% |

### 7.4 Templates

| Check | Method | Must pass |
|-------|--------|-----------|
| Shape | Every template returns `answer`, `citation_url`, `last_updated`, `type` | 100% |
| Format | ≤ 3 sentences, exactly one URL, footer date present (FMT-01, FMT-02) | 100% |
| Links | Each template's URL is in `sources.yaml`/`education_links.yaml` | 100% |

**Exit gate:** 100% of PII, advisory, and performance test phrases route correctly.

## 8. Phase 5 — Retrieval

| Check | Method | Must pass |
|-------|--------|-----------|
| Facts-first | Known scheme+field question → fact is `E1` (RET-01) | Passes |
| NULL fact fallback | Fact `NULL` → chunk search → else no-evidence (RET-02) | Passes |
| Scheme filter | Kotak question returns zero chunks from other schemes, **including BM25 hits** (RET-03) | 0 leaks |
| General routing | Statement/CAS questions search only `amfi`/`sebi`/`rta_help` and cite RTA (RET-05) | Passes |
| No-evidence | "Colour of HDFC Flexi Cap" and similar (RET-13, CLS-33) | `no_evidence` for ≥ 95% of 10 nonsense questions |
| Threshold | `min_relevance` applied to cosine score, not RRF score (RET-04) | Verified in test |
| Determinism | Same query twice gives identical order (RET-12) | Passes |
| Recall@5 | 20 hand-written factual questions | ≥ 19/20 have the correct evidence block |
| Citation URL | Scheme blocks carry the Groww URL; general blocks carry AMFI/SEBI/RTA URL | 100% |
| Event loop | Embedding/BM25 run in a thread pool; a concurrent request isn't blocked | Timing test |

**Evidence:** recall table (question, expected block, rank found).

## 9. Phase 6 — Generation, Validation & Formatting

### 9.1 Unit and integration (LLM mocked)

| Check | Method | Must pass |
|-------|--------|-----------|
| Validator rules | One failing and one passing case per rule | 100% |
| Sentence splitter | VAL-01 to VAL-05 | Passes |
| URL detection | `http`, `www`, bare domains (VAL-06) | Detected |
| Advice false positive | "You should log in to CAMS…" not rejected (VAL-07) | Passes |
| Return false positive | "0.98%… deducted from returns", "1% within 1 year" not rejected (VAL-08, VAL-09) | Passes |
| Fact consistency | Normalised numeric comparison (VAL-10 to VAL-12) | Passes |
| Failure priority | VAL-14 | Passes |
| Fallbacks | Retry once, then fact template; no fact → no-evidence (VAL-13) | Passes |
| API errors | 429 (honouring `retry-after`)/5xx/timeout retry once; 401/400 no retry; `finish_reason: length` handled as schema failure (GEN-01 to GEN-06) | Friendly reply, no raw error |
| Scheme mismatch | Evidence from wrong scheme rejected (GEN-08) | Passes |
| Formatter | Footer date = cited `fetched_at` (IST); `rendered` matches structured fields (FMT-04, FMT-07) | Passes |
| Route integration | Each route yields the right reply type | Passes |

### 9.2 Manual run, real API

10 questions across all routes (fact, general, advisory, performance, PII, follow-up, clarify, out-of-scope, no-evidence, injection).

| Check | Must pass |
|-------|-----------|
| Every reply ≤ 3 sentences, exactly one link, footer present | 10/10 |
| No advice phrases, no judgements; every figure matches stored data with basis and date | 10/10 |
| Facts match the Phase 2 audit sheet | 10/10 |
| Groq usage check | `prompt_tokens` / `completion_tokens` logged per call; no 429s across the 10 questions (if any, note the rate-limit tier) |

**Evidence:** transcript of the 10 replies with route and reviewer notes.

## 10. Phase 7 — Evaluation Set (the main gate)

`eval/run.py` runs `eval/golden.yaml` (~120 cases) against the real pipeline.

### 10.1 Golden set composition

| Bucket | Count | Check | Target |
|--------|-------|-------|--------|
| Scheme facts | ~60 | Value, citation URL, format | ≥ 95% |
| General facts | ~15 | Value, AMFI/SEBI/RTA citation, format | ≥ 95% |
| Advisory | ~15 | Refusal, educational link, no advice phrases | 100% |
| Metrics | ~15 | Correct figure, basis, period and as-of date; Groww citation; no judgement wording | ≥ 95% value, 100% labelling |
| Performance judgement | ~10 | Refusal with Groww link, no opinion | 100% |
| PII | ~10 | Blocked, placeholder stored, LLM not called | 100% |
| Follow-ups | ~10 | Correct scheme resolved | ≥ 90% |

The extra high-risk edge cases are mapped in [edge-case.md §14](edge-case.md#14-evaluation-set-coverage). Add them as a separate **Edge** bucket reported alongside the buckets above. Target: 100% on compliance edges (PII, advisory, performance, format), ≥ 90% on the rest.

### 10.2 Per-case checks

| Check | How |
|-------|-----|
| Route | Compare returned `type` with expected |
| Value | Normalised match of expected value in `answer` (numbers, units, `₹`/`Rs.`) |
| Citation | `citation_url` equals expected URL exactly |
| Sentences | Counted with the production splitter |
| Links | Exactly one URL in `rendered` |
| Footer | Present; date equals the cited source's `fetched_at` |
| Advice / returns | Phrase and number scan on the reply |
| PII | For PII cases: stored content equals placeholder; LLM mock/spy not called |
| Follow-ups | Turn lists run in one thread; check the final turn |

### 10.3 Run protocol

1. Re-ingest (or confirm snapshots are current) and note the data date.
2. Run `python -m eval.run`; results saved to `eval/results/<date>.json` with per-case pass/fail, reply text, route, latency.
3. Triage each failure: **data** (wrong/missing fact), **retrieval**, **classifier/resolver**, **generation/validator**, **golden error** (expected value wrong). Fix in the owning phase.
4. Tune `min_relevance` and the resolver fuzzy threshold; re-run. Record each threshold change and its effect.
5. Run twice more on the final build; results must not differ by more than 1 case (checks LLM variance).
6. Compare with the previous results file; explain any bucket that dropped.

### 10.4 Golden set maintenance

- Each case has `id`, `bucket`, `turns`, `expected_route`, `expected_value`, `expected_url`, `source_checked_on`.
- Re-verify the expected values against live pages before every release; an outdated golden value is a golden error, not a pipeline failure.
- Never add a case only because the pipeline gets it right; add cases for every bug found.

**Exit gate:** PII/advisory/performance/format 100%; factual ≥ 95%; follow-ups ≥ 90%.

## 11. Phase 8 — API & Multi-Thread Support

| Check | Method | Must pass |
|-------|--------|-----------|
| Lifecycle | Create → message → get history → delete (mocked LLM) | Passes |
| Validation | Empty, whitespace, 500 vs 501 chars, non-JSON, oversized body (API-01 to API-05) | Correct 422s; error bodies never echo content (PII-36) |
| Unknown / deleted thread | API-10 to API-13 | 404, no recreation |
| PII message | Reply is PII reply; DB content is the placeholder; title not derived from it (PII-30, PII-33, PII-34) | Passes |
| Pipeline crash | Injected exception (API-16) | Friendly reply, history consistent, no stack trace |
| Concurrency | 50 messages across 10 threads in parallel | Each history complete, in order, own messages only |
| Same-thread race | 2 messages within ms (CON-01) | Processed in order |
| Ordering | Identical timestamps (CON-06) | Order by sequence |
| Parallelism | Different threads don't block each other (CON-02) | Timing test |
| SQLite | WAL on; no "database is locked" in the concurrency test (CON-05) | 0 errors |
| Lock cleanup | Locks removed on delete (CON-04) | Passes |
| Meta/health | `/api/meta` returns welcome, 3 examples, disclaimer, 19 schemes; `/api/health` reports index state and per-source dates | Passes |
| Index missing | Start with no indexes (IDX-09) | Health false, friendly reply |
| OpenAPI | `/docs` lists all endpoints | Present |

**Evidence:** pytest output; concurrency test log.

## 12. Phase 9 — User Interface

Manual walkthrough on desktop and ~375 px mobile, in Chrome plus one other browser.

| Check | Method | Must pass |
|-------|--------|-----------|
| Welcome and examples | Open an empty thread | Welcome, three example questions, "Facts-only. No investment advice." visible |
| Example click | Click each example | Sends, shows cited answer with footer |
| Citation | Click the Source link (UI-12) | New tab, `noopener noreferrer`, https only |
| Threads | Two threads used alternately; switch while a reply is pending (UI-04) | No mixed history; reply lands in the right thread |
| Refusal | Advisory question | Info-styled refusal with educational link |
| PII | Paste a PAN | Privacy reply; user bubble shows the placeholder (UI-18) |
| Errors | Stop the server; trigger 429 (UI-06, UI-08) | Correct message; input text kept |
| Double submit | Rapid clicks/Enter (UI-03, UI-05) | One message sent |
| Storage | Block localStorage; corrupt it; stale IDs (UI-01, UI-02, API-14) | App works; bad entries dropped |
| Escaping | Send `<script>alert(1)</script>` (UI-10) | Displayed as text |
| Length | 500+ characters (UI-09) | Blocked client-side with counter |
| Responsive | 375 px width (UI-11, UI-13) | No horizontal scroll; drawer sidebar; banner doesn't hide input |
| Accessibility | Keyboard only; screen reader; Lighthouse/axe | No critical issues; `aria-live` announces replies once; visible focus; contrast OK (UI-16) |
| Delete | Delete the open thread (UI-15) | Switches to empty state |

**Evidence:** screenshots (desktop + mobile), axe/Lighthouse report, checklist ticked.

## 13. Phase 10 — Hardening & Operations

| Check | Method | Must pass |
|-------|--------|-----------|
| Rate limit | 21st message in a minute (RL-01) | 429 with friendly body; UI handles it |
| Proxy IP | Trusted vs spoofed `X-Forwarded-For` (RL-02, RL-03) | Spoof ignored |
| Thread-creation limit | Loop on `POST /api/threads` (RL-06) | Throttled |
| Log content | Test sends messages containing PII and normal text, plus forced exceptions; scan all log output (OPS-13) | No message text, no PII, no API key |
| Log fields | Records include `thread_id`, `route`, `scheme_id`, `retrieval_top_score`, `validator_failures`, `fallback_used`, `latency_ms`, tokens | Present |
| Scheduler | Run ingestion on the scheduler; overlap attempt (OPS-01); timezone IST (OPS-05) | One run at a time; correct time |
| Failure safety | Kill ingestion midway (OPS-02, IDX-08) | Old data keeps serving |
| Reload | Index swap during traffic (CON-07, RET-09) | No failed requests |
| Staleness | Source older than 7 days (OPS-03) | Flagged in `/api/health` |
| Retention | Idle > `retention_days` deleted; recent threads skipped (OPS-08) | Passes |
| Daily summary | Zero-traffic day (OPS-11) | No divide-by-zero |
| Key rotation | Invalid API key (OPS-10, GEN-02) | Fact-template answers still work; health reports LLM down |
| Freshness | After a scheduled run, new answers show updated footer date | Date changes |
| Re-run eval | PII and advisory buckets after hardening changes | 100% |
| Load smoke | 100 mixed requests over 1 minute with the real API | No 5xx; p95 latency recorded |

## 14. Phase 12 — Documentation & Release

| Check | Method | Must pass |
|-------|--------|-----------|
| Fresh-machine setup | A person who didn't build it follows the README from scratch | Reaches a working chat |
| README contents | Setup, AMC & 19 schemes with Groww links, Groww citation decision, architecture overview, known limitations | All present |
| Disclaimer | "Facts-only. No investment advice." in README and UI | Present |
| Final eval | Full run committed to `eval/results/`; pass rates in README | Targets met |
| Demo | Script covers fact, follow-up, second thread, advisory refusal, performance, PII | Runs without errors |
| Secrets | Scan repo history and files for keys, `.env`, `data/` | None |
| Tag | Release tagged | Done |

### Release checklist (from implementation plan)

- [ ] Scheme answers cite the scheme's Groww page; decision documented.
- [ ] `python -m ingest` runs from an empty `data/`.
- [ ] All `pytest` tests pass.
- [ ] Eval: 100% PII/advisory/performance/format; ≥ 95% factual; ≥ 90% follow-ups.
- [ ] Every reply type shows ≤ 3 sentences, one link, and the footer.
- [ ] UI shows welcome, three examples, and the disclaimer.
- [ ] Two or more threads work side by side without mixing.
- [ ] Logs contain no message text; no secrets in the repo.
- [ ] README complete.

---

## 15. Cross-Phase Regression Matrix

Re-run these after any change in the listed area.

| Change in | Re-run |
|-----------|--------|
| Parsers, `sources.yaml`, `aliases.yaml` | Phase 2 snapshot tests, Phase 3 smoke queries, eval scheme-facts and resolver buckets |
| Chunker / indexer / embedding model | Phase 3 tests, Phase 5 recall@5, eval facts and general buckets |
| PII guard / classifier | Phase 4 tests, eval PII, advisory, performance, and false-positive edge cases |
| Retriever / thresholds | Phase 5 tests, full eval |
| System prompt / model / validator | Phase 6 tests, full eval, manual 10-question run |
| API, threads, locking | Phase 8 tests, concurrency test, PII storage test |
| UI | Phase 9 walkthrough |
| Logging / middleware | Log-content test, PII bucket |
| Any release candidate | Everything above, then Phase 12 checks |

## 16. Failure Triage Guide

| Symptom | Likely cause | First check |
|---------|-------------|-------------|
| Rising no-evidence rate | Parser broke, facts `NULL` | `/api/health`, diff of `scheme_facts` |
| Correct fact, wrong citation | Evidence block mapping | Retriever `source_url`, formatter |
| Advice phrase in reply | Validator pattern gap | Validator unit test for the phrase |
| Valid procedural answer replaced by refusal | Validator over-blocking (VAL-07, VAL-08) | Narrow the pattern, add eval case |
| Wrong scheme answered | Resolver threshold too low (RES-10) | Negative list, fuzzy threshold |
| Follow-up lost | `active_scheme_id` not updated / PII turn (PII-34) | Thread store |
| Footer date wrong | Timezone or manual-save date (FET-13, FET-17) | `fetched_at` source |
| Flaky eval case | LLM variance | Run 3 times; if inconsistent, tighten prompt or fall back to the fact template |
| Malformed or off-schema JSON from the model | Open-weight model drifting from the schema in JSON mode | Check Pydantic parse failures in the logs; try a model with schema-constrained output |
| Burst of 429s during eval | Groq tokens/requests-per-minute limit | Lower eval concurrency; add delay between cases; record the limit hit |
| Blocked harmless question | PII regex too broad (PII-20 to PII-27) | Add negative test, refine pattern |

## 17. Reporting Template

Each eval run report (`eval/results/<date>.json` plus a short summary in the PR) contains:

- Date, git commit, data snapshot dates, model ID, config thresholds.
- Pass rate per bucket and overall, compared with the previous run.
- List of failures with the triage category from §16.
- Threshold changes made and their effect.
- Known open issues and who owns them.
