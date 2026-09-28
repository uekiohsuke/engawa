"""記憶システム（仕様8章）。

- STM：過去の会話（発言そのもの。要約しない）と自己言及記憶（生活由来）。埋め込みで意味検索してプロンプトに入れ、
  入れるたびに参照回数を数える（日をまたいで累積）。期限は3日。
- 会話の種：STMとは別に持つ話題のストック。能動発話のときにコードが1つ選ぶ。期限は3日。
- 記憶調整：判定層が選んだときに、最近の会話から会話の種と自己言及記憶を作る（仕様6-4 c）。
- 夜間蒸留：眠りに落ちたら、残っているSTM全件（前日以前から持ち越したものも含む）を参照回数つきで渡して
  LTM（会話由来／生活由来の2項目）を書き直させる。その後、その日のSTMは参照回数の上位だけを残し、期限まで持ち越す。
- 能動発話では会話の種を使うが、一定の確率で種を使わず即興の話題にする。
- 会話履歴（直近数件）はこれとは別に、会話処理側がそのままプロンプトに貼る。
"""

from __future__ import annotations

import array
import asyncio
import logging
import math
import random
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from engawa.characters import Character
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import ADJUST_PREFIX, DISTILL_PREFIX, Embedder, LLMClient
from engawa.core.state_service import StateService

log = logging.getLogger(__name__)

STM_TTL = timedelta(days=3)
STM_KEEP_PER_DAY = 10  # 蒸留後、その日のSTMのうち参照回数の上位だけを残す
SEED_TTL = timedelta(days=3)
IMPROMPTU_PROBABILITY = 0.2  # 会話の種があっても即興の話題にする確率
LTM_MAX_CHARS = 2000
RETRIEVE_TOP_K = 3
RETRIEVE_MIN_SIMILARITY = 0.55  # bge-m3 で関連する発言と無関係な発言が分かれる目安
MAX_SEEDS_PER_ADJUST = 3
MAX_SELF_PER_ADJUST = 2
ADJUST_CONTEXT_MESSAGES = 40

# マスターの性別は決めていないので、LLMに推測させない
NO_PRONOUN_RULE = "マスターのことは「マスター」と書き、「彼」「彼女」などの三人称の代名詞は使わないこと。"

KIND_CONVERSATION = "conversation"
KIND_SELF = "self"
# STM を想起するセッション。サブスレッドは LTM のみ参照する（仕様4-1）。記録はサブスレッドの会話も含めて行う
RECALL_SESSION_KINDS = ("dialogue", "main")


def pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def unpack(blob: bytes) -> list[float]:
    return array.array("f", blob).tolist()


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a) * sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _stamp(iso: str) -> str:
    t = datetime.fromisoformat(iso).astimezone()
    return f"{t.month}/{t.day} {t:%H:%M}"


def format_stm_line(item: dict[str, Any], character_name: str) -> str:
    if item["kind"] == KIND_SELF:
        return f"[{_stamp(item['created_at'])} 自分の記憶] {item['content']}"
    speaker = "マスター" if item["speaker"] == "user" else character_name
    return f"[{_stamp(item['created_at'])}] {speaker}：{item['content']}"


