"""Run the golden evaluation set (eval/golden.yaml) through the full answer pipeline.

    python eval/run.py                         # every case, real Groq API
    python eval/run.py --bucket pii --bucket advisory
    python eval/run.py --case sf-kotak-exit -v # one case, print the replies
    python eval/run.py --rerun-failures eval/results/2026-11-10.json
    python eval/run.py --no-llm                # templates only: free, checks routing/format
    python eval/run.py --set retrieval.min_relevance=0.55 --compare eval/results/2026-11-10.json

Every case is checked for its route (reply type), scheme, citation and values (see the header
of golden.yaml), and every reply for the format rules: <= 3 sentences, exactly one allowlisted
link, the footer with the cited source's fetch date, no advice or judgement wording. PII cases
must also be blocked with only the placeholder stored, make no LLM call, and leave no trace of
the personal detail in logs. No log record of any case may contain the message text.

Prints pass rates per bucket, the Phase 7 gates from golden.yaml, and the failures, and saves
everything to eval/results/<date>.json (-nollm for --no-llm; -2, -3 ... for repeat runs).

Groq budget: one full run makes ~120 answer calls of ~1.2K tokens (~60% of the free tier's
200K tokens/day) and takes ~20 minutes at 8K tokens/minute. Batch runs wait for rate budget
(--max-wait, default 300 s) instead of falling back. A reply produced because the LLM was
unavailable is reported as such, so it isn't mistaken for a model or pipeline failure.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from checks import (
    BUCKETS,
    GOLDEN_FILE,
    CitationContext,
    TurnCheck,
    check_compliance,
    check_expectations,
    check_format,
    load_golden,
    load_sources,
    schema_problems,
)

from app.config import get_settings
from app.pipeline import llm as llm_module
from app.pipeline import scheme_resolver
from app.pipeline.orchestrator import AnswerPipeline, ThreadContext
from app.pipeline.pii_guard import WITHHELD_PLACEHOLDER

RESULTS_DIR = Path(__file__).resolve().parent / "results"
QUIET = ("httpx", "httpcore", "sentence_transformers", "chromadb", "transformers", "groq",
         "urllib3", "huggingface_hub", "filelock")


# ------------------------------------------------------------------------------ instrumentation
class Recorder(logging.Handler):
    """Keeps every formatted log record of the current case (for the no-text-in-logs check)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:  # noqa: BLE001 - a bad record must not stop the eval
            self.messages.append(str(record.msg))


class LLMMeter:
    """Counts LLM calls and tokens by wrapping LLMClient.chat (classifier and generator)."""

    def __init__(self):
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.unavailable: Counter = Counter()
        original = llm_module.LLMClient.chat
        meter = self

        async def chat(client, *args, **kwargs):
            meter.calls += 1
            try:
                result = await original(client, *args, **kwargs)
            except llm_module.LLMUnavailable as exc:
                meter.unavailable[exc.reason] += 1
                raise
            meter.prompt_tokens += result.usage.prompt_tokens
            meter.completion_tokens += result.usage.completion_tokens
            return result

        llm_module.LLMClient.chat = chat

    def snapshot(self) -> tuple[int, int, int, int]:
        return (self.calls, self.prompt_tokens, self.completion_tokens,
                sum(self.unavailable.values()))


def apply_overrides(settings, pairs: list[str]) -> dict[str, str]:
    """--set section.key=value on the settings object, or resolver.fuzzy_mean_min=N."""
    applied = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        if key in ("resolver.fuzzy_mean_min", "resolver.fuzzy_token_min"):
            name = key.split(".", 1)[1].upper()
            setattr(scheme_resolver, name, float(value))
        else:
            section, _, attr = key.partition(".")
            target = getattr(settings, section)
            current = getattr(target, attr)
            setattr(target, attr, type(current)(value) if current is not None else value)
        applied[key] = value
    return applied


# ------------------------------------------------------------------------------ running
# Checks of published figures and their dates: they follow Groww's page, so they drift as soon
# as the data is refreshed. `--ignore-drift` reports them instead of failing (daily CI gate).
DRIFT_CHECKS = ("values", "as_of")


