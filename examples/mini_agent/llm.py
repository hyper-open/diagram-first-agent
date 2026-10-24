"""LLM adapters: a scripted mock and an OpenAI-compatible HTTP client."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .types import Decision, ToolCall


class MockExhausted(RuntimeError):
    """Raised when a scripted mock runs out of responses."""


def _estimate_tokens(text: str) -> int:
    # Rough heuristic only; enough to exercise budget accounting in the demo.
    return max(1, len(text) // 4)


def tool(name: str, **arguments: Any) -> Dict[str, Any]:
    """Convenience helper for writing mock scripts."""
    return {"tool": name, "arguments": arguments}


def final(content: str) -> Dict[str, Any]:
    return {"final": content}


ScriptItem = Union[Decision, Dict[str, Any], Callable[..., Decision]]


class MockLLM:
    """A deterministic LLM used by the tutorial and tests.

    Each call returns the next scripted decision.  A script item can be:
      * ``{"tool": "calculator", "arguments": {...}}``
      * ``{"final": "answer"}``
      * a ``Decision`` instance
      * a callable ``(messages, tools, index) -> Decision``
    """

    def __init__(self, script: Sequence[ScriptItem]):
        self.script = list(script)
        self.calls = 0
        self.seen_messages: List[List[Dict[str, Any]]] = []

    def decide(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Decision:
        index = self.calls
        self.calls += 1
        self.seen_messages.append(messages)
        if index >= len(self.script):
            raise MockExhausted(f"mock script exhausted after {len(self.script)} steps")

        item = self.script[index]
        if callable(item):
            return item(messages, tools, index)
        if isinstance(item, Decision):
            return item
        if "tool" in item:
            return Decision(
                tool_calls=[
                    ToolCall(
                        name=item["tool"],
                        arguments=item.get("arguments", {}),
                        id=f"mock-{index}",
                    )
                ],
                usage={
                    "input_tokens": _estimate_tokens(json.dumps(messages, ensure_ascii=False)),
                    "output_tokens": 20,
                },
            )
        if "final" in item:
            content = item["final"]
            return Decision(
                content=content,
                final=True,
                usage={
                    "input_tokens": _estimate_tokens(json.dumps(messages, ensure_ascii=False)),
                    "output_tokens": _estimate_tokens(content),
                },
            )
        raise ValueError(f"unsupported mock item: {item!r}")


class OpenAICompatLLM:
    """Minimal client for OpenAI-compatible ``/chat/completions`` endpoints.

    Configure with environment variables:
      OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL
    The client uses only urllib so the tutorial has no runtime dependencies.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout_s: float = 60.0,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (
            base_url
            or os.environ.get("OPENAI_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL", "")
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.max_tokens = max_tokens
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for the live client")
        if not self.model:
            raise ValueError("OPENAI_MODEL is required for the live client")

    def decide(
        self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]
    ) -> Decision:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"LLM HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM connection error: {exc.reason}") from exc

        data = json.loads(body)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""

        calls: List[ToolCall] = []
        for raw_call in message.get("tool_calls") or []:
            function = raw_call.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            if isinstance(raw_args, str):
                try:
                    arguments = json.loads(raw_args)
                except json.JSONDecodeError:
                    arguments = {"__raw_arguments__": raw_args}
            else:
                arguments = raw_args
            if not isinstance(arguments, dict):
                arguments = {"__raw_arguments__": arguments}
            calls.append(
                ToolCall(
                    name=function.get("name") or "",
                    arguments=arguments,
                    id=raw_call.get("id") or f"call-{len(calls)}",
                )
            )

        usage_raw = data.get("usage") or {}
        usage = {
            "input_tokens": int(usage_raw.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage_raw.get("completion_tokens", 0) or 0),
            "total_tokens": int(usage_raw.get("total_tokens", 0) or 0),
        }
        return Decision(
            content=content,
            tool_calls=calls,
            final=not calls,
            usage=usage,
        )
