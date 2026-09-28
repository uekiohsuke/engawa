"""読み上げ（TTS）：VOICEVOX で対話の発言を文ごとに合成し、順番に再生する（仕様5章）。

音声は UI を動かしているマシンで鳴らすため、コアではなく UI 側に置く。
生成中のストリームを文の区切りで切り出し、前の文を再生している間に次の文を合成しておく。
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from collections import deque
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QObject, QUrl, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

log = logging.getLogger(__name__)

SENTENCE_END = re.compile(r"[。！？!?\n]+[」』）)]*")
TMP_DIR = Path(tempfile.gettempdir()) / "engawa_tts"


def split_sentences(buffer: str) -> tuple[list[str], str]:
    """文の区切りまでを切り出す。残り（まだ文が終わっていない部分）も返す。"""
    sentences, start = [], 0
    for match in SENTENCE_END.finditer(buffer):
        sentences.append(buffer[start : match.end()])
        start = match.end()
    return [s for s in (s.strip() for s in sentences) if s], buffer[start:]


def clean_for_speech(text: str) -> str:
    """読み上げに向かない部分を取り除く。"""
    text = text.replace("(mock)", "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>|~]", "", text)  # Markdown の記号
    text = re.sub(r"[…‥]+", "…", text)
    return text.strip()


def has_speakable(text: str) -> bool:
    return re.search(r"[\w぀-ヿ一-鿿]", text) is not None


class VoicevoxEngine:
    """VOICEVOX エンジンの起動・終了。既に動いていればそれを使い、自分で起動したものだけを終了する。"""

    def __init__(self, exe_path: str | None):
        self._exe_path = exe_path
        self._process: subprocess.Popen | None = None

    def start(self) -> bool:
        if not self._exe_path or not Path(self._exe_path).exists():
            log.warning("VOICEVOX engine not found: %s", self._exe_path)
            return False
        try:
            self._process = subprocess.Popen(
                [self._exe_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except OSError:
            log.exception("failed to start VOICEVOX engine")
            return False

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
        self._process = None


class Speaker(QObject):
    """文ごとの合成と再生の待ち行列。"""

    speaking_changed = Signal(bool)

    def __init__(self, base_url: str, speaker_id: int, parent: QObject | None = None):
        super().__init__(parent)
        self._base_url = base_url.rstrip("/")
        self._speaker_id = speaker_id
        self._nam = QNetworkAccessManager(self)
        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._player.mediaStatusChanged.connect(self._on_media_status)
        self._pending_text: deque[str] = deque()  # 合成待ちの文
        self._ready: deque[Path] = deque()  # 合成済み・再生待ちの音声
        self._synthesizing = False
        self._playing = False
        self._generation = 0  # stop() で古い合成結果を捨てるための世代番号
        self._buffer = ""
        self._counter = 0
        self.enabled = True
        TMP_DIR.mkdir(exist_ok=True)
        for old in TMP_DIR.glob("*.wav"):
            old.unlink(missing_ok=True)

    # --- 入力 ---

    def begin_stream(self) -> None:
        self._buffer = ""

    def feed(self, delta: str) -> None:
        """生成中の断片を受け取り、文が終わったものから読み上げに回す。"""
        self._buffer += delta
        sentences, self._buffer = split_sentences(self._buffer)
        for sentence in sentences:
            self.say(sentence)

    def end_stream(self) -> None:
        rest, self._buffer = self._buffer, ""
        if rest.strip():
            self.say(rest.strip())

    def say(self, text: str) -> None:
        text = clean_for_speech(text)
        if not self.enabled or not has_speakable(text):
            return
        self._pending_text.append(text)
        self._synthesize_next()

    def stop(self) -> None:
        """読み上げを止め、待ち行列を空にする。"""
        self._generation += 1
        self._pending_text.clear()
        self._ready.clear()
        self._buffer = ""
        self._synthesizing = False
        self._player.stop()
        self._set_playing(False)

    # --- 合成（audio_query → synthesis） ---

    def _synthesize_next(self) -> None:
        if self._synthesizing or not self._pending_text:
            return
        self._synthesizing = True
        text = self._pending_text.popleft()
        generation = self._generation
        url = f"{self._base_url}/audio_query?text={quote(text)}&speaker={self._speaker_id}"
        reply = self._nam.post(QNetworkRequest(QUrl(url)), b"")
        reply.finished.connect(lambda: self._on_query(reply, generation))

    def _on_query(self, reply: QNetworkReply, generation: int) -> None:
        body = bytes(reply.readAll().data())
        failed = reply.error() != QNetworkReply.NetworkError.NoError
        if failed:
            log.warning("VOICEVOX audio_query failed: %s", reply.errorString())
        reply.deleteLater()
        if generation != self._generation:
            return
        if failed:
            self._synthesizing = False
            self._synthesize_next()
            return
        request = QNetworkRequest(QUrl(f"{self._base_url}/synthesis?speaker={self._speaker_id}"))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        synth = self._nam.post(request, body)
        synth.finished.connect(lambda: self._on_synthesis(synth, generation))

    def _on_synthesis(self, reply: QNetworkReply, generation: int) -> None:
        wav = bytes(reply.readAll().data())
        failed = reply.error() != QNetworkReply.NetworkError.NoError
        if failed:
            log.warning("VOICEVOX synthesis failed: %s", reply.errorString())
        reply.deleteLater()
        if generation != self._generation:
            return
        self._synthesizing = False
        if not failed:
            self._counter += 1
            path = TMP_DIR / f"{self._counter:06d}.wav"
            path.write_bytes(wav)
            self._ready.append(path)
            self._play_next()
        self._synthesize_next()  # 再生中に次の文を合成しておく

    # --- 再生 ---

    def _play_next(self) -> None:
        if self._playing or not self._ready:
            return
        path = self._ready.popleft()
        self._set_playing(True)
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.play()

    def _on_media_status(self, status: QMediaPlayer.MediaStatus) -> None:
        if status in (QMediaPlayer.MediaStatus.EndOfMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            self._set_playing(False)
            self._play_next()

    def _set_playing(self, playing: bool) -> None:
        if self._playing != playing:
            self._playing = playing
            self.speaking_changed.emit(playing)
