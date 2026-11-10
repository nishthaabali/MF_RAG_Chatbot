"""Validate config/sources.yaml, aliases.yaml and education_links.yaml.

Run: python -m ingest.check_sources
Exit code 0 when all checks pass, 1 otherwise.
"""

import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import yaml

from app.config import ROOT_DIR

CONFIG_DIR = ROOT_DIR / "config"

SOURCE_TYPES = {"scheme_page", "amfi", "sebi", "rta_help"}
GENERAL_TYPES = SOURCE_TYPES - {"scheme_page"}
RENDER_MODES = {"static", "browser"}
EXPECTED_SCHEMES = 19
MIN_GENERAL_SOURCES = 10
REQUIRED_TOPICS = {"default", "choosing_a_fund", "risk", "sip_basics", "tax"}
SCHEME_HOST = "groww.in"
GENERAL_HOSTS = (
    "amfiindia.com",
    "mutualfundssahihai.com",
    "sebi.gov.in",
    "camsonline.com",
    "kfintech.com",
    "mfcentral.com",
)
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def normalise(alias: str) -> str:
    """Case-insensitive, punctuation-insensitive form used for duplicate detection."""
    alias = alias.casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", alias).split())


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _check_paginate(sid: str, cfg) -> list[str]:
    """Optional `paginate` block: CSS selectors for page links and items, plus a page cap."""
    if cfg is None:
        return []
    if not isinstance(cfg, dict):
        return [f"{sid}: paginate must be a mapping"]
    errors = [
        f"{sid}: paginate.{key} must be a non-empty CSS selector"
        for key in ("pages", "items")
        if not isinstance(cfg.get(key), str) or not cfg[key].strip()
    ]
    max_pages = cfg.get("max_pages", 10)
    if not isinstance(max_pages, int) or not 2 <= max_pages <= 50:
        errors.append(f"{sid}: paginate.max_pages must be an integer from 2 to 50")
    unknown = set(cfg) - {"pages", "items", "max_pages"}
    if unknown:
        errors.append(f"{sid}: unknown paginate key(s): {', '.join(sorted(unknown))}")
    return errors


def check_sources(sources: list[dict]) -> list[str]:
    errors: list[str] = []
    ids = Counter(s.get("id") for s in sources)
    urls = Counter(s.get("url") for s in sources)
    for sid, n in ids.items():
        if n > 1:
            errors.append(f"duplicate source id: {sid}")
    for url, n in urls.items():
        if n > 1:
            errors.append(f"duplicate url: {url}")

    scheme_pages: dict[str, int] = Counter()
    for s in sources:
        sid = s.get("id", "<missing id>")
        if not ID_RE.match(str(s.get("id", ""))):
            errors.append(f"{sid}: id must be a lowercase slug")
        stype = s.get("type")
        if stype not in SOURCE_TYPES:
            errors.append(f"{sid}: unknown type {stype!r}")
            continue
        if s.get("render") not in RENDER_MODES:
            errors.append(f"{sid}: render must be one of {sorted(RENDER_MODES)}")
        if not s.get("name"):
            errors.append(f"{sid}: name is required")
        errors += _check_paginate(sid, s.get("paginate"))

        parsed = urlparse(str(s.get("url", "")))
        if parsed.scheme != "https" or not parsed.netloc:
            errors.append(f"{sid}: url must be an absolute https URL")
            continue
        host = parsed.netloc.lower()

        if stype == "scheme_page":
            if not _host_matches(host, SCHEME_HOST):
                errors.append(f"{sid}: scheme pages must be on {SCHEME_HOST}")
            for field in ("scheme_id", "amc", "category"):
                if not s.get(field):
                    errors.append(f"{sid}: {field} is required for scheme pages")
            if s.get("scheme_id"):
                scheme_pages[s["scheme_id"]] += 1
        else:
            if not any(_host_matches(host, d) for d in GENERAL_HOSTS):
                errors.append(f"{sid}: host {host} is not an AMFI/SEBI/RTA domain")
            if s.get("scheme_id"):
                errors.append(f"{sid}: only scheme pages may set scheme_id")

    for scheme_id, n in scheme_pages.items():
        if n != 1:
            errors.append(f"scheme {scheme_id} has {n} Groww pages, expected exactly 1")
    if len(scheme_pages) != EXPECTED_SCHEMES:
        errors.append(f"expected {EXPECTED_SCHEMES} schemes, found {len(scheme_pages)}")

    general = [s for s in sources if s.get("type") in GENERAL_TYPES]
    if len(general) < MIN_GENERAL_SOURCES:
        errors.append(
            f"need at least {MIN_GENERAL_SOURCES} AMFI/SEBI/RTA sources, found {len(general)}"
        )
    present = {s.get("type") for s in general}
    for needed in sorted(GENERAL_TYPES - present):
        errors.append(f"no sources of type {needed}")
    return errors


