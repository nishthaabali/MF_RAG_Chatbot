"""Chunking x embedding x retrieval benchmark over data/parsed.

usage: python bench.py <parsed_dir> <out_json> [model ...]
"""
import json, glob, os, re, sys, time, statistics as st
from collections import defaultdict
from transformers import AutoTokenizer
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer
import numpy as np

PARSED, OUT = sys.argv[1], sys.argv[2]
MODELS = sys.argv[3:] or ["BAAI/bge-small-en-v1.5"]
TOK = AutoTokenizer.from_pretrained("BAAI/bge-small-en-v1.5")
ntok = lambda t: len(TOK.encode(t, add_special_tokens=False))

docs = [json.load(open(p, encoding="utf-8")) for p in sorted(glob.glob(os.path.join(PARSED, "*.json")))]

# ---------------------------------------------------------------- cleaning
NAV_HEADINGS = {"dark", "view more", "videos", "quick links", "media", "other links"}
def is_noise(doc, sec):
    h, t = (sec["heading"] or "").strip(), sec["text"].strip()
    if h.lower() in NAV_HEADINGS: return "nav-heading"
    if t.startswith("Navigated to"): return "navigated-to"
    if ntok(t) < 12: return "too-short"
    lines = [l for l in t.split("\n") if l.strip()]
    if len(lines) >= 6 and sum(len(l.split()) <= 4 for l in lines) / len(lines) > 0.6: return "menu-like"
    if re.search(r"\b[6-9]\d{9}\b", t): return "contains-phone"
    return None

def is_structured(doc, sec):
    h = sec["heading"] or ""
    return doc["type"] == "scheme_page" and h.startswith("Portfolio holdings")

# ---------------------------------------------------------------- splitting helpers
def split_units(text):
    """Split into sentence/list-item units; ';' separates items in Groww data sections."""
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(])|;\s+|\n+", text)
    return [p.strip() for p in parts if p.strip()]

def pack(units, max_tok, overlap_units=1):
    out, cur, cur_t = [], [], 0
    for u in units:
        t = ntok(u)
        if cur and cur_t + t > max_tok:
            out.append(cur)
            cur = cur[-overlap_units:] if overlap_units else []
            cur_t = sum(ntok(x) for x in cur)
        cur.append(u); cur_t += t
    if cur: out.append(cur)
    return out

def header(doc, sec):
    return f"[{doc['title']}] {sec['heading'] or ''}".strip()

def mk(doc, sec, text, i=0):
    return {"source_id": doc["source_id"], "type": doc["type"], "scheme_id": doc.get("scheme_id"),
            "section": sec["heading"] or "", "text": text, "i": i}

# ---------------------------------------------------------------- strategies
def strat_window(doc_list, size=400, overlap=50):
    """Plan as written: whole doc as one token stream, ~400 tok windows, 50 overlap."""
    chunks = []
    for d in doc_list:
        body = "\n\n".join(((s["heading"] or "") + "\n" + s["text"]).strip() for s in d["sections"])
        ids = TOK.encode(body, add_special_tokens=False)
        step = size - overlap
        for k, start in enumerate(range(0, max(len(ids) - overlap, 1), step)):
            txt = TOK.decode(ids[start:start + size])
            # section label = heading whose text the window starts in (best effort)
            sec = next((s for s in d["sections"] if s["text"][:40] in txt), d["sections"][0])
            chunks.append(mk(d, sec, f"[{d['title']}] " + txt, k))
    return chunks

