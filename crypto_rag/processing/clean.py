"""Text cleanup before chunking: HTML, whitespace and boilerplate sign-offs.

Only derived text is cleaned; articles.body_text keeps the raw value (rule 3).
"""

import re

from bs4 import BeautifulSoup

# Trailing boilerplate that carries no information about the story.
_BOILERPLATE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^\s*(sign up|subscribe) (to|for) .{0,120}$",
        r"^\s*explore more on these topics.*$",
        r"^\s*reuse this content.*$",
        r"^\s*(photograph|illustration|composite)\s*:.{0,120}$",
        r"^\s*this article (was|has been) amended on .*$",
        r"^\s*read more\s*:.*$",
        r"^\s*disclaimer\s*:.*$",
    )
]


def strip_html(text: str) -> str:
    if "<" not in text:
        return text
    return BeautifulSoup(text, "lxml").get_text("\n")


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    lines = []
    for line in strip_html(text).splitlines():
        line = re.sub(r"[ \t\u00a0]+", " ", line).strip()
        if not line or any(p.match(line) for p in _BOILERPLATE):
            continue
        lines.append(line)
    return "\n".join(lines)
