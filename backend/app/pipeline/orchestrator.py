"""The whole online pipeline for one message (architecture §6).

PII guard -> resolver -> classifier (router) -> retriever -> generator -> validator ->
formatter, short-circuiting to a template wherever a step settles the reply. Every reply,
whichever way it is produced, has at most 3 sentences, exactly one link and the footer.

Failure handling (architecture §6.5-6.6):
- a reply that fails a retryable check gets one retry with a short correction message;
- after that, or when the LLM is unavailable (rate budget, errors, no API key), the answer
  comes from the deterministic fact/metric template when the evidence has one, otherwise the
  no-evidence reply. Raw errors never reach the user.
"""

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any

from app.pipeline import comparison, templates
from app.pipeline.catalog import Catalog, Link
from app.pipeline.classifier import Classifier, GroqClassifier
from app.pipeline.formatter import format_answer
from app.pipeline.generator import Generator, select_evidence
from app.pipeline.llm import LLMClient, LLMUnavailable
from app.pipeline.retriever import Evidence, RetrievalResult, Retriever
from app.pipeline.router import RetrievalRequest, Router
from app.pipeline.scheme_resolver import Intent, MetricIntent, has_reference
from app.pipeline.text import split_sentences
from app.pipeline.validator import correction_message, validate

log = logging.getLogger(__name__)


@dataclass
class ThreadContext:
    """What the pipeline needs from a conversation thread."""

    active_scheme_id: str | None = None
    pending_candidates: tuple[str, ...] = ()
    history: list[dict[str, str]] = field(default_factory=list)  # {"role", "content"}
    last_intent: Intent | None = None  # what the last question asked for, for "and for X?"


# A turn that names a scheme but no field continues the previous question: "And for Bandhan
# Small Cap?", "what about ICICI?", "the ICICI one" (a reply to a clarifying question).
_ELLIPTICAL = re.compile(r"^\W*(and|what\s+about|how\s+about|same\s+for|for|ok\W+and)\b", re.I)
MAX_ELLIPTICAL_WORDS = 4


def intent_to_dict(intent: Intent | None) -> dict | None:
    if intent is None:
        return None
    return {"fields": list(intent.fields),
            "metrics": [[m.metric, m.period, m.basis] for m in intent.metrics],
            "managers": list(intent.managers)}


def intent_from_dict(data: dict | None) -> Intent | None:
    if not data:
        return None
    return Intent(tuple(data.get("fields", ())),
                  tuple(MetricIntent(*m) for m in data.get("metrics", ())),
                  tuple(data.get("managers", ())))


@dataclass(frozen=True)
class PipelineResult:
    reply: templates.Reply
    route: str  # how the reply was produced: answer, template_fallback, advisory, ...
    stored_user_text: str  # the placeholder for a PII-blocked message
    active_scheme_id: str | None
    pending_candidates: tuple[str, ...]
    trace: dict[str, Any]  # for logs and the CLI; never contains message text
    intent: Intent | None = None  # what this turn asked for (inherited ones included)

    def next_context(self, ctx: ThreadContext, max_history: int = 50) -> ThreadContext:
        history = [
            *ctx.history,
            {"role": "user", "content": self.stored_user_text},
            {"role": "assistant", "content": self.reply.answer},
        ][-max_history:]
        asked = self.intent if self.intent is not None and not self.intent.empty else None
        return ThreadContext(self.active_scheme_id, self.pending_candidates, history,
                             asked or ctx.last_intent)


