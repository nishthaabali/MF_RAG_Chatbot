"""Tune `retrieval.min_relevance` and the resolver's fuzzy-match thresholds (no LLM calls).

    python eval/tune.py [--out eval/results/tuning-<date>.json]

1. min_relevance. Every golden turn that reaches retrieval is retrieved once with the threshold
   at 0, recording the best dense cosine, whether a structured fact answered it, and whether the
   content-word rule passed. The no-evidence decision is then replayed for each threshold:
   - answerable turns (expected type `answer`) wrongly refused = false refusals;
   - UNANSWERABLE questions below (plus golden no_evidence cases) answered anyway = false answers.
   Only turns without a structured fact depend on the threshold; facts always answer.

2. Fuzzy matching (scheme_resolver.FUZZY_MEAN_MIN / FUZZY_TOKEN_MIN). The resolver runs over
   every golden turn that names a scheme, the TYPOS below (must still resolve) and UNCOVERED
   names (must never resolve to a covered scheme), for a grid of thresholds.

The recommendation is the setting with the fewest errors (false refusals / wrong resolutions
first), keeping the current value when it is among the best and otherwise taking the middle of
the widest equally good range rather than an edge. min_relevance is chosen on general questions
only (cosine cannot separate scheme-scoped ones) and kept at least --margin below the
lowest-scoring answerable question, because a false refusal loses a correct answer while a
false pass still meets the model's `answerable` check.
"""

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from checks import EVAL_DIR, load_golden

from app.config import get_settings
from app.pipeline import scheme_resolver
from app.pipeline.orchestrator import AnswerPipeline

# Questions the corpus cannot answer. Scheme-scoped ones are the hard case: every chunk of a
# scheme shares its name, so cosine alone can't reject them (chunking-research.md §4.1).
UNANSWERABLE = [
    "What is the colour of HDFC Flexi Cap?",
    "Who is the CEO of the company behind Kotak Multi Cap Fund?",
    "Who audits the accounts of Bandhan Small Cap Fund?",  # (its office address IS on the page)
    "What is the customer care number for Edelweiss Mid Cap?",
    "When is the next dividend date of Parag Parikh Flexi Cap?",
    "What was the closing price of ICICI Prudential Large Cap on its launch day in 1990?",
    "How many employees work on Nippon India Large Cap?",
    "What is the GST number of HDFC Silver ETF FoF?",
    "Which stock exchange lists the mutual fund penalty for late SIP payment?",
    "What is the interest rate on a mutual fund loan against units?",
    "How do I change the password of my bank netbanking account?",
    "What is the maximum speed of a mutual fund?",
    "Which planet is the riskometer named after?",
    "What is the refund policy for unused mutual fund vouchers?",
    "What is the minimum age to open a demat account in Japan?",
]

TYPOS = {
    "expense ratio of Kotak Multicp": "kotak-multi-cap",
    "exit load of Bandhn Small Cap": "bandhan-small-cap",
    "min sip edelwiess mid cap": "edelweiss-mid-cap",
    "Parag Parik Flexi Cap expense ratio": "ppfas-flexi-cap",
    "benchmark of Nipon India Large Cap": "nippon-large-cap",
    "ICICI Prudentail Large Cap exit load": "icici-large-cap",
    "Aditya Birla Sun Lfe Flexi Cap TER": "absl-flexi-cap",
    "HDFC Flexy Cap expense ratio": "hdfc-flexi-cap",
    "HDFC Pharma and Helthcare exit load": "hdfc-pharma-healthcare",
    "HDFC Corprate Bond riskometer": "hdfc-corporate-bond",
    "Aditya Birla Sun Life Credit Rsik Fund riskometer": "absl-credit-risk",
    "ABSL Small Cpa fund lumpsum": "absl-small-cap",
}

UNCOVERED = [
    "expense ratio of SBI Bluechip Fund",
    "Axis Bluechip Fund exit load",
    "HDFC Mid Cap Opportunities Fund expense ratio",
    "Mirae Asset Large Cap Fund benchmark",
    "Kotak Emerging Equity Fund exit load",
    "Quant Small Cap Fund riskometer",
    "Nippon India Small Cap Fund expense ratio",
    "ICICI Prudential Value Discovery Fund exit load",
    "Parag Parikh Tax Saver Fund lock-in",
    "HDFC Top 100 Fund expense ratio",
    "Kotak Flexicap Fund exit load",
    "Bandhan Sterling Value Fund expense ratio",
    "Edelweiss Balanced Advantage Fund benchmark",
    "ABSL Frontline Equity Fund expense ratio",
    "HDFC Balanced Advantage Fund exit load",
]

THRESHOLDS = [round(0.40 + 0.025 * i, 3) for i in range(15)]  # 0.40 .. 0.75
FUZZY_MEAN = [80, 82, 84, 86, 88, 90, 92, 94, 96]
FUZZY_TOKEN = [70, 75, 80]


