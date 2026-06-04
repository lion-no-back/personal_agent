"""CJK tokenizer for the bespoke keyword scorer (keyword.py).

Used exclusively for in-Python relevance scoring after FTS5 candidate
generation (TITLE_TOKEN_WEIGHT × 5, CONTENT_TOKEN_WEIGHT × 1, etc.).
NOT used for FTS indexing — the FTS5 trigram tokenizer handles that
internally with no pre-processing required.

Strategy: CJK bigrams + unigrams, plus ASCII alphanumeric words.
"""

import re

_CJK = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
_ASCII = re.compile(r"[a-zA-Z0-9]+")


def tokenize(text: str) -> list[str]:
    """CJK bigrams + unigrams, plus ASCII words (lowercased by caller as needed)."""
    tokens: list[str] = []
    cjk = _CJK.findall(text)
    for i, ch in enumerate(cjk):
        tokens.append(ch)
        if i + 1 < len(cjk):
            tokens.append(ch + cjk[i + 1])
    tokens.extend(_ASCII.findall(text.lower()))
    return tokens