class AnswerPipeline:
    def __init__(self, catalog: Catalog, router: Router, retriever: Retriever | None,
                 generator: Generator | None, *, max_evidence_tokens: int = 1500):
        self.catalog = catalog
        self.router = router
        # None = degraded: no corpus is installed yet (a fresh server volume before the first
        # bundle). Fixed replies (refusals, PII, greetings) still work; anything that needs
        # the sources gets the honest `corpus_unavailable` reply until a corpus is loaded.
        self.retriever = retriever
        self.generator = generator
        self.max_evidence_tokens = max_evidence_tokens

    @classmethod
    def from_settings(cls, settings, *, use_llm: bool = True) -> "AnswerPipeline":
        retriever = Retriever.from_settings(settings)
        return cls._assemble(settings, retriever.catalog, retriever, use_llm=use_llm)

    @classmethod
    def degraded(cls, settings, *, use_llm: bool = True) -> "AnswerPipeline":
        """A pipeline without a corpus: the catalog comes from config/ only (no fetch dates)."""
        return cls._assemble(settings, Catalog.load(), None, use_llm=use_llm)

    @classmethod
    def _assemble(cls, settings, catalog: Catalog, retriever: Retriever | None, *,
                  use_llm: bool) -> "AnswerPipeline":
        llm = None
        api_key = settings.groq_api_key.get_secret_value()
        if use_llm and api_key:
            llm = LLMClient(settings.llm, api_key)
        classifier_llm = GroqClassifier(llm) if llm and settings.llm.classifier_llm else None
        router = Router(catalog, Classifier(classifier_llm))
        generator = None
        if llm is not None:
            generator = Generator(llm, history_messages=settings.llm.history_messages,
                                  max_evidence_tokens=settings.llm.max_evidence_tokens)
        return cls(catalog, router, retriever, generator,
                   max_evidence_tokens=settings.llm.max_evidence_tokens)

    def with_fresh_corpus(self, settings, data_dir=None, engine=None) -> "AnswerPipeline":
        """A new pipeline over the corpus now on disk (Phase 10 hot-reload).

        Catalog (fetch dates for footers), facts, Chroma and BM25 are loaded again and checked;
        the embedding model, the classifier and the Groq client (with its rate limiter) are
        shared with this pipeline. Raises if the corpus on disk is incomplete or mismatched.
        A degraded pipeline (no corpus yet) loads the embedding model here for the first time.
        """
        from app.corpus import active_corpus_dir

        data_dir = data_dir or active_corpus_dir()
        if self.retriever is None:
            retriever = Retriever.from_settings(settings, data_dir=data_dir, verify=True)
            router = Router(retriever.catalog, self.router.classifier)
            return AnswerPipeline(retriever.catalog, router, retriever, self.generator,
                                  max_evidence_tokens=self.max_evidence_tokens)
        if engine is None and data_dir == getattr(self.retriever, "data_dir", None):
            engine = self.retriever.store.engine  # same corpus folder: same database
        retriever = Retriever.from_settings(
            settings, engine=engine, embedder=self.retriever.vector.embedder,
            data_dir=data_dir, verify=True,
        )
        router = Router(retriever.catalog, self.router.classifier)
        return AnswerPipeline(retriever.catalog, router, retriever, self.generator,
                              max_evidence_tokens=self.max_evidence_tokens)

    # ------------------------------------------------------------------ main entry
    async def answer(self, ctx: ThreadContext, text: str) -> PipelineResult:
        route = await self.router.route_message(
            text, active_scheme_id=ctx.active_scheme_id,
            pending_candidates=ctx.pending_candidates,
        )
        active = ctx.active_scheme_id if route.keeps_active_scheme else route.active_scheme_id
        trace: dict[str, Any] = {
            "route": route.route,
            "query_class": route.classification.cls.value if route.classification else None,
            "classified_by": route.classification.decided_by if route.classification else None,
            "pii_kinds": list(route.pii_kinds),
            "scheme_id": route.resolution.scheme_id if route.resolution else None,
        }

        intent = route.intent

        def result(reply, how: str) -> PipelineResult:
            trace["reply_route"] = how
            trace["reply_type"] = reply.type
            # DEBUG: the API logs one whitelisted `turn` record per message (observability.py).
            log.debug("pipeline %s", json.dumps(trace, default=str))
            return PipelineResult(reply, how, route.stored_text, active,
                                  route.pending_candidates, trace, intent)

        if route.reply is not None:
            return result(route.reply, route.route)

        if route.comparison is not None:
            request = route.comparison
            trace["compared"] = list(request.scheme_ids)
            if self.retriever is None:  # degraded: no corpus installed yet
                return result(templates.corpus_unavailable(self.catalog, request.scheme_ids[0]),
                              "corpus_unavailable")
            evidence = await self.retriever.structured_for(request.scheme_ids, request.intent)
            reply = comparison.build(self.catalog, request.scheme_ids, request.intent, evidence,
                                     left_out=request.left_out, dropped=request.dropped)
            return result(reply, "comparison")

        request = route.retrieval
        inherited = self._continues_previous(ctx, request, route, text)
        if inherited:
            request = replace(request, intent=ctx.last_intent)
            intent = ctx.last_intent
            trace["intent_inherited"] = True
        # History only helps a turn that leans on the conversation. A self-contained question
        # gets none: after "ELSS lock-in?", the model read "How do I download my capital gains
        # statement?" as an ELSS question and refused it (Phase 8 smoke test).
        via = route.resolution.via if route.resolution else None
        uses_history = inherited or via in ("context", "pending") or has_reference(text)
        trace["history_sent"] = uses_history
        if self.retriever is None:  # degraded: no corpus installed yet
            return result(templates.corpus_unavailable(self.catalog, request.scheme_id),
                          "corpus_unavailable")
        retrieval = await self.retriever.retrieve(request)
        trace.update(retrieval_status=retrieval.status, retrieval_top_score=retrieval.top_score,
                     scheme_id=retrieval.scheme_id, evidence=len(retrieval.evidence))
        if retrieval.status == "no_evidence":
            trace["no_evidence_reason"] = retrieval.reason
            return result(self._no_evidence(retrieval), "no_evidence")

        history = ctx.history if uses_history else []
        reply, how = await self._generate(ctx, request, retrieval, trace, history)
        reply = self._invite_follow_up(reply, request)
        return result(reply, how)

    # ------------------------------------------------------------------ generation
    async def _generate(self, ctx: ThreadContext, request: RetrievalRequest,
                        retrieval: RetrievalResult, trace: dict,
                        history: list[dict[str, str]]) -> tuple[templates.Reply, str]:
        if self.generator is None:
            trace["fallback_used"] = "no_llm"
            return self._fallback(request, retrieval, llm_down=True)

        evidence = select_evidence(retrieval.evidence, self.max_evidence_tokens)
        resolved = self._resolved_note(request, retrieval)
        failures: list[str] = []
        correction = None
        trace.update(llm_calls=0, prompt_tokens=0, completion_tokens=0)
        for attempt in (1, 2):
            try:
                gen = await self.generator.generate(request.question, evidence, history,
                                                    correction, resolved=resolved)
            except LLMUnavailable as exc:
                trace["fallback_used"] = f"llm_{exc.reason}"
                trace["validator_failures"] = failures
                return self._fallback(request, retrieval, llm_down=True)
            trace["llm_calls"] += 1
            trace["prompt_tokens"] += gen.usage.prompt_tokens
            trace["completion_tokens"] += gen.usage.completion_tokens
            verdict = validate(gen.answer, evidence, request.intent)
            failures += [f"{f}#{attempt}" for f in verdict.failures]
            trace["validator_failures"] = failures
            if verdict.fixes:
                trace["validator_fixes"] = list(verdict.fixes)

            if verdict.action == "accept":
                return format_answer(verdict.answer, verdict.evidence, self.catalog), "answer"
            if verdict.action == "advisory_refusal":
                topic = templates.pick_education_topic(request.question)
                return templates.advisory_refusal(self.catalog, topic), "validator_advisory"
            if verdict.action == "judgement_refusal":
                reply = templates.performance_refusal(self.catalog, retrieval.scheme_id)
                return reply, "validator_judgement"
            if verdict.action == "no_evidence":
                # The model can't see that "HDFC Equity Fund" is HDFC Flexi Cap; a stored fact
                # or figure for the asked field is authoritative, so state it from the template.
                reply = deterministic_reply(self.catalog, request, retrieval.evidence)
                if reply is not None:
                    trace["fallback_used"] = "unanswerable_with_fact"
                    return reply, "template_fallback"
                return self._no_evidence(retrieval), "no_evidence"
            correction = correction_message(verdict.failures)  # retry once

        trace["fallback_used"] = "validator"
        return self._fallback(request, retrieval)

    @staticmethod
    def _continues_previous(ctx: ThreadContext, request: RetrievalRequest, route,
                            text: str) -> bool:
        """'And for Bandhan Small Cap?' after 'Expense ratio of HDFC Flexi Cap?'."""
        if ctx.last_intent is None or ctx.last_intent.empty or not request.intent.empty:
            return False
        if request.scheme_id is None:  # general questions stand on their own
            return False
        picked = route.resolution is not None and route.resolution.via == "pending"
        return (picked or bool(_ELLIPTICAL.search(text))
                or len(text.split()) <= MAX_ELLIPTICAL_WORDS)

    def _resolved_note(self, request: RetrievalRequest,
                       retrieval: RetrievalResult) -> str | None:
        """'<canonical name> (Direct Plan - Growth); asked about: <field>' for the generator."""
        from app.pipeline.templates import FIELD_LABELS

        asked = [FIELD_LABELS.get(f, f.replace("_", " ")) for f in request.intent.fields]
        asked += [m.metric.replace("_", " ") for m in request.intent.metrics]
        parts = []
        scheme_id = retrieval.scheme_id
        if scheme_id and scheme_id in self.catalog.schemes:
            parts.append(f"{self.catalog.schemes[scheme_id].name} (Direct Plan - Growth)")
        if asked:
            parts.append("asked about: " + ", ".join(dict.fromkeys(asked)))
        if request.intent.managers:
            parts.append("fund manager(s) named: " + ", ".join(request.intent.managers))
        return "; ".join(parts) or None

    def _fallback(self, request: RetrievalRequest, retrieval: RetrievalResult,
                  llm_down: bool = False) -> tuple[templates.Reply, str]:
        """Deterministic template if one fits; otherwise say why there is no answer."""
        reply = deterministic_reply(self.catalog, request, retrieval.evidence)
        if reply is not None:
            return reply, "template_fallback"
        if llm_down and retrieval.evidence:  # evidence exists, only the model is missing
            top = retrieval.evidence[0]
            link = Link(top.source_url, top.last_updated or retrieval.fallback.last_updated)
            return templates.unavailable(link), "unavailable"
        return self._no_evidence(retrieval), "no_evidence_fallback"

    def _no_evidence(self, retrieval: RetrievalResult) -> templates.Reply:
        return templates.no_evidence(self.catalog, retrieval.scheme_id, link=retrieval.fallback)

    def _invite_follow_up(self, reply: templates.Reply,
                          request: RetrievalRequest) -> templates.Reply:
        """Several schemes named: one citation means one scheme per answer (RES-30)."""
        others = [s for s in request.other_scheme_ids if s in self.catalog.schemes]
        if reply.type != "answer" or not others:
            return reply
        if len(split_sentences(reply.answer)) >= 3:
            return reply
        name = self.catalog.schemes[others[0]].name
        answer = f"{reply.answer} Ask me about {name} in a separate question."
        return templates.Reply(reply.type, answer, reply.citation_url, reply.last_updated)