async def relevance_rows(pipeline, golden) -> list[dict]:
    rows = []

    async def probe(question: str, expect: str, source: str, ctx_scheme=None) -> None:
        route = await pipeline.router.route_message(question, active_scheme_id=ctx_scheme)
        if route.retrieval is None:
            return
        res = await pipeline.retriever.retrieve(route.retrieval)
        structured = any(e.kind != "chunk" for e in res.evidence)
        rows.append({
            "q": question, "expect": expect, "source": source,
            "scope": "scheme" if res.scheme_id else "general",
            "top_score": res.top_score, "structured": structured,
            "supported": res.status == "ok" or res.reason == "below min_relevance",
            "no_hits": res.reason == "no chunks",
        })

    for case in golden.cases:
        active = None
        for turn in case.turns:
            types = turn.expect.get("type") or []
            if types == ["answer"]:
                await probe(turn.q, "answer", case.id, active)
            elif "no_evidence" in types and "answer" not in types:
                await probe(turn.q, "no_evidence", case.id, active)
            active = turn.expect.get("scheme") or active
    for q in UNANSWERABLE:
        await probe(q, "no_evidence", "UNANSWERABLE")
    return rows


def decide(row: dict, threshold: float) -> str:
    if row["structured"]:
        return "ok"
    if row["no_hits"] or row["top_score"] is None or row["top_score"] < threshold:
        return "no_evidence"
    return "ok" if row["supported"] else "no_evidence"


def sweep_relevance(rows: list[dict]) -> list[dict]:
    out = []
    for t in THRESHOLDS:
        false_refusals = [r["q"] for r in rows if r["expect"] == "answer"
                          and decide(r, t) == "no_evidence"]
        false_answers = [r["q"] for r in rows if r["expect"] == "no_evidence"
                         and decide(r, t) == "ok"]
        general = {r["q"] for r in rows if r["scope"] == "general"}
        out.append({"threshold": t, "false_refusals": false_refusals,
                    "false_answers": false_answers,
                    "general_false_refusals": [q for q in false_refusals if q in general],
                    "general_false_answers": [q for q in false_answers if q in general]})
    return out


def resolver_cases(golden) -> tuple[list[tuple[str, str]], list[str]]:
    positives = []
    for case in golden.cases:
        first = case.turns[0]
        sid = first.expect.get("scheme")
        types = first.expect.get("type") or []
        if sid and case.bucket != "pii" and "clarify" not in types:
            positives.append((first.q, sid))
    positives += list(TYPOS.items())
    return positives, UNCOVERED


def sweep_fuzzy(pipeline, positives, negatives) -> list[dict]:
    resolver = pipeline.router.resolver
    saved = scheme_resolver.FUZZY_MEAN_MIN, scheme_resolver.FUZZY_TOKEN_MIN
    out = []
    try:
        for token in FUZZY_TOKEN:
            for mean in FUZZY_MEAN:
                scheme_resolver.FUZZY_MEAN_MIN, scheme_resolver.FUZZY_TOKEN_MIN = mean, token
                missed, wrong = [], []
                for q, sid in positives:
                    res, _ = resolver.analyse(q, None, ())
                    if res.scheme_id != sid:
                        (wrong if res.scheme_id else missed).append(q)
                for q in negatives:
                    res, _ = resolver.analyse(q, None, ())
                    if res.status in ("resolved", "context") and res.scheme_id:
                        wrong.append(q)
                out.append({"fuzzy_mean_min": mean, "fuzzy_token_min": token,
                            "missed": missed, "wrong": wrong})
    finally:
        scheme_resolver.FUZZY_MEAN_MIN, scheme_resolver.FUZZY_TOKEN_MIN = saved
    return out


