"""python -m engawa.ui で UI を起動する（コアが先に起動している必要がある）。"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from engawa.config import load_settings
from engawa.ui.client import CoreClient
from engawa.ui.main_window import MainWindow
from engawa.ui.tts import Speaker, VoicevoxEngine
from engawa.ui.widgets import APP_STYLE


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("縁側")
    app.setOrganizationName("engawa")  # QSettings（読み上げON/OFFなど）の保存先
    app.setStyleSheet(APP_STYLE)
    settings = load_settings()
    client = CoreClient(settings.core_base_url)
    state: dict = {}

    speaker = None
    if settings.tts_enabled:
        engine = VoicevoxEngine(settings.voicevox_path)
        engine.start()  # 既に動いているエンジンがあれば、そちらが使われる（ポートが埋まっていて新しい方は終了する）
        app.aboutToQuit.connect(engine.stop)
        speaker = Speaker(settings.voicevox_url, settings.voicevox_speaker)

    def fail(status: int, detail: str) -> None:
        QMessageBox.critical(None, "縁側", f"コアに接続できませんでした。\n先に python -m engawa.core を起動してください。\n\n{detail}")
        app.quit()

    def on_characters(characters: list[dict]) -> None:
        sessions: dict[str, list[dict]] = {}

        def on_sessions(character_id: str, result: list[dict]) -> None:
            sessions[character_id] = result
            if len(sessions) == len(characters):
                window = MainWindow(client, characters, sessions, speaker)
                state["window"] = window
                client.connect_events()
                window.show()

        for c in characters:
            client.get(f"/characters/{c['id']}/sessions", lambda result, cid=c["id"]: on_sessions(cid, result), fail)

    client.get("/characters", on_characters, fail)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