async def run_case(case, pipeline, ctx_checks, sources, meter, recorder, *, no_llm: bool,
                   ignore_drift: bool = False):
    thread = ThreadContext()
    turns_out = []
    for turn in case.turns:
        recorder.messages.clear()
        before = meter.snapshot()
        started = time.monotonic()
        result = await pipeline.answer(thread, turn.q)
        latency = int((time.monotonic() - started) * 1000)
        after = meter.snapshot()
        thread = result.next_context(thread)

        tc = TurnCheck()
        check_format(result.reply, ctx_checks, tc)
        check_compliance(result.reply, tc)
        check_expectations(result, turn.expect, ctx_checks, sources, tc, no_llm=no_llm)
        if tc.skipped:  # content not checkable without the LLM; keep only the format result
            tc.checks = {k: v for k, v in tc.checks.items() if k.startswith("format.")}
        drift = []
        if ignore_drift:
            for k in DRIFT_CHECKS:
                if tc.checks.get(k) is False:
                    drift.append(f"{k}: {tc.details.get(k, 'failed')}")
                tc.checks.pop(k, None)
        logs = "\n".join(recorder.messages)
        tc.add("privacy.no_text_in_logs", turn.q not in logs, "message text found in logs")
        if case.pii:
            trace = json.dumps(result.trace, default=str)
            tc.add("privacy.stored_placeholder", result.stored_user_text == WITHHELD_PLACEHOLDER,
                   "stored text is not the placeholder")
            tc.add("privacy.no_llm_call", after[0] == before[0],
                   f"{after[0] - before[0]} LLM call(s)")
            tc.add("privacy.not_in_logs_or_trace", case.pii not in logs and case.pii not in trace,
                   "personal detail in logs or trace")
        turns_out.append({
            "q": turn.q,
            "type": result.reply.type,
            "route": result.route,
            "scheme": result.active_scheme_id,
            "answer": result.reply.answer,
            "citation": result.reply.citation_url,
            "last_updated": str(result.reply.last_updated),
            "checks": tc.checks,
            "failures": tc.failures(),
            "skipped": tc.skipped,
            "drift": drift,
            "format_ok": tc.format_ok,
            "llm_calls": after[0] - before[0],
            "prompt_tokens": after[1] - before[1],
            "completion_tokens": after[2] - before[2],
            "llm_unavailable": after[3] > before[3] or str(
                result.trace.get("fallback_used", "")).startswith("llm_"),
            "fallback_used": result.trace.get("fallback_used"),
            "validator_failures": result.trace.get("validator_failures", []),
            "retrieval_top_score": result.trace.get("retrieval_top_score"),
            "latency_ms": latency,
        })
    skipped = any(t["skipped"] for t in turns_out)
    passed = all(not t["failures"] for t in turns_out)
    return {"id": case.id, "bucket": case.bucket, "passed": passed, "skipped": skipped,
            "turns": turns_out}


def summarise(results: list[dict], golden) -> dict:
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        by_bucket[r["bucket"]].append(r)

    def rate(rows):
        counted = [r for r in rows if not r["skipped"]]
        if not counted:
            return None, 0, 0
        ok = sum(r["passed"] for r in counted)
        return ok / len(counted), ok, len(counted)

    buckets = {}
    for b in BUCKETS:
        if by_bucket.get(b):
            value, ok, n = rate(by_bucket[b])
            buckets[b] = {"pass_rate": value, "passed": ok, "counted": n,
                          "skipped": sum(r["skipped"] for r in by_bucket[b])}
    factual_rows = [r for b in golden.factual_buckets for r in by_bucket.get(b, [])]
    turns = [t for r in results for t in r["turns"]]
    format_ok = sum(t["format_ok"] for t in turns)
    follow = [t for r in by_bucket.get("follow_ups", []) for t in r["turns"]
              if "scheme" in t["checks"]]

    gates = {}
    for name, target in golden.targets.items():
        if name == "format":
            value = format_ok / len(turns) if turns else None
        elif name == "factual":
            value = rate(factual_rows)[0]
        else:
            value = buckets.get(name, {}).get("pass_rate")
        gates[name] = {"target": target, "value": value,
                       "met": None if value is None else value >= target - 1e-9}

    routes = Counter(t["route"] for t in turns)
    return {
        "buckets": buckets,
        "gates": gates,
        "follow_up_scheme_resolution": (sum(t["checks"]["scheme"] for t in follow) / len(follow)
                                        if follow else None),
        "replies": len(turns),
        "routes": dict(routes.most_common()),
        "llm_calls": sum(t["llm_calls"] for t in turns),
        "prompt_tokens": sum(t["prompt_tokens"] for t in turns),
        "completion_tokens": sum(t["completion_tokens"] for t in turns),
        "llm_unavailable_replies": sum(t["llm_unavailable"] for t in turns),
        "validator_retries": sum(1 for t in turns if t["validator_failures"]),
        "template_fallbacks": routes.get("template_fallback", 0),
    }


