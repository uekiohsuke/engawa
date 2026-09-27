# 縁側（Engawa）

能動的に会話ができるデスクトップ上の隣人。仕様は [neighbor-spec.md](neighbor-spec.md)。

## 構成

- **コア**（`engawa/core`）：FastAPI の常駐サーバー。会話履歴DB（SQLite）、LLM 呼び出しを持つ。
  UI へは WebSocket（`/ws`）でイベントを配信する。
- **UI**（`engawa/ui`）：PySide6。Discord 風のチャット本体と、キャラクター一覧から開く対話ウィンドウ。
- **LLM**：Ollama の OpenAI 互換 API。`ENGAWA_LLM_BACKEND=mock` で LLM なしでも動作確認できる。

## セットアップ

```powershell
python -m venv .venv          # Python 3.11
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env   # 必要に応じて編集
```

## 起動

```powershell
.\run.ps1                     # コア＋UI をまとめて起動
# 個別に起動する場合
.\.venv\Scripts\python.exe -m engawa.core
.\.venv\Scripts\python.exe -m engawa.ui
```

- チャンネル `# main`：メッセージのメインスレッド
- キャラクター一覧で名前をダブルクリック：対話ウィンドウ（最前面・ドラッグ移動可）

## テスト

```powershell
.\.venv\Scripts\python.exe -m pytest
```