def strat_section(doc_list, max_tok=350, clean=False, drop_structured=False):
    chunks = []
    for d in doc_list:
        for s in d["sections"]:
            if clean and is_noise(d, s): continue
            if drop_structured and is_structured(d, s): continue
            h = header(d, s)
            body = s["text"]
            if ntok(h) + ntok(body) + 2 <= max_tok:
                chunks.append(mk(d, s, h + "\n" + body)); continue
            # scheme-page data sections repeat the scheme-name lead sentence: keep it in every piece
            units = split_units(body)
            lead = units[0] if d["type"] == "scheme_page" else None
            rest = units[1:] if lead else units
            budget = max_tok - ntok(h) - (ntok(lead) if lead else 0) - 4
            for k, grp in enumerate(pack(rest, budget, overlap_units=0 if lead else 1)):
                txt = " ".join(([lead] if lead else []) + grp)
                chunks.append(mk(d, s, h + "\n" + txt, k))
    return chunks

STRATEGIES = {
    "A_window400": lambda: strat_window(docs),
    "B_section350": lambda: strat_section(docs, 350),
    "C_section350_clean_noholdings": lambda: strat_section(docs, 350, clean=True, drop_structured=True),
}

# ---------------------------------------------------------------- queries (gold = source_id + heading regex)
Q = [
 # scheme facts / sections (scheme name in query; resolver would also filter)
 ("What is the exit load of Kotak Multi Cap Fund?", "groww-kotak-multi-cap", r"Costs"),
 ("expense ratio parag parikh flexi cap", "groww-ppfas-flexi-cap", r"Costs"),
 ("Minimum SIP amount for HDFC Flexi Cap", "groww-hdfc-flexi-cap", r"limits"),
 ("riskometer of ICICI Prudential Large Cap", "groww-icici-large-cap", r"Scheme details"),
 ("benchmark index of Nippon India Large Cap Fund", "groww-nippon-large-cap", r"Scheme details"),
 ("Who manages the Bandhan Small Cap fund", "groww-bandhan-small-cap", r"Scheme details|Fund manager"),
 ("What is the investment objective of Edelweiss Mid Cap?", "groww-edelweiss-mid-cap", r"objective"),
 ("NAV of HDFC Nifty Next 50 index fund", "groww-hdfc-nifty-next-50", r"NAV"),
 ("AUM of ABSL Small Cap fund", "groww-absl-small-cap", r"NAV"),
 ("3 year return of Aditya Birla Sun Life Flexi Cap", "groww-absl-flexi-cap", r"Annualised|Absolute"),
 ("SIP returns of HDFC Pharma and Healthcare fund", "groww-hdfc-pharma-healthcare", r"SIP returns"),
 ("Sharpe ratio of ICICI Prudential Corporate Bond Fund", "groww-icici-corporate-bond", r"Risk measures"),
 ("rank of Kotak Multi Cap in its category", "groww-kotak-multi-cap", r"Rank"),
 ("ISIN of Parag Parikh Flexi Cap direct growth", "groww-ppfas-flexi-cap", r"Identifiers"),
 ("Is there a lock-in for HDFC Silver ETF FoF?", "groww-hdfc-silver-etf-fof", r"limits"),
 ("Rajeev Thakkar education and experience", "groww-ppfas-flexi-cap", r"Rajeev Thakkar"),
 ("Does Parag Parikh Flexi Cap hold Alphabet / Google shares?", "groww-ppfas-flexi-cap", r"Portfolio holdings"),
 ("category average 5 year return for credit risk funds", "groww-absl-credit-risk", r"category average"),
 ("PPFAS Mutual Fund registered office address", "groww-ppfas-flexi-cap", r"Fund house"),
 ("launch date of ABSL BSE total market index fund", "groww-absl-bse-total-market", r"Scheme details"),
 ("funds similar to HDFC Flexi Cap", "groww-hdfc-flexi-cap", r"Similar"),
 ("stamp duty on Nippon India large cap", "groww-nippon-large-cap", r"Costs"),
 # general (no scheme)
 ("What is the lock-in period of an ELSS fund?", "amfi-elss", r""),
 ("How do I download my capital gains statement?", "rta-cams-capital-gains", r"Capital Gain"),
 ("How can I get a consolidated account statement (CAS)?", "rta-cams-cas|sebi-cas", r""),
 ("What is an exit load?", "amfi-exit-load", r""),
 ("What is a riskometer and how is it calculated?", "amfi-riskometer", r""),
 ("What is a systematic investment plan?", "amfi-sip", r""),
 ("What does expense ratio mean", "amfi-exit-load|amfi-glossary", r""),
 ("what is a benchmark for a mutual fund", "amfi-glossary", r"Benchmark"),
 ("What is credit risk in debt funds?", "amfi-glossary", r"Credit risk"),
 ("difference between direct plan and regular plan", "amfi-glossary", r"Direct Plans"),
 ("what is a close ended scheme", "amfi-glossary", r"Close-Ended"),
 ("How should I compare two mutual fund schemes?", "amfi-compare-performance", r""),
 ("Should I go through a distributor or advisor?", "amfi-choosing-advisor", r""),
 ("low risk vs high risk investments", "amfi-risk", r"Low Risk"),
 ("What is a mutual fund and how does it work?", "amfi-what-is-mutual-fund|amfi-mf-basics|amfi-mfsh-home", r""),
 ("things a smart investor checks before investing", "sebi-smart-investor", r""),
 ("how to keep my demat account secure", "sebi-secure-account", r""),
 ("dividend reinvestment option meaning", "amfi-glossary", r"Dividend Reinvest"),
 # paraphrase / colloquial
 ("charges if I withdraw early from kotak multicap", "groww-kotak-multi-cap", r"Costs"),
 ("how much do i need to start investing monthly in ppfas", "groww-ppfas-flexi-cap", r"limits"),
 ("how risky is the HDFC corporate bond fund", "groww-hdfc-corporate-bond", r"Scheme details|Risk measures"),
 ("tax benefit 80C mutual fund", "amfi-elss", r""),
]