def print_report(summary: dict, results: list[dict], *, verbose: bool) -> None:
    print("\nBucket                  pass   counted  skipped")
    for b, s in summary["buckets"].items():
        value = "  -  " if s["pass_rate"] is None else f"{s['pass_rate']:6.1%}"
        print(f"  {b:22} {value} {s['passed']:>3}/{s['counted']:<3}   {s['skipped']}")
    print("\nGate                    value   target")
    for name, g in summary["gates"].items():
        value = "  -  " if g["value"] is None else f"{g['value']:6.1%}"
        mark = {True: "PASS", False: "FAIL", None: "n/a"}[g["met"]]
        print(f"  {name:22} {value}  >= {g['target']:.0%}  {mark}")
    fur = summary["follow_up_scheme_resolution"]
    if fur is not None:
        print(f"\nFollow-up turns resolved to the right scheme: {fur:.1%}")
    print(f"Replies: {summary['replies']}  routes: {summary['routes']}")
    print(f"LLM calls: {summary['llm_calls']}  tokens: {summary['prompt_tokens']} prompt + "
          f"{summary['completion_tokens']} completion  validator retries: "
          f"{summary['validator_retries']}  template fallbacks: {summary['template_fallbacks']}")
    if summary["llm_unavailable_replies"]:
        print(f"!! {summary['llm_unavailable_replies']} replies were produced while the LLM was "
              "unavailable (rate budget / API errors): re-run those cases before trusting them.")

    failed = [r for r in results if not r["passed"]]
    if failed:
        print(f"\nFailures ({len(failed)}):")
    for r in failed:
        for i, t in enumerate(r["turns"], start=1):
            if not t["failures"]:
                continue
            turn = f" turn {i}" if len(r["turns"]) > 1 else ""
            flag = " [LLM unavailable]" if t["llm_unavailable"] else ""
            print(f"- {r['id']}{turn} ({r['bucket']}){flag}: {'; '.join(t['failures'])}")
            print(f"    q: {t['q']}")
            print(f"    a: [{t['type']}/{t['route']}] {t['answer']}")
    if verbose:
        print("\nAll replies:")
        for r in results:
            for t in r["turns"]:
                status = "skip" if t["skipped"] else ("ok" if not t["failures"] else "FAIL")
                print(f"[{status}] {r['id']}: {t['q']}\n    [{t['type']}/{t['route']}] "
                      f"{t['answer']}\n    {t['citation']}  ({t['last_updated']})")


def compare(previous: Path, results: list[dict]) -> None:
    old = {r["id"]: r for r in json.loads(previous.read_text(encoding="utf-8"))["cases"]}
    fixed = [r["id"] for r in results if r["passed"] and r["id"] in old
             and not old[r["id"]]["passed"]]
    broke = [r["id"] for r in results if not r["passed"] and r["id"] in old
             and old[r["id"]]["passed"]]
    print(f"\nCompared with {previous.name}: {len(fixed)} fixed, {len(broke)} newly failing")
    for i in fixed:
        print(f"  + {i}")
    for i in broke:
        print(f"  - {i}")


def results_path(no_llm: bool, partial: bool) -> Path:
    stem = date.today().isoformat() + ("-nollm" if no_llm else "") + ("-partial" if partial else "")
    path = RESULTS_DIR / f"{stem}.json"
    n = 2
    while path.exists():
        path = RESULTS_DIR / f"{stem}-{n}.json"
        n += 1
    return path