def check_aliases(aliases: dict, sources: list[dict]) -> list[str]:
    errors: list[str] = []
    pages = [s for s in sources if s.get("type") == "scheme_page"]
    scheme_ids = {s["scheme_id"] for s in pages if s.get("scheme_id")}
    amc_ids = {s["amc"] for s in pages if s.get("amc")}

    amcs = aliases.get("amcs") or {}
    schemes = aliases.get("schemes") or {}

    for amc in sorted(amc_ids - set(amcs)):
        errors.append(f"amc {amc} is used in sources.yaml but has no entry in aliases.yaml")
    for amc in sorted(set(amcs) - amc_ids):
        errors.append(f"aliases.yaml amc {amc} has no scheme in sources.yaml")
    for scheme_id in sorted(set(schemes) - scheme_ids):
        errors.append(f"aliases.yaml scheme {scheme_id} does not exist in sources.yaml")
    for scheme_id in sorted(scheme_ids - set(schemes)):
        errors.append(f"scheme {scheme_id} has no aliases")

    owner: dict[str, str] = {}
    for amc, entry in amcs.items():
        if not (entry or {}).get("aliases"):
            errors.append(f"amc {amc} has no aliases")
        for alias in (entry or {}).get("aliases", []):
            key = normalise(alias)
            if not key:
                errors.append(f"amc {amc}: empty alias")
            elif key in owner:
                errors.append(f"alias {alias!r} used by both {owner[key]} and amc:{amc}")
            else:
                owner[key] = f"amc:{amc}"
    for scheme_id, names in schemes.items():
        if not names:
            errors.append(f"scheme {scheme_id} has an empty alias list")
        for alias in names or []:
            key = normalise(alias)
            if not key:
                errors.append(f"scheme {scheme_id}: empty alias")
            elif key in owner:
                errors.append(f"alias {alias!r} used by both {owner[key]} and {scheme_id}")
            else:
                owner[key] = scheme_id
    return errors


def check_education_links(education: dict, sources: list[dict]) -> list[str]:
    errors: list[str] = []
    topics = (education or {}).get("topics") or {}
    by_id = {s["id"]: s for s in sources if "id" in s}
    for topic in sorted(REQUIRED_TOPICS - set(topics)):
        errors.append(f"education_links.yaml is missing required topic {topic!r}")
    for topic, source_id in topics.items():
        source = by_id.get(source_id)
        if source is None:
            errors.append(f"topic {topic}: source id {source_id!r} is not in sources.yaml")
        elif source.get("type") == "scheme_page":
            errors.append(f"topic {topic}: must link to an AMFI/SEBI/RTA page, not a scheme page")
    return errors


def _load(path: Path):
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def run_checks(config_dir: Path = CONFIG_DIR) -> list[str]:
    sources = _load(config_dir / "sources.yaml").get("sources") or []
    aliases = _load(config_dir / "aliases.yaml")
    education = _load(config_dir / "education_links.yaml")
    return [
        *check_sources(sources),
        *check_aliases(aliases, sources),
        *check_education_links(education, sources),
    ]


def main() -> int:
    errors = run_checks()
    if errors:
        print(f"check_sources FAILED with {len(errors)} problem(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    sources = _load(CONFIG_DIR / "sources.yaml")["sources"]
    schemes = sum(s["type"] == "scheme_page" for s in sources)
    print(f"check_sources OK: {schemes} scheme pages, {len(sources) - schemes} general sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
