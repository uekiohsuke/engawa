"""記憶システム v2：プロフィール型（仕様8章）。

- 短期記憶：会話ログ（messages）のうち、まだエピソード化していない発言。プロンプトには会話処理側が
  最新から一定数を会話履歴として貼る。エピソード化が済んだら「記憶済み」にする（画面の履歴としては残す）。
- エピソード記憶：短期記憶から切り出した出来事単位の記憶。毎ターン、コードが意味検索してヒットしたものだけを
  プロンプトに入れる。削除猶予（過ぎたら削除対象）と時限（過ぎたら即削除）を持ち、思い出されると両方延びる。
  件数が上限を超えたら、削除対象の中から重み付きのランダムで少しずつ消す。
- プロフィール記憶：自分自身とユーザーの「印象層」。常にプロンプトに載せる。日中は変えず、睡眠中に
  その日の印象メモからまとめて書き直す。自分のプロフィールの土台はペルソナ（persona/*.md）で、印象層はその上の積み重ね。
- 会話の種：思い出し（エピソードから。記憶調整で作る）と話題作成（印象層から。睡眠中に作る）。即興は保存しない。
- 記憶調整：判定層が選んだときに、短期記憶をエピソード化する。続いている最新の会話はエピソード化しない。
- 画面の様子：即興の話題の材料として、話しかけるときに直近のものを渡す（仕様3章・8-4）。
"""

from __future__ import annotations

import array
import asyncio
import logging
import math
import random
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from engawa.characters import Character
from engawa.core.db import Database
from engawa.core.events import EventHub
from engawa.core.llm import ADJUST_PREFIX, PROFILE_PREFIX, Embedder, LLMClient
from engawa.core.state_service import StateService
from engawa.core.timewords import describe_elapsed

log = logging.getLogger(__name__)

SUBJECT_USER = "user"
SUBJECT_SELF = "self"

# --- エピソード記憶の寿命（仮決め） ---
GRACE_MIN = timedelta(days=3)  # 削除猶予の初期値の最低値（作ったばかりの記憶が削除対象にならないように）
GRACE_PER_100_CHARS = timedelta(days=3)  # 詳しく書かれた（＝重要と判断された）ものほど猶予が長い
GRACE_MAX = timedelta(days=21)
GRACE_EXTEND = timedelta(days=3)  # 思い出されたときの延長
LIFETIME = timedelta(days=30)  # 時限の初期値
LIFETIME_EXTEND = timedelta(days=7)
EPISODE_LIMIT = 500  # 件数の上限（一時的な超過を許すので高めに）
PRUNE_MAX_PER_RUN = 10  # 上限を超えたとき、1回の判定で消す最大件数

# --- 検索（毎ターン、コードが自動で行う。仕様8-5） ---
RETRIEVE_TOP_K = 3
RETRIEVE_MIN_SIMILARITY = {"dialogue": 0.55, "main": 0.55, "sub": 0.65}  # サブは特定の話題用なので厳しめ

# --- 記憶調整（エピソード化） ---
ADJUST_MAX_MESSAGES = 80  # 1回に渡す短期記憶の最大件数。残りは次回
ONGOING_MAX_AGE = timedelta(hours=3)  # これより前に途切れた会話は「続いている」とはみなさない
SESSION_GAP = timedelta(minutes=30)  # 同じ会話の中でこれ以上間が空いたら、区切りとして示す
CONSOLIDATE_MAX_ROUNDS = 5  # 睡眠中に残りをエピソード化するときの最大回数
MAX_MEMO_CHARS = 120
RECALL_CANDIDATES = 5  # 思い出しの種の材料として渡す、少し前のエピソードの数
RECALL_MIN_AGE = timedelta(days=1)
MAX_RECALL_SEEDS = 2