def is_hit(c, gold_src, gold_head):
    return re.fullmatch(gold_src, c["source_id"]) and re.search(gold_head, c["section"] or "", re.I)

def scope(c, q_src):
    """Pipeline filter: scheme queries -> that scheme's chunks; general -> amfi/sebi/rta."""
    if q_src.startswith("groww-"): return c["source_id"] == q_src
    return c["type"] in ("amfi", "sebi", "rta_help")

def ranks(order, chunks, gold_src, gold_head):
    for r, i in enumerate(order):
        if is_hit(chunks[i], gold_src, gold_head): return r + 1
    return None

def metrics(rank_list, k=5):
    hit1 = np.mean([r == 1 for r in rank_list]); hitk = np.mean([r is not None and r <= k for r in rank_list])
    mrr = np.mean([1 / r if r else 0 for r in rank_list])
    return {"hit@1": round(float(hit1), 3), f"hit@{k}": round(float(hitk), 3), "mrr": round(float(mrr), 3)}

QUERY_PREFIX = {"BAAI/bge-small-en-v1.5": "Represent this sentence for searching relevant passages: ",
                "BAAI/bge-base-en-v1.5": "Represent this sentence for searching relevant passages: ",
                "intfloat/e5-small-v2": "query: ", "intfloat/multilingual-e5-small": "query: ",
                "nomic-ai/nomic-embed-text-v1.5": "search_query: "}
DOC_PREFIX = {"intfloat/e5-small-v2": "passage: ", "intfloat/multilingual-e5-small": "passage: ",
              "nomic-ai/nomic-embed-text-v1.5": "search_document: "}

bm_tok = lambda t: re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", t.lower())

