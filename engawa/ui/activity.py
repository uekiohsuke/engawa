"""前面ウィンドウの観測（会話の種の材料。仕様3章）。

ユーザーの画面を見るのは UI 側。前面ウィンドウのタイトルと実行ファイル名だけを一定間隔で取り、コアに送る。
次のときは送らない（コアにも残らない）：
- 「画面を見る」が OFF
- 集中モード中
- 除外に指定したアプリ（実行ファイル名かタイトルに含まれる語で指定）
- 縁側自身のウィンドウ
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QObject, QTimer

from engawa.ui.client import CoreClient

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def foreground_window() -> tuple[str, str, int] | None:
    """前面ウィンドウの（実行ファイル名, タイトル, プロセスID）。取れなければ None。"""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # 64ビットでハンドルが切り詰められないよう、戻り値と引数の型を明示する
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    title = buffer.value.strip()

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app = ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if handle:
        try:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                app = Path(path.value).name
        finally:
            kernel32.CloseHandle(handle)
    if not title and not app:
        return None
    return app or "(不明)", title, pid.value


def is_excluded(app: str, title: str, exclude: list[str]) -> bool:
    haystack = f"{app}\n{title}".lower()
    return any(word and word.lower() in haystack for word in exclude)


class ActivityWatcher(QObject):
    def __init__(self, client: CoreClient, interval_seconds: float, exclude: list[str], parent: QObject | None = None):
        super().__init__(parent)
        self._client = client
        self._exclude = exclude
        self._own_pid = os.getpid()
        self.enabled = True
        self.focus_mode = False
        self._timer = QTimer(self, interval=int(interval_seconds * 1000))
        self._timer.timeout.connect(self.observe)

    def start(self) -> None:
        self._timer.start()

    def observe(self) -> None:
        if not self.enabled or self.focus_mode:
            return
        window = foreground_window()
        if window is None:
            return
        app, title, pid = window
        if pid == self._own_pid or is_excluded(app, title, self._exclude):
            return
        self._client.post("/activity", {"app": app, "title": title}, on_error=lambda status, detail: None)