async def main_async(args) -> int:
    golden = load_golden(args.golden)
    sources = load_sources()
    settings = get_settings()
    settings.llm.max_wait_seconds = args.max_wait
    overrides = apply_overrides(settings, args.set or [])

    cases = golden.cases
    if args.bucket:
        cases = [c for c in cases if c.bucket in args.bucket]
    if args.case:
        prefixes = [p.rstrip("*") for p in args.case if p.endswith("*")]
        cases = [c for c in cases
                 if c.id in args.case or any(c.id.startswith(p) for p in prefixes)]
    if args.rerun_failures:
        prev = json.loads(Path(args.rerun_failures).read_text(encoding="utf-8"))
        failed = {r["id"] for r in prev["cases"] if not r["passed"]}
        cases = [c for c in cases if c.id in failed]
    if not cases:
        print("no cases selected")
        return 2

    use_llm = not args.no_llm
    if use_llm and not settings.groq_api_key.get_secret_value():
        print("GROQ_API_KEY is not set: use --no-llm, or set it in .env", file=sys.stderr)
        return 2

    meter = LLMMeter()
    print(f"Loading pipeline (model {settings.llm.model if use_llm else 'none'}) ...", flush=True)
    pipeline = AnswerPipeline.from_settings(settings, use_llm=use_llm)
    problems = schema_problems(golden, sources, set(pipeline.catalog.schemes))
    if problems:
        print("golden.yaml problems:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 2
    ctx_checks = CitationContext.from_catalog(pipeline.catalog, sources)

    recorder = Recorder()
    root = logging.getLogger()
    root.addHandler(recorder)
    root.setLevel(logging.DEBUG)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)

    print(f"Running {len(cases)} cases ...", flush=True)
    results = []
    started = time.monotonic()
    for i, case in enumerate(cases, start=1):
        r = await run_case(case, pipeline, ctx_checks, sources, meter, recorder,
                           no_llm=args.no_llm, ignore_drift=args.ignore_drift)
        results.append(r)
        mark = "skip" if r["skipped"] else ("ok" if r["passed"] else "FAIL")
        print(f"  [{i:3}/{len(cases)}] {mark:4} {case.id}", flush=True)
    elapsed = time.monotonic() - started

    summary = summarise(results, golden)
    print_report(summary, results, verbose=args.verbose)
    drifted = [r["id"] for r in results if any(t.get("drift") for t in r["turns"])]
    if args.ignore_drift:
        print(f"\nData drift (figures or dates no longer match golden.yaml, not counted): "
              f"{len(drifted)} case(s)" + (f": {', '.join(drifted)}" if drifted else ""))
    if args.compare:
        compare(Path(args.compare), results)

    partial = len(cases) != len(golden.cases)
    if not args.no_save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = results_path(args.no_llm, partial)
        payload = {
            "date": date.today().isoformat(),
            "golden": str(Path(args.golden).name),
            "partial": partial,
            "config": {
                "model": settings.llm.model if use_llm else None,
                "no_llm": args.no_llm,
                "min_relevance": settings.retrieval.min_relevance,
                "fuzzy_mean_min": scheme_resolver.FUZZY_MEAN_MIN,
                "fuzzy_token_min": scheme_resolver.FUZZY_TOKEN_MIN,
                "overrides": overrides,
            },
            "elapsed_seconds": round(elapsed, 1),
            "summary": summary,
            "cases": results,
        }
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str),
                        encoding="utf-8")
        shown = path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path
        print(f"\nSaved {shown}")

    gates_met = all(g["met"] is not False for g in summary["gates"].values())
    return 0 if gates_met else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", type=Path, default=GOLDEN_FILE)
    ap.add_argument("--bucket", action="append", choices=BUCKETS)
    ap.add_argument("--case", action="append", help="case id (repeatable; `prefix*` allowed)")
    ap.add_argument("--rerun-failures", metavar="RESULTS_JSON")
    ap.add_argument("--no-llm", action="store_true", help="never call Groq (templates only)")
    ap.add_argument("--max-wait", type=float, default=300.0,
                    help="seconds to wait for Groq rate budget before falling back")
    ap.add_argument("--set", action="append", metavar="KEY=VALUE",
                    help="override a setting, e.g. retrieval.min_relevance=0.55 or "
                         "resolver.fuzzy_mean_min=86")
    ap.add_argument("--compare", metavar="RESULTS_JSON", help="report cases fixed/broken")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--ignore-drift", action="store_true",
                    help="report, don't fail, value/as-of checks (figures change with the data)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every reply")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
