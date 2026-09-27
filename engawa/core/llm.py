"""LLM クライアント。

外部APIは使わず、ローカルLLM（Ollama の OpenAI互換API）で完結させる（仕様7章）。
LLM が使えない環境向けに、固定文を返すモッククライアントも用意する。
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncIterator
from typing import Any, Protocol

import httpx

from engawa.config import Settings

ChatMessage = dict[str, str]


# 生成層への指示（能動発話など、ユーザー発言ではない最終ターン）の先頭に付ける印
INSTRUCTION_PREFIX = "【指示】"


class LLMClient(Protocol):
    name: str

    def stream_chat(self, messages: list[ChatMessage]) -> AsyncIterator[str]: ...

    async def complete_json(self, messages: list[ChatMessage]) -> dict[str, Any]: ...

    async def ping(self) -> bool: ...


class OllamaClient:
    """Ollama の /v1/chat/completions を SSE ストリーミングで呼ぶ。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        reasoning_effort: str = "none",
        temperature: float = 0.7,
        timeout: float = 120.0,
    ):
        self.name = f"ollama:{model}"
        self._base_url = base_url
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._temperature = temperature
        self._timeout = timeout

    def _payload(self, messages: list[ChatMessage], **extra: Any) -> dict[str, Any]:
        payload = {"model": self._model, "messages": messages, "temperature": self._temperature, **extra}
        # 思考対応モデル（gemma4 等）は既定で長い推論を先に生成し、応答開始が数十秒遅れる。
        # 会話では "none" で思考を切る。空文字ならモデルの既定に任せる。
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        return payload

    async def complete_json(self, messages: list[ChatMessage]) -> dict[str, Any]:
        payload = self._payload(messages, stream=False, response_format={"type": "json_object"})
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(f"{self._base_url}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        return json.loads(resp.json()["choices"][0]["message"]["content"])

    async def stream_chat(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        payload = self._payload(messages, stream=True)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            async with client.stream(
                "POST", f"{self._base_url}/v1/chat/completions", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:") :].strip()
                    if data == "[DONE]":
                        break
                    choices = json.loads(data).get("choices") or []
                    if choices:
                        delta = choices[0].get("delta", {}).get("content")
                        if delta:
                            yield delta

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(f"{self._base_url}/v1/models")
            return resp.is_success
        except httpx.HTTPError:
            return False


MOCK_REPLIES = [
    "「{text}」って？ ……ふーん、ちゃんと聞いてるよ。続けて。",
    "へえ、「{text}」ね。マスターがそういう話するの、ちょっと珍しいじゃん。",
    "「{text}」かあ。……べ、別に興味津々とかじゃないけど、もうちょっと詳しく聞かせてよ。",
]


MOCK_PROACTIVE_REPLY = "ねえマスター、ちょっといい？ ……別に用ってわけじゃないけど。"


class MockLLMClient:
    """LLM なしで動作確認するためのモック。

    会話：直前のユーザー発言を引用した定型文をストリーム風に返す（能動発話の指示なら固定の話しかけ）。
    判定：常に「話しかける」と答える（意図は空なので判定層側で候補の先頭が使われる）。
    """

    name = "mock"

    def __init__(self, delay: float = 0.03):
        self._delay = delay

    async def stream_chat(self, messages: list[ChatMessage]) -> AsyncIterator[str]:
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if last_user.startswith(INSTRUCTION_PREFIX):
            reply = "(mock) " + MOCK_PROACTIVE_REPLY
        else:
            text = last_user.strip().replace("\n", " ")
            if len(text) > 20:
                text = text[:20] + "…"
            reply = "(mock) " + random.choice(MOCK_REPLIES).format(text=text)
        for i in range(0, len(reply), 3):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield reply[i : i + 3]

    async def complete_json(self, messages: list[ChatMessage]) -> dict[str, Any]:
        return {"action": "speak", "intent": "", "reason": "(mock) 常に話しかける"}

    async def ping(self) -> bool:
        return True


def create_llm(settings: Settings) -> LLMClient:
    """会話用（生成層）のクライアント。"""
    if settings.llm_backend == "ollama":
        return OllamaClient(settings.llm_url, settings.llm_model, settings.llm_reasoning_effort)
    if settings.llm_backend == "mock":
        return MockLLMClient()
    raise ValueError(f"unknown ENGAWA_LLM_BACKEND: {settings.llm_backend}")


def create_judge_llm(settings: Settings) -> LLMClient:
    """判定層用の軽量モデルのクライアント。"""
    if settings.llm_backend == "ollama":
        return OllamaClient(settings.llm_url, settings.judge_model, "none", temperature=0.3)
    if settings.llm_backend == "mock":
        return MockLLMClient()
    raise ValueError(f"unknown ENGAWA_LLM_BACKEND: {settings.llm_backend}")
