"""Steps 1-3 of the online pipeline in order: PII guard -> scheme resolver -> classifier.

`route_message` ends in one of three ways:
- a finished template `Reply` (PII blocked, refusal, greeting, out of scope, not covered,
  clarifying question), with no retrieval or answer LLM call;
- a `RetrievalRequest` that Phase 5 (retrieval) and Phase 6 (generation) complete; or
- a `ComparisonRequest`: published values for 2-4 schemes side by side (comparison.py).

It also says what the thread should store: the placeholder instead of a blocked message, the new
active scheme, and the candidates offered by a clarifying question.
"""

from dataclasses import dataclass, field

from app.pipeline import comparison, templates
from app.pipeline.catalog import Catalog
from app.pipeline.classifier import Classification, Classifier, QueryClass
from app.pipeline.pii_guard import WITHHELD_PLACEHOLDER, check_pii
from app.pipeline.scheme_resolver import (
    Intent,
    Resolution,
    SchemeResolver,
    has_reference,
    mentions_other_plan,
)
from app.pipeline.text import normalise_name

_KEEP = object()  # sentinel: leave the thread's active scheme unchanged


@dataclass(frozen=True)
class RetrievalRequest:
    query_class: QueryClass  # factual_scheme | factual_metric | factual_general
    scheme_id: str | None  # None for general questions
    other_scheme_ids: tuple[str, ...]  # further schemes named; answered in follow-ups (RES-30)
    intent: Intent
    question: str


@dataclass(frozen=True)
class ComparisonRequest:
    scheme_ids: tuple[str, ...]  # 2..MAX_SCHEMES, in order of mention
    intent: Intent  # what the table shows (DEFAULT_INTENT when nothing was named)
    left_out: tuple[str, ...] = ()  # asked items a table can't show (holdings, ...)
    dropped: tuple[str, ...] = ()  # schemes named past MAX_SCHEMES


@dataclass(frozen=True)
class Route:
    stored_text: str  # what the thread stores for the user turn
    reply: templates.Reply | None = None
    retrieval: RetrievalRequest | None = None
    route: str = ""  # short label for logs: pii_blocked, advisory, retrieval, ...
    pii_kinds: tuple[str, ...] = ()
    classification: Classification | None = None
    resolution: Resolution | None = None
    active_scheme_id: object = _KEEP  # new active scheme id, or _KEEP
    pending_candidates: tuple[str, ...] = field(default_factory=tuple)
    intent: Intent | None = None  # what the message asks for (fields / metrics), if anything
    comparison: ComparisonRequest | None = None

    @property
    def keeps_active_scheme(self) -> bool:
        return self.active_scheme_id is _KEEP


