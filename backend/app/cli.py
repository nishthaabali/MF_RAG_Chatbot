"""Ask the assistant from the command line.

    python -m app.cli "What is the exit load of Kotak Multi Cap?"
    python -m app.cli --thread demo "What is the exit load of Kotak Multi Cap?"
    python -m app.cli --thread demo "and its benchmark?"      # follow-up in the same thread
    python -m app.cli --thread demo --reset                   # forget that thread
    python -m app.cli --no-llm "expense ratio of PPFAS Flexi Cap"   # templates only

Prints the reply, then the route taken and a trace (tokens, validator results). A `--thread`
keeps its context (active scheme, pending candidates, history) in data/cli_threads/<name>.json.
"""

import argparse
import asyncio
import json
import logging
import sys

from app.config import DATA_DIR, get_settings

THREADS_DIR = DATA_DIR / "cli_threads"


def _load(name: str | None):
    from app.pipeline.orchestrator import ThreadContext, intent_from_dict

    if not name:
        return ThreadContext()
    path = THREADS_DIR / f"{name}.json"
    if not path.exists():
        return ThreadContext()
    data = json.loads(path.read_text(encoding="utf-8"))
    return ThreadContext(data.get("active_scheme_id"), tuple(data.get("pending_candidates", ())),
                         data.get("history", []), intent_from_dict(data.get("last_intent")))


def _save(name: str, ctx) -> None:
    from app.pipeline.orchestrator import intent_to_dict

    THREADS_DIR.mkdir(parents=True, exist_ok=True)
    data = {"active_scheme_id": ctx.active_scheme_id,
            "pending_candidates": list(ctx.pending_candidates), "history": ctx.history,
            "last_intent": intent_to_dict(ctx.last_intent)}
    (THREADS_DIR / f"{name}.json").write_text(json.dumps(data, indent=1, ensure_ascii=False),
                                              encoding="utf-8")


async def _run(args) -> int:
    from app.pipeline.orchestrator import AnswerPipeline

    if args.thread and args.reset:
        (THREADS_DIR / f"{args.thread}.json").unlink(missing_ok=True)
        print(f"thread {args.thread!r} reset")
        if not args.question:
            return 0
    question = " ".join(args.question).strip()
    if not question:
        print('Ask a question, e.g. python -m app.cli "What is the exit load of Kotak Multi Cap?"')
        return 2
    settings = get_settings()
    if not args.no_llm and not settings.groq_api_key.get_secret_value():
        print("(GROQ_API_KEY not set: answering from templates only)", file=sys.stderr)
    pipeline = AnswerPipeline.from_settings(settings, use_llm=not args.no_llm)
    ctx = _load(args.thread)
    result = await pipeline.answer(ctx, question)
    if args.thread:
        _save(args.thread, result.next_context(ctx))

    if args.json:
        print(json.dumps({**result.reply.to_dict(), "route": result.route,
                          "trace": result.trace}, indent=1, ensure_ascii=False, default=str))
        return 0
    print(result.reply.rendered)
    print(f"\n[route: {result.route} | type: {result.reply.type} | "
          f"active scheme: {result.active_scheme_id}]")
    if args.verbose:
        print(json.dumps(result.trace, indent=1, default=str))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question", nargs="*")
    parser.add_argument("--thread", help="keep context across invocations under this name")
    parser.add_argument("--reset", action="store_true", help="forget the --thread first")
    parser.add_argument("--no-llm", action="store_true", help="never call Groq")
    parser.add_argument("--json", action="store_true", help="print the structured reply")
    parser.add_argument("-v", "--verbose", action="store_true", help="print the trace and logs")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "sentence_transformers", "chromadb", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # ₹ on Windows consoles
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