# --- プロフィール・会話の種 ---
PROFILE_MAX_CHARS = 1000  # 印象層の長さ（対象ごと）
MAX_TOPIC_SEEDS = 2
SEED_TTL = timedelta(days=3)
IMPROMPTU_PROBABILITY = 0.2  # 会話の種があっても即興の話題にする確率

# --- 画面の様子（即興の材料） ---
ACTIVITY_TTL = timedelta(days=1)
ACTIVITY_MERGE_GAP = timedelta(minutes=2)  # 観測の間隔がこれ以内なら同じ活動として続ける
ACTIVITY_MIN_MINUTES = 1.0  # ちらっと切り替えただけのものは材料にしない
ACTIVITY_TITLE_MAX = 120
IMPROMPTU_ACTIVITY_WINDOW = timedelta(minutes=30)
IMPROMPTU_ACTIVITY_MAX = 5

def no_pronoun_rule(user_name: str) -> str:
    # ユーザーの性別は決めていないので、LLMに推測させない
    return f"{user_name}のことは「{user_name}」と書き、「彼」「彼女」などの三人称の代名詞は使わないこと。"

SESSION_LABELS = {"dialogue": "対話", "main": "#main", "sub": "サブチャンネル"}


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


def initial_grace(content: str) -> timedelta:
    """削除猶予の初期値。文字数が多い（＝重要と判断されて詳しく書かれた）ほど長い。"""
    return min(GRACE_MAX, GRACE_MIN + GRACE_PER_100_CHARS * (len(content) / 100))


def prune_weight(episode: dict[str, Any], now: datetime) -> float:
    """件数の上限で消すときの重み。大事なもの（よく思い出される）ほど小さく、時限が近いもの・短いものほど大きい。"""
    days_left = max(0.0, (datetime.fromisoformat(episode["expires_at"]) - now).total_seconds() / 86400)
    return (1 / (1 + episode["access_count"])) * (1 / (1 + days_left / 7)) * (100 / max(len(episode["content"]), 20))


def activity_minutes(activity: dict[str, Any]) -> float:
    started = datetime.fromisoformat(activity["started_at"])
    ended = datetime.fromisoformat(activity["ended_at"])
    return (ended - started).total_seconds() / 60


def format_activity_line(activity: dict[str, Any]) -> str:
    started = datetime.fromisoformat(activity["started_at"]).astimezone()
    ended = datetime.fromisoformat(activity["ended_at"]).astimezone()
    return f"[{started:%H:%M}〜{ended:%H:%M}] {activity['app']} — {activity['title']}"


def format_episode_line(episode: dict[str, Any]) -> str:
    return f"[{_stamp(episode['happened_at'])}] {episode['content']}"


