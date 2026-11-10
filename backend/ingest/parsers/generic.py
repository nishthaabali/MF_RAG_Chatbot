"""Readable text and headings from AMFI, SEBI and RTA pages (no structured fields)."""

import io

import pdfplumber
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from ingest import normalise as n

BOILERPLATE_TAGS = (
    "script",
    "style",
    "noscript",
    "svg",
    "iframe",
    "nav",
    "footer",
    "header",
    "form",
    "button",
)
HEADINGS = {"h1", "h2", "h3", "h4"}
BLOCK_TAGS = (
    "h1",
    "h2",
    "h3",
    "h4",
    "p",
    "li",
    "dt",
    "dd",
    "td",
    "th",
    "div",
    "section",
    "article",
    "blockquote",
    "pre",
)
BLOCKS = ",".join(BLOCK_TAGS)
MIN_BLOCK_CHARS = 3


def _is_leaf_block(node) -> bool:
    """True if no other block element is nested inside (lexbor's css() includes the node itself)."""
    nested = node.css(BLOCKS)
    return len(nested) <= (1 if node.tag in BLOCK_TAGS else 0)


def parse_generic(html: str) -> dict:
    """Return {title, description, sections: [{heading, text}]} in document order.

    Text is taken from leaf block elements (blocks with no nested block), so a paragraph is read
    once with its inline formatting intact, and wrapper <div>s don't repeat their children.
    """
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = n.clean_text(title_node.text()) if title_node else None
    meta = tree.css_first('meta[name="description"]') or tree.css_first(
        'meta[property="og:description"]'
    )
    description = n.clean_text(meta.attributes.get("content")) if meta else None
    root = tree.body or tree.root
    for tag in BOILERPLATE_TAGS:
        for node in root.css(tag):
            node.decompose()

    sections: list[dict] = []
    current = {"heading": None, "text": []}
    seen: set[str] = set()
    for node in root.css(BLOCKS):
        if node.tag not in HEADINGS and not _is_leaf_block(node):
            continue
        text = n.clean_text(node.text(separator=" "))
        if not text or len(text) < MIN_BLOCK_CHARS:
            continue
        if node.tag in HEADINGS:
            if current["text"]:
                sections.append(current)
            current = {"heading": text, "text": []}
            continue
        if text in seen:
            continue
        seen.add(text)
        current["text"].append(text)
    if current["text"]:
        sections.append(current)

    return {
        "title": title,
        "description": description,
        "sections": [{"heading": s["heading"], "text": "\n".join(s["text"])} for s in sections],
    }


def parse_pdf(data: bytes) -> dict:
    sections = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            text = n.clean_text(page.extract_text() or "")
            if text:
                sections.append({"heading": f"Page {i}", "text": text})
    return {"title": None, "description": None, "sections": sections}
