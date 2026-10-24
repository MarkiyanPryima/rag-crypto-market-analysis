"""Minimal stand-ins for the OpenAI client, matching the SDK attributes used."""

from types import SimpleNamespace


class FakeEmbeddings:
    def __init__(self, dim: int = 1536) -> None:
        self.dim = dim
        self.calls: list[list[str]] = []

    def create(self, *, model, input, dimensions):
        self.calls.append(list(input))
        data = [
            SimpleNamespace(index=i, embedding=[float(len(t) % 7 + 1)] + [0.0] * (dimensions - 1))
            for i, t in enumerate(input)
        ]
        return SimpleNamespace(data=data, usage=SimpleNamespace(prompt_tokens=10 * len(input)))


class FakeResponses:
    def __init__(self, outputs) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(
            output_parsed=out, usage=SimpleNamespace(input_tokens=100, output_tokens=5)
        )


class FakeOpenAI:
    def __init__(self, parse_outputs=()) -> None:
        self.embeddings = FakeEmbeddings()
        self.responses = FakeResponses(parse_outputs)


# --- LangChain chat model fake -------------------------------------------------

from typing import Any  # noqa: E402

from langchain_core.language_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langchain_core.runnables import RunnableLambda  # noqa: E402


class ScriptedChat(BaseChatModel):
    """Returns pre-scripted AIMessages in order; records every prompt it saw."""

    script: list[Any] = []
    seen: list[list[Any]] = []
    structured: list[Any] = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.seen.append(list(messages))
        msg = self.script.pop(0)
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def bind_tools(self, tools, **kwargs):
        self.seen.append([{"tools": [t["function"]["name"] for t in tools]}])
        return self

    def with_structured_output(self, schema, include_raw=False, **kwargs):
        def _run(_messages):
            parsed = self.structured.pop(0)
            raw = AIMessage(
                content="",
                usage_metadata={"input_tokens": 50, "output_tokens": 5, "total_tokens": 55},
            )
            return {"raw": raw, "parsed": parsed, "parsing_error": None} if include_raw else parsed

        return RunnableLambda(_run)