def _texts(values: Any, limit: int, max_chars: int | None = None) -> list[str]:
    texts = [str(v).strip() for v in (values or []) if v and str(v).strip()][:limit]
    return [t[:max_chars] for t in texts] if max_chars else texts


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
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._consolidating: set[str] = set()

    def _now(self) -> datetime:
        return self._state.now()

    async def wait_idle(self) -> None:
        """バックグラウンドの処理を待つ（テスト用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _publish(self, character_id: str) -> None:
        await self._hub.publish("memory.updated", character_id=character_id)

    async def embed_query(self, text: str) -> list[float] | None:
        try:
            [vector] = await self._embedder.embed([text])
            return vector
        except Exception:
            log.exception("embedding failed (query)")
            return None

    async def _embed_many(self, texts: list[str]) -> list[list[float] | None]:
        if not texts:
            return []
        try:
            return list(await self._embedder.embed(texts))
        except Exception:
            log.exception("embedding failed")
            return [None] * len(texts)

    # --- 短期記憶 ---

    def unprocessed(self, character_id: str) -> list[dict[str, Any]]:
        """まだエピソード化していない発言。"""
        return self._db.unmemorized_messages(character_id)

    # --- エピソード記憶の検索 ---

    async def retrieve(
        self,
        character_id: str,
        query_vector: list[float] | None,
        session_kind: str,
        window: tuple[int, int] | None = None,
    ) -> list[dict[str, Any]]:
        """関連するエピソードを探し、思い出したものの削除猶予・時限を延ばす。

        window＝(セッションID, 会話履歴として貼る最古の発言ID)。会話履歴と重なるエピソードは出さない。
        """
        if query_vector is None:
            return []
        now = self._now()
        threshold = RETRIEVE_MIN_SIMILARITY.get(session_kind, max(RETRIEVE_MIN_SIMILARITY.values()))
        scored = []
        for episode in self._db.list_episodes(character_id):
            if episode["embedding"] is None or datetime.fromisoformat(episode["expires_at"]) <= now:
                continue
            if window and episode["session_id"] == window[0] and (episode["last_message_id"] or 0) >= window[1]:
                continue
            similarity = cosine(query_vector, unpack(episode["embedding"]))
            if similarity >= threshold:
                scored.append((similarity, episode))
        scored.sort(key=lambda s: -s[0])
        hits = [dict(e, similarity=sim) for sim, e in scored[:RETRIEVE_TOP_K]]
        for episode in hits:
            grace = max(datetime.fromisoformat(episode["grace_until"]), now) + GRACE_EXTEND
            expires = datetime.fromisoformat(episode["expires_at"]) + LIFETIME_EXTEND
            self._db.touch_episode(episode["id"], now.isoformat(), grace.isoformat(), expires.isoformat())
        if hits:
            await self._publish(character_id)
        return hits

    def related_section(self, character: Character, hits: list[dict[str, Any]]) -> str | None:
        if not hits:
            return None
        lines = [
            "## 思い出したこと（今の話に関係しそうな過去の出来事。自然に触れてよいが、無理に持ち出さないこと）",
            *(f"- {format_episode_line(e)}" for e in sorted(hits, key=lambda e: e["happened_at"])),
        ]
        return "\n".join(lines)

    # --- プロフィール記憶 ---

    def profile_section(self, character_id: str) -> str | None:
        """常にプロンプトに載せる印象層。日中は変わらない（推論キャッシュも効きやすい）。"""
        profiles = self._db.get_profiles(character_id)
        user = profiles.get(SUBJECT_USER, {}).get("impression", "").strip()
        own = profiles.get(SUBJECT_SELF, {}).get("impression", "").strip()
        if not (user or own):
            return None
        return "\n".join(
            [
                "## 記憶（これまでに積み重なった印象）",
                f"### {self._characters[character_id].user_name}について",
                user or "（まだ無い）",
                "### 自分について",
                own or "（まだ無い）",
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

    # --- 画面の様子（即興の話題の材料。仕様3章・8-4） ---

    async def record_activity(self, app: str, title: str) -> None:
        """前面ウィンドウの観測を記録する。同じウィンドウが続いている間は1件にまとめて終了時刻を延ばす。"""
        now = self._now()
        title = title[:ACTIVITY_TITLE_MAX]
        last = self._db.last_activity()
        if (
            last
            and (last["app"], last["title"]) == (app, title)
            and now - datetime.fromisoformat(last["ended_at"]) <= ACTIVITY_MERGE_GAP
        ):
            self._db.extend_activity(last["id"], now.isoformat())
        else:
            self._db.add_activity(app, title, now.isoformat())

    def recent_activity(self) -> list[dict[str, Any]]:
        """即興で話しかけるときの材料：直近に、ある程度続けて前面にあったウィンドウ。"""
        since = (self._now() - IMPROMPTU_ACTIVITY_WINDOW).isoformat()
        activity = [a for a in self._db.list_activity(since=since) if activity_minutes(a) >= ACTIVITY_MIN_MINUTES]
        return activity[-IMPROMPTU_ACTIVITY_MAX:]

    # --- 記憶調整（エピソード化） ---

    def _recall_candidates(self, character_id: str) -> list[dict[str, Any]]:
        """思い出しの種の材料：少し前のエピソードから、思い出されていないものを優先していくつか選ぶ。"""
        before = self._now() - RECALL_MIN_AGE
        older = [e for e in self._db.list_episodes(character_id) if datetime.fromisoformat(e["happened_at"]) <= before]
        older.sort(key=lambda e: (e["last_accessed_at"] or "", self._rand()))
        return sorted(older[:RECALL_CANDIDATES], key=lambda e: e["happened_at"])

    def _split_blocks(
        self, pending: list[dict[str, Any]], chunk: list[dict[str, Any]], finish_all: bool
    ) -> list[dict[str, Any]]:
        """渡す発言を、会話（セッション）ごと・間が空いたところごとのブロックに分ける。

        「続いているかもしれない」のは、各会話の最新の発言まで渡せていて、途切れてから間もない最後のブロックだけ。
        """
        now = self._now()
        by_session: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for message in chunk:
            by_session[message["session_id"]].append(message)
        last_ids = {m["session_id"]: m["id"] for m in pending}
        blocks: list[dict[str, Any]] = []
        for session_id, messages in by_session.items():
            segments: list[list[dict[str, Any]]] = [[messages[0]]]
            for previous, message in zip(messages, messages[1:]):
                gap = datetime.fromisoformat(message["created_at"]) - datetime.fromisoformat(previous["created_at"])
                if gap >= SESSION_GAP:  # 間が空いたところは、たいてい別の出来事
                    segments.append([])
                segments[-1].append(message)
            for i, segment in enumerate(segments):
                recent = now - datetime.fromisoformat(segment[-1]["created_at"]) < ONGOING_MAX_AGE
                may_continue = (
                    not finish_all and i == len(segments) - 1 and segment[-1]["id"] == last_ids[session_id] and recent
                )
                blocks.append(
                    {"no": len(blocks) + 1, "session_id": session_id, "messages": segment, "may_continue": may_continue}
                )
        return blocks

    def build_adjust_messages(
        self, character: Character, blocks: list[dict[str, Any]], recall: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        now = self._now()
        seeds = self._db.list_seeds(character.id, since=(now - SEED_TTL).isoformat(), unused_only=True)
        lines: list[str] = []
        for block in blocks:
            messages = block["messages"]
            title = SESSION_LABELS[messages[0]["session_kind"]]
            if messages[0]["session_kind"] == "sub":
                title += f"「{messages[0]['session_title']}」"
            elapsed = now - datetime.fromisoformat(messages[-1]["created_at"])
            note = "（まだ続いているかもしれない）" if block["may_continue"] else "（終わった会話）"
            lines += [
                f"### 会話{block['no']}：{title}　最後の発言から{describe_elapsed(elapsed)}{note}",
                *(
                    f"#{m['id']} [{_stamp(m['created_at'])}] "
                    f"{character.user_name if m['role'] == 'user' else character.name}：{m['content']}"
                    for m in messages
                ),
                "",
            ]
        prompt = "\n".join(
            [
                f"{ADJUST_PREFIX}あなたはキャラクター「{character.name}」の記憶を整理する係です。",
                f"{character.name}は{character.user_name}（ユーザー）のデスクトップに住む隣人です。まだ記憶にしていない会話を振り返り、"
                "出来事単位の記憶（エピソード）に切り出してください。",
                "",
                "## ルール",
                "- エピソードは1つの会話の中の、ひとまとまりの出来事（話題）。会話番号と発言番号の範囲（from〜to）で示す。",
                "- 「（終わった会話）」は、最初の発言から最後の発言まで漏れなくどれかのエピソードに含めること。",
                f"- 重要そうな出来事（{character.user_name}の予定・好み・悩み・嬉しかったこと、約束など）は具体的に詳しく、"
                "ただの挨拶や他愛ないやり取りは1文で簡潔に書く。詳しさがそのまま記憶の残りやすさになる。",
                f"- エピソードは{character.name}の視点で、後から読んで何があったか分かるように、常体（だ・である調）の地の文で書く。",
                "- 各エピソードについて、印象メモを書いてよい（無ければ null）。"
                f"{MAX_MEMO_CHARS}字以内、{character.name}の一人称・常体で。",
                f"  - about_master：そこから分かった{character.user_name}のこと（好み・予定・事情・人柄など）。"
                "1回の出来事から性格を決めつけず、分かった事実を中心に書く。",
                f"  - about_self：{character.name}自身の気持ちや好み（例：「{character.user_name}の山の話を聞くのは楽しい」）。"
                "自分の応対の出来を評価したり、「成長した」「寄り添えた」のような振り返りは書かない。",
                "- 「（まだ続いているかもしれない）」会話は、話の流れから、まだ続いていると思う部分があれば"
                "その最初の発言番号を ongoing に挙げる。そこから先はエピソードにしない（次の機会に回す）。"
                "話が一区切りついているなら挙げずにエピソードにする。",
                f"- 今回の会話や「少し前の出来事」から、あとで自分から「そういえば……」と話を振ってみたいもの"
                f"（後日どうなったかを聞きたい予定など）があれば、会話の種（recall_seeds）として最大{MAX_RECALL_SEEDS}個、"
                "20〜60字で書く。無理に作らなくてよい。",
                "- 既存の会話の種のうち、もう話す必要がなくなったもの（会話で既に話題になった等）があれば番号を挙げる。",
                f"- {no_pronoun_rule(character.user_name)}",
                "",
                f"- 現在時刻：{now:%m/%d %H:%M}",
                "",
                "## まだ記憶にしていない会話",
                *lines,
                "## 少し前の出来事（思い出しの種の材料）",
                *([f"- {format_episode_line(e)}" for e in recall] or ["（無し）"]),
                "",
                "## 既存の会話の種",
                *([f"{s['id']}. {s['content']}" for s in seeds] or ["（無し）"]),
                "",
                "JSONのみで答えること：{"
                '"episodes": [{"session": 会話番号, "from": 最初の発言番号, "to": 最後の発言番号, "content": "エピソード", '
                '"about_master": "印象" または null, "about_self": "気づき" または null}], '
                '"ongoing": [{"session": 会話番号, "from": まだ続いている部分の最初の発言番号}], '
                '"recall_seeds": ["種"], "drop_seeds": [不要になった種の番号]}',
            ]
        )
        return [{"role": "user", "content": prompt}]

    async def adjust(self, character_id: str, finish_all: bool = False) -> dict[str, Any]:
        """記憶調整：短期記憶をエピソード化する。

        finish_all=True（睡眠時）は、続いているかもしれない会話もすべて終わったものとして扱う。
        """
        async with self._locks[character_id]:
            result = await self._adjust(character_id, finish_all)
        await self._publish(character_id)
        return result

    async def _adjust(self, character_id: str, finish_all: bool) -> dict[str, Any]:
        character = self._characters[character_id]
        now = self._now()
        result: dict[str, Any] = {"episodes": 0, "processed": 0, "ongoing": 0, "memos": 0, "seeds": [], "dropped_seeds": []}
        pending = self.unprocessed(character_id)
        chunk = pending[:ADJUST_MAX_MESSAGES]
        if not chunk:
            return result
        blocks = {b["no"]: b for b in self._split_blocks(pending, chunk, finish_all)}

        recall = self._recall_candidates(character_id)
        data = await self._llm.complete_json(self.build_adjust_messages(character, list(blocks.values()), recall))
        if not isinstance(data, dict):
            data = {}

        # 続いている部分：「続いているかもしれない」ブロックの中の発言番号だけ（会話ごとに、そこから先は保留）
        ongoing: dict[int, int] = {}
        for item in data.get("ongoing") or []:
            try:
                block, first = blocks.get(int(item.get("session"))), int(item.get("from"))
            except (AttributeError, TypeError, ValueError):
                continue
            if block and block["may_continue"] and first in {m["id"] for m in block["messages"]}:
                ongoing[block["session_id"]] = first

        # エピソード：会話番号と発言番号の範囲が正しく、続いている部分にかからないものだけ
        episodes = []
        for item in data.get("episodes") or []:
            if not isinstance(item, dict):
                continue
            try:
                block, first, last = blocks.get(int(item.get("session"))), int(item.get("from")), int(item.get("to"))
            except (TypeError, ValueError):
                continue
            content = str(item.get("content") or "").strip()
            ids = {m["id"] for m in block["messages"]} if block else set()
            if not content or first not in ids or last not in ids or first > last:
                continue
            if last >= ongoing.get(block["session_id"], math.inf):
                continue
            episodes.append((block, first, last, content, item))

        vectors = await self._embed_many([e[3] for e in episodes])
        created_at = now.isoformat()
        for (block, first, last, content, item), vector in zip(episodes, vectors):
            session_id = block["session_id"]
            happened_at = next(m["created_at"] for m in block["messages"] if m["id"] == first)
            episode_id = self._db.add_episode(
                character_id,
                SUBJECT_USER,
                content,
                session_id=session_id,
                first_message_id=first,
                last_message_id=last,
                happened_at=happened_at,
                created_at=created_at,
                grace_until=(now + initial_grace(content)).isoformat(),
                expires_at=(now + LIFETIME).isoformat(),
                embedding=pack(vector) if vector else None,
            )
            result["episodes"] += 1
            for subject, key in ((SUBJECT_USER, "about_master"), (SUBJECT_SELF, "about_self")):
                for memo in _texts([item.get(key)], 1, MAX_MEMO_CHARS):
                    self._db.add_memo(character_id, subject, memo, created_at, episode_id)
                    result["memos"] += 1

        # 記憶済みにするのは、ブロックごとに、エピソードにした範囲の最後まで（続いている部分にはかからない）。
        # エピソードにされなかったブロック・発言（取りこぼし）は短期記憶のまま残し、次の機会にやり直す
        done: list[int] = []
        for block in blocks.values():
            upto = max((last for b, _, last, _, _ in episodes if b is block), default=0)
            done += [m["id"] for m in block["messages"] if m["id"] <= upto]
        self._db.mark_memorized(done)
        result["processed"] = len(done)
        result["ongoing"] = sum(1 for m in chunk if m["session_id"] in ongoing and m["id"] >= ongoing[m["session_id"]])

        seeds = _texts(data.get("recall_seeds"), MAX_RECALL_SEEDS)
        for content in seeds:
            self._db.add_seed(character_id, content, created_at, source="recall")
        existing = {s["id"] for s in self._db.list_seeds(character_id, unused_only=True)}
        drop = [int(i) for i in (data.get("drop_seeds") or []) if str(i).isdigit() and int(i) in existing]
        self._db.delete_seeds(character_id, drop)
        result.update(seeds=seeds, dropped_seeds=drop, pruned=self._prune(character_id))

        await self._state.record(
            character_id,
            "memory",
            f"記憶調整：発言{len(chunk)}件を振り返り、エピソード +{result['episodes']}"
            f"（記憶済み {result['processed']}件・続いている会話 {result['ongoing']}件は保留）、"
            f"印象メモ +{result['memos']}、種 +{len(seeds)}／-{len(drop)}",
        )
        return result

    def _prune(self, character_id: str) -> dict[str, int]:
        """時限切れを消し、件数が上限を超えていれば削除対象（猶予切れ）から重み付きのランダムで少しだけ消す。"""
        now = self._now()
        expired = self._db.delete_expired_episodes(character_id, now.isoformat())
        episodes = self._db.list_episodes(character_id)
        excess = len(episodes) - EPISODE_LIMIT
        removed: list[int] = []
        if excess > 0:
            candidates = [e for e in episodes if datetime.fromisoformat(e["grace_until"]) <= now]
            weights = [prune_weight(e, now) for e in candidates]
            for _ in range(min(excess, PRUNE_MAX_PER_RUN, len(candidates))):
                pick = self._rand() * sum(weights)
                for i, weight in enumerate(weights):
                    pick -= weight
                    if pick <= 0 or i == len(weights) - 1:
                        removed.append(candidates.pop(i)["id"])
                        weights.pop(i)
                        break
            self._db.delete_episodes(removed)
        return {"expired": expired, "over_limit": len(removed)}

    # --- 睡眠中の整理 ---

    def build_profile_messages(
        self, character: Character, subject: str, memos: list[dict[str, Any]], with_topics: bool
    ) -> list[dict[str, str]]:
        current = self._db.get_profiles(character.id).get(subject, {}).get("impression", "")
        target = character.user_name if subject == SUBJECT_USER else f"{character.name}自身"
        seeds = self._db.list_seeds(character.id, since=(self._now() - SEED_TTL).isoformat(), unused_only=True)
        topics = (
            f"- あわせて、更新した印象から「〇〇好きだったよね」のように、あとで自分から{character.user_name}に振ってみたい話題を"
            f"会話の種（topic_seeds）として最大{MAX_TOPIC_SEEDS}個、20〜60字で作る。"
            "印象に書かれた具体的な物事（好きなもの・予定・事情）に触れること。「最近どう？」のような誰にでも言える話題や、"
            "既存の会話の種と同じ話題は作らない。"
            if with_topics
            else '- topic_seeds は空の配列 [] でよい。'
        )
        existing = ["", "## 既存の会話の種", *([f"- {s['content']}" for s in seeds] or ["（無し）"])] if with_topics else []
        prompt = "\n".join(
            [
                f"{PROFILE_PREFIX}あなたはキャラクター「{character.name}」の記憶を整える係です。"
                f"{character.name}は眠りについたところで、今日の印象メモを、{target}についての印象（プロフィール）にまとめ直します。",
                "",
                "## ルール",
                f"- 印象は{target}の嗜好・性格・関係性・大事な事情などの、長く通用する印象。その日の出来事の記録ではない"
                "（出来事は別にエピソードとして残っている）。",
                f"- 既存の印象を土台に、今日の印象メモを反映して書き直す。{PROFILE_MAX_CHARS}字以内。収まらなければ重要度の低いものから削る。",
                "- 何度も出てくる印象・はっきりした印象ほど詳しく、弱い印象は簡潔に書く。矛盾したら新しい方を優先する。",
                "- 分かった事実（好きなもの・予定・事情）を中心に書き、少ない材料から性格を決めつけない。",
                f"- {character.name}の視点で、常体（だ・である調）で書く。敬語は使わない（箇条書きでも文章でもよい）。",
                *(
                    [f"- 自分については、{character.name}自身の好み・気持ち・こだわりを書く。応対の出来の評価や「成長した」のような振り返りは書かない。"]
                    if subject == SUBJECT_SELF
                    else []
                ),
                topics,
                f"- {no_pronoun_rule(character.user_name)}",
                "",
                "## 既存の印象",
                current or "（まだ無い）",
                "",
                "## 今日の印象メモ",
                *(f"- [{_stamp(m['created_at'])}] {m['content']}" for m in memos),
                *existing,
                "",
                'JSONのみで答えること：{"impression": "書き直した印象", "topic_seeds": ["種"]}',
            ]
        )
        return [{"role": "user", "content": prompt}]

    async def consolidate(self, character_id: str) -> dict[str, Any] | None:
        """睡眠中の整理：残りの短期記憶をすべてエピソード化し、印象メモから印象層を書き直す。"""
        if character_id in self._consolidating:
            return None
        self._consolidating.add(character_id)
        try:
            return await self._consolidate(character_id)
        finally:
            self._consolidating.discard(character_id)

    async def _consolidate(self, character_id: str) -> dict[str, Any]:
        character = self._characters[character_id]
        result: dict[str, Any] = {"episodes": 0, "profiles": {}, "topic_seeds": []}
        # 1. 残りの短期記憶をエピソード化する（眠ったので、続いている会話は無い）
        for _ in range(CONSOLIDATE_MAX_ROUNDS):
            if not self.unprocessed(character_id):
                break
            adjusted = await self.adjust(character_id, finish_all=True)
            result["episodes"] += adjusted["episodes"]
            if not adjusted["processed"]:
                break  # 出力が不完全で進まない。次の機会に回す

        # 2. 印象メモから印象層を書き直す。使い終わった印象メモは消す
        now = self._now()
        for subject in (SUBJECT_USER, SUBJECT_SELF):
            memos = self._db.list_memos(character_id, subject)
            if not memos:
                continue
            data = await self._llm.complete_json(
                self.build_profile_messages(character, subject, memos, with_topics=subject == SUBJECT_USER)
            )
            impression = str((data or {}).get("impression") or "").strip()[:PROFILE_MAX_CHARS]
            if not impression:
                continue
            self._db.save_profile(character_id, subject, impression, now.isoformat())
            self._db.delete_memos([m["id"] for m in memos])
            result["profiles"][subject] = len(impression)
            if subject == SUBJECT_USER:
                for content in _texts(data.get("topic_seeds"), MAX_TOPIC_SEEDS):
                    self._db.add_seed(character_id, content, now.isoformat(), source="topic")
                    result["topic_seeds"].append(content)

        # 3. 期限切れの整理
        result["pruned"] = self._prune(character_id)
        result["expired_seeds"] = self._db.delete_seeds_before(character_id, (now - SEED_TTL).isoformat())
        result["expired_activity"] = self._db.delete_activity_before((now - ACTIVITY_TTL).isoformat())

        profiles = "、".join(
            f"{character.user_name if s == SUBJECT_USER else '自分'}の印象 {n}字" for s, n in result["profiles"].items()
        )
        await self._state.record(
            character_id,
            "memory",
            f"睡眠中の整理：エピソード +{result['episodes']}、{profiles or '印象の更新なし'}、"
            f"話題の種 +{len(result['topic_seeds'])}（時限切れ {result['pruned']['expired']}件・"
            f"上限超過 {result['pruned']['over_limit']}件を削除）",
        )
        await self._publish(character_id)
        return result

    # --- 表示用 ---

    def snapshot(self, character_id: str) -> dict[str, Any]:
        now = self._now()
        episodes = []
        for e in reversed(self._db.list_episodes(character_id)):
            item = {k: v for k, v in e.items() if k != "embedding"}
            item["embedded"] = e["embedding"] is not None
            item["deletable"] = datetime.fromisoformat(e["grace_until"]) <= now  # 削除対象（猶予切れ）
            episodes.append(item)
        profiles = self._db.get_profiles(character_id)
        return {
            "character_id": character_id,
            "profiles": {s: profiles.get(s) for s in (SUBJECT_USER, SUBJECT_SELF)},
            "profile_max_chars": PROFILE_MAX_CHARS,
            "memos": list(reversed(self._db.list_memos(character_id))),
            "seeds": self._db.list_seeds(character_id, since=(now - SEED_TTL).isoformat()),
            "episodes": episodes,
            "unprocessed": len(self.unprocessed(character_id)),
            "rules": {
                "episode_limit": EPISODE_LIMIT,
                "grace_days": [GRACE_MIN.days, GRACE_MAX.days],
                "lifetime_days": LIFETIME.days,
                "seed_ttl_days": SEED_TTL.days,
                "impromptu_probability": IMPROMPTU_PROBABILITY,
                "retrieve_top_k": RETRIEVE_TOP_K,
                "retrieve_min_similarity": RETRIEVE_MIN_SIMILARITY,
            },
        }
