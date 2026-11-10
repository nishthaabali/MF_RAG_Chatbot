"""Text helpers shared by the guards, the resolver and the reply templates."""

import re
import unicodedata

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_CAP_COMPOUND = re.compile(r"\b(flexi|multi|small|mid|large)cap\b")


def normalise_unicode(text: str) -> str:
    """Fold look-alike characters so regexes see plain ASCII digits and spaces.

    NFKC turns full-width digits and most Unicode spaces into ASCII; zero-width characters are
    dropped (so "9876​543210" reads as one number) and any other decimal digit (Devanagari,
    Bengali, ...) becomes its ASCII digit.
    """
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    text = "".join(
        str(unicodedata.decimal(c)) if c.isdecimal() and not c.isascii() else c for c in text
    )
    return re.sub(r"[^\S\n]", " ", text)


def normalise_name(text: str) -> str:
    """Lower-case word form used for alias and keyword matching.

    "HDFC's Flexi-Cap & Pharma" -> "hdfc flexi cap and pharma"; "multicap" -> "multi cap".
    """
    text = normalise_unicode(text).lower().replace("&", " and ")
    text = re.sub(r"['’`]s\b", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = _CAP_COMPOUND.sub(r"\1 cap", text)
    return " ".join(text.split())


def tokens(text: str) -> list[str]:
    return normalise_name(text).split()


# Abbreviations followed by a full stop. The first never end a sentence ("Rs. 500",
# "e.g. HDFC", "Mr. Shah"); the second end one only when the next word is capitalised
# ("1 yr. There is ...", but "approx. 5 days" and "Ltd. and Fidelity" continue).
_NEVER_END = {
    "rs", "e.g", "i.e", "vs", "approx", "mr", "mrs", "ms", "dr", "st", "no", "nos", "p.a",
    "pvt",  # always followed by "Ltd": "Invesco Asset Management (India) Pvt. Ltd."
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}
_MAYBE_END = {"etc", "ltd", "inc", "co", "yr", "yrs", "mo", "min", "max", "avg", "cr",
              "lk", "fig"}
_SENTENCE_END = re.compile(r"[.!?]+[\"')\]]*(?=\s+|$)")


def split_sentences(text: str) -> list[str]:
    """Split on . ! ? followed by whitespace or the end, ignoring decimals and abbreviations."""
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        before = text[start : match.start()]
        last_word = re.search(r"([A-Za-z][A-Za-z.]*)$", before)
        if match.group().startswith(".") and last_word:
            word = last_word.group(1).lower().rstrip(".")
            following = text[end:].lstrip()
            next_is_capital = bool(following) and following[0].isupper()
            if word in _NEVER_END or (word in _MAYBE_END and not next_is_capital):
                continue
            if len(last_word.group(1)) == 1:  # initials such as "D. Silva"
                continue
        piece = text[start:end].strip()
        if piece:
            sentences.append(piece)
        start = end
    tail = text[start:].strip()
    if tail:
        sentences.append(tail)
    return sentences