class Router:
    def __init__(self, catalog: Catalog, classifier: Classifier | None = None):
        self.catalog = catalog
        self.resolver = SchemeResolver(catalog)
        self.classifier = classifier or Classifier()

    async def route_message(
        self,
        text: str,
        *,
        active_scheme_id: str | None = None,
        pending_candidates: tuple[str, ...] | list[str] | None = None,
    ) -> Route:
        # Step 1: PII guard, before anything else sees the text (PII-31, PII-32, PII-34).
        pii = check_pii(text)
        if pii.blocked:
            return Route(
                stored_text=WITHHELD_PLACEHOLDER,
                reply=templates.pii_blocked(self.catalog),
                route="pii_blocked",
                pii_kinds=pii.kinds,
            )

        # Step 3 runs before step 2 because the classifier needs to know whether a scheme was
        # named; the resolver never sends text anywhere.
        resolution, intent = self.resolver.analyse(text, active_scheme_id, pending_candidates)
        mentioned = resolution.status in ("resolved", "ambiguous", "not_covered") or (
            resolution.status == "context"
        )
        classification = await self.classifier.classify(
            text, scheme_mentioned=mentioned, intent=intent
        )
        cls = classification.cls
        named = resolution.scheme_id if resolution.status in ("resolved", "context") else None
        new_active = named if resolution.status == "resolved" else _KEEP

        def done(reply, route, **extra) -> Route:
            return Route(
                stored_text=text, reply=reply, route=route, classification=classification,
                resolution=resolution, active_scheme_id=extra.get("active", new_active),
                pending_candidates=extra.get("pending", ()), intent=intent,
            )

        if cls == QueryClass.ADVISORY:
            topic = templates.pick_education_topic(text)
            return done(templates.advisory_refusal(self.catalog, topic), "advisory")
        if cls == QueryClass.PERFORMANCE_JUDGEMENT:
            return done(templates.performance_refusal(self.catalog, named), "performance")
        if cls == QueryClass.GREETING:
            return done(templates.greeting(self.catalog), "greeting")
        if cls == QueryClass.OUT_OF_SCOPE:
            if resolution.status == "not_covered":
                return done(templates.not_covered(self.catalog), "not_covered")
            return done(templates.out_of_scope(self.catalog), "out_of_scope")

        if cls == QueryClass.FACTUAL_GENERAL:
            request = RetrievalRequest(cls, None, (), intent, text)
            return Route(
                stored_text=text, retrieval=request, route="retrieval_general",
                classification=classification, resolution=resolution,
                active_scheme_id=new_active, intent=intent,
            )

        if cls == QueryClass.COMPARISON:
            return self._comparison(text, resolution, intent, active_scheme_id, done,
                                    classification)
        if (resolution.status == "resolved" and resolution.via == "alias"
                and len(resolution.scheme_ids) > 1 and comparison.can_tabulate(intent)
                and not mentions_other_plan(text)):
            # "Expense ratio of X and Y?": one table instead of one scheme per answer.
            return self._comparison(text, resolution, intent, active_scheme_id, done,
                                    classification)

        # factual_scheme / factual_metric need exactly one scheme.
        if resolution.status == "not_covered":
            return done(templates.not_covered(self.catalog), "not_covered")
        if resolution.status == "ambiguous":
            return done(
                templates.clarify(self.catalog, resolution.candidates, resolution.amc),
                "clarify", pending=resolution.candidates,
            )
        if named is None:
            return done(templates.clarify(self.catalog), "clarify")
        if mentions_other_plan(text):
            return done(templates.plan_not_covered(self.catalog, named), "plan_not_covered")
        request = RetrievalRequest(cls, named, resolution.scheme_ids[1:], intent, text)
        return Route(
            stored_text=text, retrieval=request, route="retrieval_scheme",
            classification=classification, resolution=resolution, active_scheme_id=new_active,
            intent=intent,
        )

    def _comparison(self, text: str, resolution: Resolution, intent: Intent,
                    active_scheme_id: str | None, done, classification) -> Route:
        ids = list(resolution.scheme_ids) if resolution.status in ("resolved", "context") else []
        words = normalise_name(text).split()
        if len(ids) == 1 and resolution.via == "alias":
            # An exact name stops the resolver before its typo pass; look for the other scheme
            # ("compare kotak mutli cap and ICICI Pru Large Cap").
            fuzzy = self.resolver._fuzzy(words)
            if fuzzy and ids[0] in fuzzy[0] and len(fuzzy[0]) > 1:
                ids = list(fuzzy[0])
        # "Compare it with ICICI Pru Large Cap": the thread's scheme is the other one.
        if (len(ids) == 1 and active_scheme_id in self.catalog.schemes
                and active_scheme_id not in ids and has_reference(text)):
            ids.insert(0, active_scheme_id)
        if len(ids) < 2:
            if not ids:
                # Nothing to put side by side ("direct vs regular plan", "compare ELSS funds"):
                # the refusal with the AMFI page on comparing funds, as before.
                return done(templates.advisory_refusal(
                    self.catalog, templates.pick_education_topic(text)), "advisory")
            if self.resolver._mentions_uncovered_amc(words):
                return done(templates.not_covered(self.catalog), "not_covered")
            return done(comparison.needs_second_scheme(self.catalog, ids[0]), "clarify")
        if mentions_other_plan(text):
            return done(templates.plan_not_covered(self.catalog, ids[0]), "plan_not_covered")
        shown, left_out = comparison.table_intent(intent)
        kept, dropped = ids[:comparison.MAX_SCHEMES], ids[comparison.MAX_SCHEMES:]
        request = ComparisonRequest(tuple(kept), shown, tuple(left_out), tuple(dropped))
        return Route(
            stored_text=text, route="comparison", classification=classification,
            resolution=resolution, active_scheme_id=kept[0], intent=intent,
            comparison=request,
        )
