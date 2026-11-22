"""Dutch and English.

Dutch is the source language: every text the user sees is written in Dutch and goes through
``_()`` (templates and Python) or ``BM.t()`` (JavaScript). ``locale_en.EN`` holds the English
translations; a test makes sure every text has one.

The language of a request comes from the ``bm_lang`` cookie, else from the browser
(Accept-Language: Dutch -> Dutch, anything else -> English).
"""

from __future__ import annotations

import contextvars

from .locale_en import EN, JS

LANGS = ("nl", "en")
COOKIE = "bm_lang"
_lang: contextvars.ContextVar[str] = contextvars.ContextVar("bm_lang", default="nl")


def get_lang() -> str:
    return _lang.get()


def set_lang(lang: str):
    return _lang.set(lang if lang in LANGS else "nl")


def pick_lang(cookie: str | None, accept: str | None) -> str:
    if cookie in LANGS:
        return cookie
    for part in (accept or "").split(","):
        code = part.split(";")[0].strip().lower()
        if not code or code == "*":
            continue
        return "nl" if code.startswith("nl") else "en"
    return "nl"


def _(text: str, **kw) -> str:
    """Translate a Dutch text to the language of this request; {name} placeholders are filled in."""
    if _lang.get() == "en":
        text = EN.get(text, text)
    return text.format(**kw) if kw else text


def _n(singular: str, plural: str, n, **kw) -> str:
    """Singular/plural: _n('{n} fles', '{n} flessen', n)."""
    return _(singular if n == 1 else plural, n=n, **kw)


def js_strings() -> dict:
    """The translations the JavaScript needs (empty in Dutch: the source texts are Dutch)."""
    return {k: EN.get(k, k) for k in JS} if _lang.get() == "en" else {}


def number(v: float, decimals: int = 1) -> str:
    s = f"{v:.{decimals}f}"
    return s.replace(".", ",") if _lang.get() == "nl" else s


MONTHS = {"nl": ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"],
          "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]}


def month(m: int) -> str:
    return MONTHS[_lang.get()][m - 1]