def deterministic_reply(catalog: Catalog, request: RetrievalRequest,
                        evidence: tuple[Evidence, ...] | list[Evidence]) -> templates.Reply | None:
    """The fact or metric template for the asked field, straight from the evidence data."""
    scheme_id = request.scheme_id
    if scheme_id is None:
        return None
    asked = list(request.intent.fields)
    for block in evidence:
        if block.kind == "fact":
            values = {f: v for f, v in block.data.get("fields", {}).items() if f in asked}
            if values:
                return templates.facts(catalog, scheme_id, values, block.fetched_at,
                                       block.data.get("managers"), block.data.get("profile", ()),
                                       block.data.get("narrowed", False))
    for block in evidence:
        if block.kind == "metric" and block.data.get("rows"):
            row = block.data["rows"][0]
            return templates.metric(
                catalog, scheme_id, label=row["label"], value=row["value"], unit=row["unit"],
                as_of=row["as_of"], metric_name=block.data.get("metric", ""),
                rank_of=row.get("rank_of"), fetched_at=block.fetched_at,
            )
    for block in evidence:
        if block.kind == "holdings" and block.data.get("holdings"):
            return templates.holdings(catalog, scheme_id, block.data["holdings"],
                                      block.data.get("portfolio_date"), block.fetched_at)
    return None
