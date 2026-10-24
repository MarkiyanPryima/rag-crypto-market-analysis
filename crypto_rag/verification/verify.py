"""verify(answer, tool_results, as_of): deterministic checks, no language model.

Checks:
  1. numbers    — every number is followed (in the same sentence) by an [m…]/[f…]
                  reference whose value matches at the answer's rounding.
                  Exceptions: dates, clock times, years, durations ("48 h",
                  "7 days"), and figures quoted verbatim from a cited [c_…]
                  chunk (reported news facts, not computed values).
  2. references — every [c_…] was retrieved; every [m…]/[f…] was computed.
  3. source dates — every cited source was published <= as_of.
  4. temporal order — news cited under "Ймовірні причини" was published no
                  later than the start of the move (sharpest hour).
"""

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

REF_RE = re.compile(r"\[((?:m|f)\d+|c_\d+)\]")
NUM_RE = re.compile(
    r"(?<![\w.\[])[-+−]?\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?%?|(?<![\w.\[])[-+−]?\$?\d+(?:\.\d+)?%?"
)
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}:\d{2}\b")
DURATION_RE = re.compile(
    r"^\s*(?:-|\s)?(h\b|hours?\b|hrs?\b|год|годин|d\b|days?\b|дн|днів|дні|день|weeks?\b|тижн|min|хв|months?\b|місяц|years?\b|рок)",
    re.IGNORECASE,
)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-ZА-ЯІЇЄҐ#*\-])|\n")
CAUSES_HEADINGS = ("ймовірні причини", "likely causes", "possible causes")
HEADING_RE = re.compile(r"^\s*(#+\s*|\*\*)(.+?)(\*\*)?\s*$")


@dataclass
class VerificationError:
    check: str
    message: str
    token: str | None = None


@dataclass
class VerificationResult:
    ok: bool
    errors: list[VerificationError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": [asdict(e) for e in self.errors]}

    def feedback(self) -> str:
        return "\n".join(f"- [{e.check}] {e.message}" for e in self.errors)


def _to_float(token: str) -> float:
    return float(token.replace("−", "-").replace("$", "").replace(",", "").rstrip("%"))


def _decimals(token: str) -> int:
    core = token.rstrip("%")
    return len(core.split(".")[1]) if "." in core else 0


def number_matches(token: str, value: float, decimals: int) -> bool:
    """Equal at the answer's precision (<= metric precision); sign may be in words."""
    k = _decimals(token)
    if k > decimals:
        return False
    return abs(round(abs(value), k) - abs(_to_float(token))) < 1e-9


def _sentences(text: str) -> list[str]:
    return [s for s in SENTENCE_END.split(text) if s.strip()]


def _is_year(token: str) -> bool:
    return re.fullmatch(r"(19|20)\d{2}", token) is not None


def _sections(answer: str) -> list[tuple[str, str]]:
    """(heading, body) pairs; text before the first heading has heading ''."""
    out: list[tuple[str, list[str]]] = [("", [])]
    for line in answer.splitlines():
        m = HEADING_RE.match(line)
        if m and len(line) < 80:
            out.append((m.group(2).strip().lower(), []))
        else:
            out[-1][1].append(line)
    return [(h, "\n".join(b)) for h, b in out]


def collect_metrics(tool_results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Flatten tool results into {id: metric-dict} for [m…]/[f…]."""
    found: dict[str, dict[str, Any]] = {}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if "id" in obj and "decimals" in obj and "value" in obj:
                found[obj["id"]] = obj
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(tool_results)
    return found


def collect_sources(tool_results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if str(obj.get("id", "")).startswith("c_") and "published_at" in obj:
                found[obj["id"]] = obj
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(tool_results)
    return found


def verify(answer: str, tool_results: dict[str, Any], as_of: datetime) -> VerificationResult:
    metrics = collect_metrics(tool_results)
    sources = collect_sources(tool_results)
    errors: list[VerificationError] = []

    # 2. references exist
    cited = REF_RE.findall(answer)
    for ref in dict.fromkeys(cited):
        pool = sources if ref.startswith("c_") else metrics
        if ref not in pool:
            errors.append(
                VerificationError("references", f"[{ref}] is not among the tool results", ref)
            )

    # 3. cited sources are not from the future
    for ref in dict.fromkeys(r for r in cited if r.startswith("c_")):
        src = sources.get(ref)
        if src and datetime.fromisoformat(src["published_at"]) > as_of:
            errors.append(
                VerificationError("source_dates", f"[{ref}] was published after as_of", ref)
            )

    # 1. numbers
    for sentence in _sentences(answer):
        cleaned = DATE_RE.sub(" ", sentence)
        cited_chunks = [
            sources[r]["text"]
            for r in REF_RE.findall(sentence)
            if r in sources and "text" in sources[r]
        ]
        for m in NUM_RE.finditer(cleaned):
            token = m.group(0)
            core = token.lstrip("+-−$")
            if _is_year(core) or DURATION_RE.match(cleaned[m.end() :]):
                continue
            if any(core.rstrip("%") in chunk for chunk in cited_chunks):
                continue  # a figure quoted from the cited news text
            following = REF_RE.search(cleaned, m.end())
            ref = (
                following.group(1)
                if following and not following.group(1).startswith("c_")
                else None
            )
            if ref is None:
                errors.append(
                    VerificationError(
                        "numbers", f"number {token} has no [m…]/[f…] reference", token
                    )
                )
                continue
            met = metrics.get(ref)
            if met is None:
                continue  # already reported under references
            if met["value"] is None:
                errors.append(
                    VerificationError(
                        "numbers", f"{token} cites [{ref}], which has no value", token
                    )
                )
            elif not number_matches(token, met["value"], met["decimals"]):
                errors.append(VerificationError(
                    "numbers", f"{token} does not match [{ref}] = {met['display']}", token))  # fmt: skip

    # 4. causes precede the move
    move_starts = [
        datetime.fromisoformat(m["extra"]["ts"])
        for ref, m in metrics.items()
        if m.get("name") == "sharpest_hour_pct" and m.get("extra", {}).get("ts")
        and (f"[{ref}]" in answer or not any(f"[{r}]" in answer for r in metrics if metrics[r].get("name") == "sharpest_hour_pct"))
    ]  # fmt: skip
    if move_starts:
        move_start = max(move_starts)
        for heading, body in _sections(answer):
            if not any(h in heading for h in CAUSES_HEADINGS):
                continue
            for ref in dict.fromkeys(REF_RE.findall(body)):
                src = sources.get(ref)
                if src and datetime.fromisoformat(src["published_at"]) > move_start:
                    errors.append(VerificationError(
                        "temporal_order",
                        f"[{ref}] is named as a cause but was published after the move began "
                        f"({move_start:%Y-%m-%d %H:%M} UTC); describe it as a reaction or drop it",
                        ref,
                    ))  # fmt: skip
    return VerificationResult(ok=not errors, errors=errors)