class MemoryService:
    def __init__(
        self,
        db: Database,
        hub: EventHub,
        state: StateService,
        llm: LLMClient,
        embedder: Embedder,
        characters: Iterable[Character],
        rand: Callable[[], float] = random.random,
    ):
        self._db = db
        self._hub = hub
        self._state = state
        self._llm = llm
        self._embedder = embedder
        self._characters = {c.id: c for c in characters}
        self._rand = rand
        self._tasks: set[asyncio.Task[Any]] = set()
        self._distilling: set[str] = set()

    def _now(self) -> datetime:
        return self._state.now()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def wait_idle(self) -> None:
        """バックグラウンドの埋め込み計算を待つ（テスト用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _publish(self, character_id: str) -> None:
        await self._hub.publish("memory.updated", character_id=character_id)

    # --- STM ---

    async def record_message(
        self, character_id: str, message: dict[str, Any], embedding: list[float] | None = None
    ) -> None:
        """会話の発言をSTMに登録する。埋め込みが無ければバックグラウンドで計算する。"""
        if message["role"] not in ("user", "character"):
            return
        stm_id = self._db.add_stm(
            character_id,
            KIND_CONVERSATION,
            message["content"],
            message["created_at"],
            speaker=message["role"],
            message_id=message["id"],
            embedding=pack(embedding) if embedding else None,
        )
        if embedding is None:
            self._spawn(self._embed_later(stm_id, message["content"]))

    async def _embed_later(self, stm_id: int, text: str) -> None:
        try:
            [vector] = await self._embedder.embed([text])
            self._db.set_stm_embedding(stm_id, pack(vector))
        except Exception:
            log.exception("embedding failed (stm %s)", stm_id)

    async def embed_query(self, text: str) -> list[float] | None:
        try:
            [vector] = await self._embedder.embed([text])
            return vector
        except Exception:
            log.exception("embedding failed (query)")
            return None

    async def retrieve(
        self,
        character_id: str,
        query_vector: list[float] | None,
        exclude_message_ids: Iterable[int] = (),
    ) -> list[dict[str, Any]]:
        """関連するSTMを探して参照回数を数える。"""
        if query_vector is None:
            return []
        excluded = set(exclude_message_ids)
        since = (self._now() - STM_TTL).isoformat()
        scored = []
        for item in self._db.list_stm(character_id, since=since):
            if item["embedding"] is None or item["message_id"] in excluded:
                continue
            similarity = cosine(query_vector, unpack(item["embedding"]))
            if similarity >= RETRIEVE_MIN_SIMILARITY:
                scored.append((similarity, item))
        scored.sort(key=lambda s: -s[0])
        hits = [dict(item, similarity=sim) for sim, item in scored[:RETRIEVE_TOP_K]]
        self._db.increment_stm_refs([h["id"] for h in hits])
        if hits:
            await self._publish(character_id)
        return hits

    def related_section(self, character: Character, hits: list[dict[str, Any]]) -> str | None:
        if not hits:
            return None
        lines = [
            "## 思い出したこと（今の話に関係しそうな最近の記憶。自然に触れてよいが、無理に持ち出さないこと）",
            *(f"- {format_stm_line(h, character.name)}" for h in sorted(hits, key=lambda h: h["id"])),
        ]
        return "\n".join(lines)

    # --- LTM ---

    def ltm_section(self, character_id: str) -> str | None:
        ltm = self._db.get_ltm(character_id)
        if not ltm or not (ltm["conversation"].strip() or ltm["self"].strip()):
            return None
        return "\n".join(
            [
                "## 長期記憶（これまでに積み重なった記憶）",
                "### マスターとのこと",
                ltm["conversation"].strip() or "（まだ無い）",
                "### 自分のこと",
                ltm["self"].strip() or "（まだ無い）",
            ]
        )

    # --- 会話の種 ---

    def pick_seed(self, character_id: str) -> dict[str, Any] | None:
        """未使用の会話の種を1つ選ぶ（仕様6-4：選ぶのはコード）。None なら即興。

        種があっても IMPROMPTU_PROBABILITY の確率で即興にする。
        """
        seeds = self._db.list_seeds(character_id, since=(self._now() - SEED_TTL).isoformat(), unused_only=True)
        if not seeds or self._rand() < IMPROMPTU_PROBABILITY:
            return None
        return seeds[min(int(self._rand() * len(seeds)), len(seeds) - 1)]

    async def mark_seed_used(self, character_id: str, seed_id: int) -> None:
        self._db.mark_seed_used(seed_id, self._now().isoformat())
        await self._publish(character_id)

    # --- 記憶調整 ---

    def _cursor_key(self, character_id: str) -> str:
        return f"memory_cursor:{character_id}"

    def unprocessed(self, character_id: str) -> list[dict[str, Any]]:
        """前回の記憶調整以降の会話。"""
        cursor = self._db.get_setting(self._cursor_key(character_id), 0)
        return [s for s in self._db.list_stm(character_id, after_id=cursor) if s["kind"] == KIND_CONVERSATION]

    def build_adjust_messages(self, character: Character) -> list[dict[str, str]]:
        now = self._now()
        conversation = self.unprocessed(character.id)[-ADJUST_CONTEXT_MESSAGES:]
        seeds = self._db.list_seeds(character.id, since=(now - SEED_TTL).isoformat(), unused_only=True)
        selves = [s for s in self._db.list_stm(character.id) if s["kind"] == KIND_SELF][-10:]
        ltm = self.ltm_section(character.id)
        prompt = "\n".join(
            [
                f"{ADJUST_PREFIX}あなたはキャラクター「{character.name}」の記憶を整理する係です。",
                f"{character.name}はマスター（ユーザー）のデスクトップに住む隣人で、今は会話していない、ひとりの時間です。",
                "最近の会話を振り返って、次の2種類の記憶を作ってください。",
                "1. 会話の種：あとで自分からマスターに振ってみたい話題や、会話の中で気になった疑問。"
                "具体的に、20〜60字で。既存の種と重複しないこと。無ければ空でよい。",
                f"2. 自己言及記憶：今マスターに対してどう思っているか、あるいは自分の生活の中で今日なんとなく思ったこと。"
                f"{character.name}の一人称で、20〜80字で。生活の出来事は具体的に作り込まず、ぼんやりとした輪郭だけにすること。"
                "「今の調子」（だるい・眠い等）は書かないこと。",
                "既存の会話の種のうち、もう話す必要がなくなったもの（会話で既に話題になった等）があれば番号を挙げる。",
                NO_PRONOUN_RULE,
                "",
                f"- 現在時刻：{now:%m/%d %H:%M}",
                "",
                "### 最近の会話",
                *(format_stm_line(s, character.name) for s in conversation),
                "",
                "### 既存の会話の種",
                *([f"{s['id']}. {s['content']}" for s in seeds] or ["（無し）"]),
                "",
                "### 最近の自己言及記憶",
                *([f"- {s['content']}" for s in selves] or ["（無し）"]),
                "",
                *([ltm, ""] if ltm else []),
                f"JSONのみで答えること：{{\"seeds\": [最大{MAX_SEEDS_PER_ADJUST}個の文字列], "
                f"\"drop_seeds\": [不要になった種の番号], \"self_memories\": [最大{MAX_SELF_PER_ADJUST}個の文字列]}}",
            ]
        )
        return [{"role": "user", "content": prompt}]

    async def adjust(self, character_id: str) -> dict[str, Any]:
        """記憶調整：会話の種と自己言及記憶を作る。"""
        character = self._characters[character_id]
        pending = self.unprocessed(character_id)
        data = await self._llm.complete_json(self.build_adjust_messages(character))
        now = self._now().isoformat()

        seeds = [str(s).strip() for s in (data.get("seeds") or []) if str(s).strip()][:MAX_SEEDS_PER_ADJUST]
        for content in seeds:
            self._db.add_seed(character_id, content, now)
        existing = {s["id"] for s in self._db.list_seeds(character_id, unused_only=True)}
        drop = [int(i) for i in (data.get("drop_seeds") or []) if str(i).isdigit() and int(i) in existing]
        self._db.delete_seeds(character_id, drop)

        selves = [str(s).strip() for s in (data.get("self_memories") or []) if str(s).strip()][:MAX_SELF_PER_ADJUST]
        vectors = await self._embed_many(selves)
        for content, vector in zip(selves, vectors):
            self._db.add_stm(character_id, KIND_SELF, content, now, embedding=pack(vector) if vector else None)

        if pending:
            self._db.set_setting(self._cursor_key(character_id), pending[-1]["id"])
        result = {"seeds": seeds, "dropped_seeds": drop, "self_memories": selves, "processed": len(pending)}
        await self._state.record(
            character_id,
            "memory",
            f"記憶調整：会話{len(pending)}件を整理（種 +{len(seeds)}／-{len(drop)}、自己言及 +{len(selves)}）",
        )
        await self._publish(character_id)
        return result

    async def _embed_many(self, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []
        try:
            return list(await self._embedder.embed(texts))
        except Exception:
            log.exception("embedding failed")
            return [None] * len(texts)

    # --- 夜間蒸留 ---

    def build_distill_messages(self, character: Character, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        ltm = self._db.get_ltm(character.id) or {"conversation": "", "self": ""}
        conversation = [s for s in items if s["kind"] == KIND_CONVERSATION]
        selves = [s for s in items if s["kind"] == KIND_SELF]

        def line(s: dict[str, Any]) -> str:
            earlier = "（前日以前から残っている記憶）" if s["distilled"] else ""
            return f"［参照{s['ref_count']}回］{format_stm_line(s, character.name)}{earlier}"

        prompt = "\n".join(
            [
                f"{DISTILL_PREFIX}あなたはキャラクター「{character.name}」の長期記憶を更新する係です。"
                f"{character.name}は眠りについたところで、短期記憶を長期記憶に整理します。",
                "",
                "## ルール",
                "- 長期記憶は「マスターとのこと（会話由来）」と「自分のこと（生活由来の自己言及記憶から）」の2項目。出所を混ぜないこと。",
                f"- 2項目の合計を{LTM_MAX_CHARS}字以内に収めること。収まらなければ重要度の低いものから削る。",
                "- 各短期記憶には［参照n回］が付いている（日をまたいだ通算）。参照回数が多いものほど重要なので、"
                "詳しく（多くの分量を割いて）書く。少ないものは簡潔にまとめるか、削る。",
                "- 「前日以前から残っている記憶」は、何日にもわたって思い出されてきた大事な記憶である。",
                "- 既存の長期記憶のうち今も大事なことは残す。何を残し、何を削るかはあなたが判断する。",
                f"- {character.name}の視点で、地の文として書く（箇条書きでも文章でもよい）。マスターの好み・予定・出来事・関係性を具体的に。",
                f"- {NO_PRONOUN_RULE}",
                "",
                "## 既存の長期記憶",
                "### マスターとのこと",
                ltm["conversation"] or "（まだ無い）",
                "### 自分のこと",
                ltm["self"] or "（まだ無い）",
                "",
                "## 短期記憶：会話",
                *([line(s) for s in conversation] or ["（無し）"]),
                "",
                "## 短期記憶：自己言及記憶",
                *([line(s) for s in selves] or ["（無し）"]),
                "",
                'JSONのみで答えること：{"conversation": "マスターとのこと", "self": "自分のこと"}',
            ]
        )
        return [{"role": "user", "content": prompt}]

    async def distill(self, character_id: str) -> dict[str, Any] | None:
        """夜間蒸留：残っているSTM全件から LTM を更新し、その日のSTMと会話の種を整理する。"""
        if character_id in self._distilling:
            return None
        self._distilling.add(character_id)
        try:
            return await self._distill(character_id)
        finally:
            self._distilling.discard(character_id)

    async def _distill(self, character_id: str) -> dict[str, Any]:
        character = self._characters[character_id]
        now = self._now()
        result: dict[str, Any] = {
            "expired_stm": self._db.delete_stm_before(character_id, (now - STM_TTL).isoformat()),
            "expired_seeds": self._db.delete_seeds_before(character_id, (now - SEED_TTL).isoformat()),
            "ltm_chars": None,
        }
        # 入力は記録として残っているSTM全件（前日以前から残っているものも含む）
        items = self._db.list_stm(character_id)
        today = [s for s in items if not s["distilled"]]
        result.update(input=len(items), today=len(today))
        if items:
            data = await self._llm.complete_json(self.build_distill_messages(character, items))
            conversation = str(data.get("conversation") or "").strip()
            self_text = str(data.get("self") or "").strip()
            if conversation or self_text:
                self._db.save_ltm(character_id, conversation, self_text, now.isoformat())
                result["ltm_chars"] = len(conversation) + len(self_text)

        # その日のSTMは参照回数の上位だけを残す（同数なら新しいもの）。残ったものは期限まで持ち越す
        ranked = sorted(today, key=lambda s: (s["ref_count"], s["id"]), reverse=True)
        keep = ranked[:STM_KEEP_PER_DAY]
        self._db.mark_stm_distilled([s["id"] for s in keep])
        self._db.delete_stm([s["id"] for s in ranked[STM_KEEP_PER_DAY:]])
        result["kept"] = len(keep)

        ltm = f"LTM {result['ltm_chars']}字" if result["ltm_chars"] is not None else "LTM 更新なし"
        await self._state.record(
            character_id,
            "memory",
            f"夜間蒸留：STM {len(items)}件（うち今日 {len(today)}件）→ {ltm}、今日の分は{len(keep)}件残す"
            f"（期限切れ STM {result['expired_stm']}件・種 {result['expired_seeds']}件を削除）",
        )
        await self._publish(character_id)
        return result

    # --- 表示用 ---

    def snapshot(self, character_id: str) -> dict[str, Any]:
        now = self._now()
        stm = self._db.list_stm(character_id)
        ltm = self._db.get_ltm(character_id)
        return {
            "character_id": character_id,
            "ltm": ltm,
            "ltm_max_chars": LTM_MAX_CHARS,
            "seeds": self._db.list_seeds(character_id, since=(now - SEED_TTL).isoformat()),
            "stm": [
                {k: v for k, v in s.items() if k != "embedding"} | {"embedded": s["embedding"] is not None}
                for s in reversed(stm)
            ],
            "unprocessed": len(self.unprocessed(character_id)),
            "rules": {
                "stm_ttl_days": STM_TTL.days,
                "stm_keep_per_day": STM_KEEP_PER_DAY,
                "seed_ttl_days": SEED_TTL.days,
                "impromptu_probability": IMPROMPTU_PROBABILITY,
                "retrieve_top_k": RETRIEVE_TOP_K,
                "retrieve_min_similarity": RETRIEVE_MIN_SIMILARITY,
            },
        }
