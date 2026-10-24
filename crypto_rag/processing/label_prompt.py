"""Prompt and output schema for article labelling (kept apart from the code)."""

from typing import Literal

from pydantic import BaseModel, Field

Topic = Literal[
    "regulation", "hacks_and_failures", "institutional", "technology", "macro", "market", "other"
]
Tone = Literal["negative", "neutral", "positive"]


class ArticleLabelOut(BaseModel):
    topic: Topic = Field(description="Single dominant topic of the article")
    tone: Tone = Field(description="Tone of the article towards the crypto market")


LABEL_INSTRUCTIONS = """\
You label news articles about cryptocurrency markets. Return exactly one topic and one tone.

Topics:
- regulation: laws, regulators, enforcement, court cases, policy, taxation, government action
- hacks_and_failures: hacks, exploits, scams, fraud, bankruptcies, outages, collapses
- institutional: ETFs, funds, banks, corporate treasuries, institutional adoption or flows
- technology: protocol upgrades, scaling, wallets, infrastructure, new products, research
- macro: interest rates, inflation, central banks, equities, geopolitics, the dollar
- market: price moves, trading, volatility, derivatives, liquidations, market analysis
- other: anything that fits none of the above

Tone is the article's stance towards the crypto market (not the writing style):
- negative: bad news for crypto prices or adoption
- positive: good news for crypto prices or adoption
- neutral: mixed, factual or unclear

Choose the dominant topic if several apply. Base the answer only on the given text.
"""