results = {"chunk_stats": {}, "runs": [], "noise_dropped": {}}
for sname, fn in STRATEGIES.items():
    chunks = fn()
    tl = [ntok(c["text"]) for c in chunks]
    results["chunk_stats"][sname] = {"n": len(chunks), "median_tok": st.median(tl), "max_tok": max(tl),
                                     "over_512": sum(t > 512 for t in tl), "total_tok": sum(tl)}
    bm = BM25Okapi([bm_tok(c["text"]) for c in chunks])
    bm_scores = [bm.get_scores(bm_tok(q)) for q, *_ in Q]
    for model_name in MODELS:
        kw = {"trust_remote_code": True} if "nomic" in model_name else {}
        m = SentenceTransformer(model_name, device="cpu", **kw)
        m.max_seq_length = min(m.max_seq_length or 512, 512)
        t0 = time.time()
        E = m.encode([DOC_PREFIX.get(model_name, "") + c["text"] for c in chunks], normalize_embeddings=True, batch_size=32)
        t_idx = time.time() - t0
        for qp_name, qp in (("prefix", QUERY_PREFIX.get(model_name, "")), ("noprefix", "")):
            if qp_name == "prefix" and not qp: continue
            t0 = time.time()
            QE = m.encode([qp + q for q, *_ in Q], normalize_embeddings=True)
            t_q = (time.time() - t0) / len(Q)
            per = {"dense_global": [], "dense_scoped": [], "bm25_scoped": [], "hybrid_scoped": []}
            gold_scores, best_wrong = [], []
            for qi, (q, gs, gh) in enumerate(Q):
                sims = E @ QE[qi]
                per["dense_global"].append(ranks(np.argsort(-sims), chunks, gs, gh))
                idx = [i for i, c in enumerate(chunks) if scope(c, gs)]
                d_ord = sorted(idx, key=lambda i: -sims[i]); b_ord = sorted(idx, key=lambda i: -bm_scores[qi][i])
                per["dense_scoped"].append(ranks(d_ord, chunks, gs, gh))
                per["bm25_scoped"].append(ranks(b_ord, chunks, gs, gh))
                rrf = defaultdict(float)
                for o in (d_ord[:8], b_ord[:8]):
                    for r, i in enumerate(o): rrf[i] += 1 / (60 + r + 1)
                per["hybrid_scoped"].append(ranks(sorted(rrf, key=lambda i: -rrf[i]), chunks, gs, gh))
                hits = [sims[i] for i in idx if is_hit(chunks[i], gs, gh)]
                if hits: gold_scores.append(float(max(hits)))
            run = {"strategy": sname, "model": model_name, "query_prefix": qp_name,
                   "index_seconds": round(t_idx, 1), "query_ms": round(t_q * 1000, 1),
                   "dim": int(E.shape[1]), "gold_cos_min": round(min(gold_scores), 3),
                   "gold_cos_p10": round(float(np.percentile(gold_scores, 10)), 3)}
            for k_, v in per.items(): run[k_] = metrics(v)
            run["misses_hybrid"] = [Q[i][0] for i, r in enumerate(per["hybrid_scoped"]) if not r or r > 5]
            run["misses_dense_global_top5"] = [Q[i][0] for i, r in enumerate(per["dense_global"]) if not r or r > 5]
            results["runs"].append(run)
            print(json.dumps({k: run[k] for k in run if not k.startswith("misses")}), flush=True)

# noise report for the clean strategy
for d in docs:
    for s in d["sections"]:
        why = is_noise(d, s)
        if why: results["noise_dropped"].setdefault(d["source_id"], []).append([s["heading"], why])

# off-topic queries: best scoped cosine (for min_relevance calibration) with the first model
OFF = ["What is the colour of HDFC Flexi Cap?", "Who won the cricket world cup?", "best pizza in Mumbai",
       "What is the weather today", "Tell me a joke about bonds"]
m = SentenceTransformer(MODELS[0], device="cpu")
chunks = STRATEGIES["C_section350_clean_noholdings"]()
E = m.encode([c["text"] for c in chunks], normalize_embeddings=True)
QE = m.encode([QUERY_PREFIX.get(MODELS[0], "") + q for q in OFF], normalize_embeddings=True)
results["offtopic_best_cos"] = {q: round(float((E @ QE[i]).max()), 3) for i, q in enumerate(OFF)}
json.dump(results, open(OUT, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
print(json.dumps(results["chunk_stats"], indent=1)); print(results["offtopic_best_cos"])