def middle_of_best(rows: list[dict], key, bad, miss, current=None) -> dict:
    """Fewest `bad` errors, then fewest `miss`. Keeps `current` when it is among those;
    otherwise the middle of the longest run of equally good settings (away from the edges)."""
    score = [(len(r[bad]), len(r[miss])) for r in rows]
    top = min(score)
    best = [r for r, s in zip(rows, score, strict=True) if s == top]
    for r in best:
        if current is not None and abs(r[key] - current) < 1e-9:
            return r
    runs, run = [], []
    for r in rows:
        if r in best:
            run.append(r)
        else:
            if run:
                runs.append(run)
            run = []
    if run:
        runs.append(run)
    longest = max(runs, key=len)
    return longest[(len(longest) - 1) // 2]


async def main_async(args) -> int:
    golden = load_golden()
    settings = get_settings()
    current_relevance = settings.retrieval.min_relevance
    settings.retrieval.min_relevance = 0.0
    pipeline = AnswerPipeline.from_settings(settings, use_llm=False)

    print("Retrieving every golden turn once (threshold 0) ...", flush=True)
    rows = await relevance_rows(pipeline, golden)
    rel = sweep_relevance(rows)
    depends = [r for r in rows if not r["structured"]]
    n_general = sum(r["scope"] == "general" for r in depends)
    print(f"\nmin_relevance sweep: {len(rows)} retrieval turns, {len(depends)} without a "
          f"structured fact ({n_general} general, {len(depends) - n_general} scheme-scoped; "
          f"{sum(r['expect'] == 'no_evidence' for r in depends)} unanswerable)")
    print("             general questions          all questions")
    print("  threshold  false refusals / answers   false refusals / answers")
    for r in rel:
        mark = "  <- current" if abs(r["threshold"] - current_relevance) < 1e-9 else ""
        print(f"  {r['threshold']:9.3f}  {len(r['general_false_refusals']):14} / "
              f"{len(r['general_false_answers']):<8}  {len(r['false_refusals']):14} / "
              f"{len(r['false_answers']):<7}{mark}")
    # Chosen on general questions: within one scheme every chunk shares the scheme name and
    # scores 0.72-0.89, so no cosine threshold separates scheme-scoped questions; the
    # content-word rule and the model's `answerable` flag handle those.
    # A false refusal loses a correct answer; a false pass still meets the model's answerable
    # flag. So stay at least `margin` below the lowest-scoring answerable question.
    lowest = min((r["top_score"] for r in depends if r["expect"] == "answer"
                  and r["scope"] == "general" and r["top_score"] is not None), default=1.0)
    safe = [r for r in rel if r["threshold"] <= lowest - args.margin + 1e-9] or rel[:1]
    pick_rel = middle_of_best(safe, "threshold", "general_false_refusals",
                              "general_false_answers", current=current_relevance)
    print(f"  recommended min_relevance: {pick_rel['threshold']:.3f} "
          f"(current {current_relevance}; chosen on general questions, at least "
          f"{args.margin} below the lowest answerable cosine {lowest:.3f})")
    for q in pick_rel["false_refusals"]:
        print(f"    refused at this threshold: {q}")
    for q in pick_rel["false_answers"]:
        print(f"    passes retrieval, left to the model's answerable flag: {q}")
    scores = sorted((r["top_score"], r["expect"], r["q"]) for r in depends
                    if r["top_score"] is not None)
    print("  cosines of turns without a fact (lowest answerable / highest unanswerable):")
    ans = [s for s in scores if s[1] == "answer"]
    neg = [s for s in scores if s[1] == "no_evidence"]
    for s in ans[:3]:
        print(f"    answerable   {s[0]:.3f}  {s[2]}")
    for s in neg[-3:]:
        print(f"    unanswerable {s[0]:.3f}  {s[2]}")

    positives, negatives = resolver_cases(golden)
    fz = sweep_fuzzy(pipeline, positives, negatives)
    print(f"\nFuzzy-match sweep: {len(positives)} scheme mentions ({len(TYPOS)} with typos), "
          f"{len(negatives)} uncovered names")
    print("  token  mean  missed  wrong")
    for r in fz:
        mark = ("  <- current" if (r["fuzzy_mean_min"], r["fuzzy_token_min"]) ==
                (scheme_resolver.FUZZY_MEAN_MIN, scheme_resolver.FUZZY_TOKEN_MIN) else "")
        print(f"  {r['fuzzy_token_min']:5}  {r['fuzzy_mean_min']:4}  {len(r['missed']):6}  "
              f"{len(r['wrong']):5}{mark}")
    by_token = {t: [r for r in fz if r["fuzzy_token_min"] == t] for t in FUZZY_TOKEN}
    current_token = by_token.get(scheme_resolver.FUZZY_TOKEN_MIN) or fz
    pick_fz = middle_of_best(current_token, "fuzzy_mean_min", "wrong", "missed",
                             current=scheme_resolver.FUZZY_MEAN_MIN)
    print(f"  recommended (token {pick_fz['fuzzy_token_min']}): fuzzy_mean_min "
          f"{pick_fz['fuzzy_mean_min']} (current {scheme_resolver.FUZZY_MEAN_MIN})")
    for q in pick_fz["missed"]:
        print(f"    still missed: {q}")
    for q in pick_fz["wrong"]:
        print(f"    still wrong: {q}")

    out = args.out or EVAL_DIR / "results" / f"tuning-{date.today().isoformat()}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "date": date.today().isoformat(),
        "min_relevance": {"current": current_relevance, "recommended": pick_rel["threshold"],
                          "sweep": rel, "rows": rows},
        "fuzzy": {"current": [scheme_resolver.FUZZY_MEAN_MIN, scheme_resolver.FUZZY_TOKEN_MIN],
                  "recommended": [pick_fz["fuzzy_mean_min"], pick_fz["fuzzy_token_min"]],
                  "sweep": fz},
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--margin", type=float, default=0.05,
                    help="min distance below the lowest answerable cosine (default 0.05)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
