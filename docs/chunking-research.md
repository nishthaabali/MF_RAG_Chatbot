# Chunking, Embedding and Vector Store Research (input to Phase 3)

Run on 2026-11-09 against `data/parsed/` (38 documents produced by Phase 2) and `data/app.db`. The scripts and raw results are in [eval/research/](../eval/research/):

- `profile_parsed.py` measures section counts and token sizes, using the bge tokenizer.
- `chunking_bench.py` compares chunking strategies, embedding models and retrieval modes.
- `chunking_bench_2026-11-09.json` holds the raw numbers.

Re-run both scripts whenever the parsers change.

**Outcome:** Phase 3 in [implementation-plan.md](implementation-plan.md) and [architecture.md §5.3–5.4](architecture.md#53-chunker) were updated to match the recommendations in §6.

---

## 1. What the parsed corpus looks like

| | Docs | Sections | Tokens (bge tokenizer) | Median tokens / doc |
|---|---|---|---|---|
| Groww scheme pages | 19 | 414 | 71,948 (89%) | 3,103 |
| AMFI | 13 | 45 | 6,978 | 273 |
| CAMS (`rta_help`) | 2 | 6 | 1,008 | 504 |
| SEBI | 4 | 15 | 569 | 135 |
| **Total** | **38** | **480** | **80,503** | |

Section sizes: median 84 tokens, p90 426, p95 757, max 1,340. **29 sections exceed 512 tokens, which is bge-small's input limit**: 25 holdings blocks plus 4 AMFI sections ("What is a Mutual Fund?" at 1,340 tokens, Riskometer, glossary "Credit risk", and the awareness-programme schedule). The core fact sections (objective, details, costs, limits, NAV, fund house, identifiers) are only 12% of Groww tokens, at 30–143 tokens each.

### 1.1 Groww sections are already "chunks"

The Groww parser writes one section per topic, and each section is a self-contained fact paragraph that starts with the scheme name. For example: *"Kotak Multi Cap Fund Direct Growth. Expense ratio: 0.70%. Exit load: … Stamp duty: 0.005%"*. Every page has the same 13 core headings:

- Investment objective
- Scheme details
- Costs and exit load
- Investment limits and lock-in
- NAV and fund size
- Annualised returns
- Absolute returns
- SIP returns
- Category averages (×2)
- Rank within category
- Risk measures
- Similar funds
- Fund house
- Identifiers

These are followed by one section per fund manager and holdings in blocks of 25. Groww's PROS/CONS judgements are correctly absent from the sections.

### 1.2 Where the tokens are

| Content | Share of Groww tokens | Notes |
|---|---|---|
| `Portfolio holdings N–M of T` (86 sections) | **60%** (43.5k tokens); 25 of the 29 sections over 512 tokens | Up to 888 tokens per 25-row block (debt funds have long ISIN-style names). The same data is in `scheme_holdings` (1,910 rows). |
| Returns, ranks and risk sections (130) | 16%, 40–155 tokens each | The same data is in `scheme_metrics` (1,001 rows) with basis, period and as-of date. |
| Fund-manager profiles (48) | 6%, 53–140 tokens each | Also in `fund_managers` (48 rows). |
| Similar funds (17) | 7%, 262–295 tokens | Contains **other schemes'** returns and AUM, which a scheme could be misattributed from. |

### 1.3 Noise in the AMFI, SEBI and CAMS pages

The generic parser keeps page chrome. These are the sections that a simple filter removes (rules in `is_noise()` in the benchmark):

| Pattern | Examples |
|---|---|
| Navigation headings | `Dark`, `View More`, `Videos`, `Quick Links`, `Media`, `Other Links` (AMFI pages, both CAMS pages) |
| Menu-like blocks (≥ 6 lines, > 60% of them ≤ 4 words) | CAMS top menu, SEBI "Topics of Interest" |
| Browser artefact | `"Navigated to Mutual Fund Glossary …"` |
| Banner stubs (< 12 tokens) | "Watch Now", "Know More", "Mutual Funds KYC Aasaan Hai" |
| **Personal contact data** | `amfi-awareness-program` schedule: names and **9 mobile numbers** of AMC contact persons. This must not be indexed: the chatbot must never emit phone numbers. |

The same nav blocks recur across documents (CAMS ×2, AMFI ×2), so without cleaning they would pollute BM25 term statistics.

### 1.4 Corpus gaps found (not chunking problems)

- **The AMFI glossary was truncated at A–D.** The last term was "Dividend Reinvest Option". The page shows 10 terms per page with numbered pagination (`/en/glossary?page=2`, `?page=3`; 27 terms in total), and the fetch captured only page 1. **Fixed in Phase 2 (2026-11-09):** sources can declare a `paginate` block, and the fetcher fetches every page and merges the items into one snapshot (see [corpus-review.md](corpus-review.md)). The re-fetched glossary has all 27 terms, including Exit Load and Expense Ratio. The site has no separate NAV or SIP glossary entries; those are covered by `amfi-mf-basics` and `amfi-sip`. *(An earlier version of this note blamed a "Load More" button. That string was only in the page's translation data; the page uses numbered page links.)*
- The ELSS article is only 181 tokens. It covers 80C and lock-in, but thinly.
- "What does expense ratio mean?" was the most frequent miss in the benchmark, because no indexed official text defined it. The full glossary now has a 3,000-character "Expense Ratio" entry. Re-run the benchmark after Phase 3 to confirm the miss is gone.

## 2. Benchmark setup

- **Queries:** 44 hand-labelled queries, each with a gold `source_id` and section:
  - 22 scheme queries;
  - 18 general AMFI/SEBI/RTA queries;
  - 4 colloquial paraphrases, e.g. "charges if I withdraw early from kotak multicap".
- **Scoped retrieval** mirrors the pipeline: a scheme query searches only that scheme's chunks, and a general query searches only `amfi`, `sebi` and `rta_help` chunks. **Global** search has no filter (it simulates a resolver miss).
- **Hybrid** retrieval takes the top 8 dense results and the top 8 BM25 results and merges them with reciprocal rank fusion (k = 60), as in architecture §6.4.
- **Hardware:** a CPU-only Windows laptop.

**Caveat:** 44 queries is small. One query is about 2.3 points, so differences under about 5 points are noise. The numbers are good enough to choose a design, not to report as accuracy; that is Phase 7's job.

## 3. Chunking strategies

| Strategy | Chunks | Median / max tokens | Total tokens indexed | Hybrid hit@1 | Hybrid hit@5 | MRR | Evidence tokens sent to LLM (top 5, median) |
|---|---|---|---|---|---|---|---|
| **A** – plan as written: ~400-token windows, 50 overlap, packed across headings | 253 | 412 / 417 | 96,857 | 0.82 | 0.98 | 0.90 | **1,756** |
| **B** – one chunk per section; oversized sections split at sentence/item boundaries to ≤ 350 tokens; header on every piece | 588 | 106 / 346 | 90,285 | 0.93 | 0.98 | 0.95 | — |
| **C** – B, plus noise removed and holdings left to SQL | **378** | 89 / 343 | **42,400** | **0.91–0.95** | 0.96–0.98 | 0.93–0.96 | **543** |

*(Strategy A is scored by content match with case and whitespace normalised. A heading-based match unfairly gives it about 0.3, because windows straddle several headings.)*

What this shows:

1. **Packing across headings is the wrong default for this corpus.** It ranks the right section first less often, and it sends **3.2× more evidence tokens** to Groq. That matters for the free-tier tokens-per-minute limit. It also mixes several facts into one chunk ("Costs" + "NAV" + "returns"), which weakens the validator's "every number must be in the cited evidence" check, because almost any number then appears in the chunk.
2. **Overlap is unnecessary for Groww sections.** They are fact lists. Splitting at `;` and sentence boundaries and repeating the lead sentence (the scheme name) in each piece keeps every piece self-describing. For prose (AMFI and SEBI articles) a one-sentence overlap is enough.
3. **Dropping holdings from the text index halves the index** (90k → 42k tokens) with no loss elsewhere. The only query that got worse was "Does Parag Parikh Flexi Cap hold Alphabet shares?", which should be answered from `scheme_holdings` by the facts-first lookup. The resolver already plans a `holdings` field intent.
4. The remaining miss, "how much do i need to start investing **monthly** in ppfas", is a vocabulary gap ("monthly" vs "SIP"). Phase 4's field-intent map handles it, and synonym-enriched headers would help too (§6.1).

## 4. Embedding models (strategy C, scoped)

| Model | Dim | Max tokens | Dense hit@1 | Dense hit@5 | Hybrid hit@1 | Hybrid hit@5 | Index time (378 chunks, CPU) | Query latency |
|---|---|---|---|---|---|---|---|---|
| `BAAI/bge-small-en-v1.5` + query instruction | 384 | 512 | 0.84 | 0.98 | **0.95** | 0.95 | 15 s | 5 ms |
| `BAAI/bge-small-en-v1.5`, no instruction | 384 | 512 | 0.82 | 0.93 | 0.89 | 0.95 | 15 s | 4 ms |
| `BAAI/bge-base-en-v1.5` + instruction | 768 | 512 | 0.93 | 0.98 | 0.91 | 0.98 | 47 s | 21 ms |
| `intfloat/e5-small-v2` (`query:`/`passage:`) | 384 | 512 | 0.93–0.95 | 0.98 | 0.91 | 0.98 | 17 s | 4 ms |
| `sentence-transformers/all-MiniLM-L6-v2` | 384 | 256 | 0.84 | 0.98 | 0.86 | 0.95 | 8 s | 2 ms |

What this shows:

- **Once scoping and hybrid fusion are applied, the three 384- and 768-dim retrieval models are within noise of each other** (±2 queries). The scheme filter does most of the work: a scoped scheme search ranks only 15–25 chunks.
- **The bge query instruction matters.** Adding `"Represent this sentence for searching relevant passages: "` to queries (not to passages) raised hybrid hit@1 from 0.89 to 0.95. The original plan doesn't mention it.
- bge-base gives better dense-only ranking but costs 3× the indexing time, 4× the query latency and twice the storage, and it doesn't improve hybrid results. It is not worth it at this corpus size.
- MiniLM truncates at 256 tokens, so it would cut off the longer AMFI sections. Rejected.
- e5-small-v2 is a good fallback if bge-small regresses in Phase 7.

### 4.1 Relevance scores and the no-evidence threshold

| | bge-small cosine |
|---|---|
| Gold chunk, worst case | 0.59 |
| Gold chunk, p10 | 0.70 |
| "What is the colour of HDFC Flexi Cap?" (should be no-evidence) | **0.74** |
| "Tell me a joke about bonds" | 0.63 |
| Unrelated queries (cricket, pizza, weather) | 0.49–0.50 |

- **`min_relevance: 0.35` in `settings.yaml` does nothing for bge.** bge cosines sit between 0.45 and 0.9 for almost any text.
- **No single cosine threshold separates in-domain nonsense from real questions.** The "colour" query scores higher than the weakest real question.
- So the no-evidence decision cannot rely on dense similarity alone. Combine:
  - a dense floor of about **0.60** (to be tuned in Phase 7), which only catches clearly off-topic text;
  - a **lexical check**: the best BM25 score in scope must be greater than 0, ignoring scheme-name tokens;
  - the Phase 4 **field-intent** result. A scheme plus an unknown attribute ("colour") becomes no-evidence or a clarifying question before retrieval.

## 5. Vector store

| Option | Fit for ~400 chunks × 384 dims (≈ 0.6 MB) |
|---|---|
| **Chroma 1.5.9** (already installed) | Persistent, metadata `where` filter on `scheme_id` / `doc_type`, delete by metadata. HNSW is approximate, but at this size it is effectively exact. |
| NumPy matrix + SQLite metadata | Exact, simplest, sub-millisecond. You write the filtering and persistence yourself. |
| sqlite-vec / LanceDB / Qdrant local | No advantage at this scale; adds a dependency. |

**Keep Chroma**, but configure it deliberately:

- Create the collection with **`metadata={"hnsw:space": "cosine"}`**. Chroma's default is L2: rankings are the same for normalised vectors, but distances aren't comparable to the cosine thresholds above. Score = `1 − distance`.
- Always supply embeddings yourself. *(Corrected during Phase 3: `embedding_function=None` is **not** enough. Chroma 1.5.9 still downloads and runs its default ONNX MiniLM whenever an `add` or `query` arrives without embeddings, so the wrapper must refuse such calls.)*
- Use deterministic `chunk_id`s (`<source_id>::<section_slug>::<n>`), so re-runs give identical IDs and counts. Replace a source with `delete(where={"source_id": sid})` followed by `add`.
- Store an **index manifest** with the embedding model, dimension, query instruction and `chunker_version`. If any of these changes, `python -m ingest` rebuilds the index from scratch; mixed embeddings in one collection are a silent failure mode.
- Hide the store behind a small `VectorIndex` interface (`upsert`, `delete_source`, `query(text, where, k)`). A NumPy backend can then replace it if Chroma becomes a problem on Windows or in deployment.

**BM25:**

- Rebuild the whole index on every run instead of updating it incrementally. It takes under 1 s for 378 chunks.
- Pickle it together with the chunk-ID list.
- Use a tokenizer that keeps decimals and amounts (`0.70`, `1.5`, `₹100`).
- Apply the scope filter after scoring.

## 6. Recommendations (adopted in Phase 3)

### 6.1 Chunking

1. **Unit = one parsed section.** Never merge sections. Split a section only if it is over **350 tokens** (bge tokenizer):
   - scheme pages: at `;` and sentence boundaries, with the lead sentence (scheme name) repeated in every piece and no overlap;
   - prose: at sentence boundaries, with one sentence of overlap.
2. **Header** on every chunk, used both for embedding and for BM25: `[<scheme name or source title>] <section heading>`. Add short synonym tails to the fixed Groww headings, for example:
   - `Costs and exit load (expense ratio, TER, charges, exit load, stamp duty)`
   - `Investment limits and lock-in (minimum SIP, monthly investment, lumpsum, lock-in)`
   - `NAV and fund size (NAV, AUM)`
3. **Exclude from the text index**, because they are answered from SQL by facts-first lookup:
   - `Portfolio holdings *` → `scheme_holdings`;
   - `Similar funds listed on Groww` → `scheme_peers` (only for an explicit "similar funds" intent, to avoid misattributing peer figures).
4. **Drop noise** with deterministic rules:
   - navigation headings;
   - menu-like blocks;
   - "Navigated to …" lines;
   - sections under 12 tokens;
   - any section containing a phone-number, email or PAN-like pattern (reuse the Phase 4 PII regexes).

   Log each dropped section with its reason in the ingest summary.
5. **Metadata** on each chunk:
   - existing: `chunk_id`, `source_id`, `source_url`, `scheme_id`, `amc`, `doc_type`, `section`, `fetched_at`;
   - new: `section_kind` (`facts | returns | risk | manager | fund_house | identifiers | objective | prose`), `as_of` (date for returns and NAV sections) and `chunker_version`.
6. Expected result on the current data: **≈ 380 chunks, median ≈ 90 tokens, max ≤ 350, about 42k tokens in total**.

### 6.2 Embedding

`BAAI/bge-small-en-v1.5`, normalised:

- the query instruction prefix is applied to **queries only**;
- `max_seq_length = 512`, and the chunker guarantees no chunk exceeds it;
- e5-small-v2 is the documented fallback.

### 6.3 Retrieval settings to carry into Phase 5 and Phase 7

- Change `min_relevance` from 0.35 to a **starting value of 0.60**, and treat it as a floor, not the no-evidence rule (see §4.1).
- Use `final_top_k` of 5. Chunks are small, so 5 chunks is about 550 tokens of evidence.

### 6.4 Follow-ups outside Phase 3

| Item | Phase |
|---|---|
| ~~Glossary pagination in the fetcher~~ (done 2026-11-09) | 2 |
| `holdings` and `similar funds` intents must route to SQL | 4 / 5 |
| Combined no-evidence rule (dense floor + BM25 > 0 + field intent) | 5 |
| Add "what does expense ratio mean", "does X hold Y" and "monthly SIP" phrasings to the golden set | 7 |
