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
- 「状態を見る」：状態確認ビュー（暇度・疲労・眠気、応答可能状態、性格タグ、生活リズム、
  プロンプトに注入される状態、状態イベントの履歴）。キャラクター名の横にも 🟢起きている／🟡取り込み中／🌙睡眠中 を表示

- 「集中モード」：ONの間は対話で話しかけず、`# main` へのメッセージだけで送ってくる

内部状態は `ENGAWA_STATE_TICK_SECONDS`（既定60秒）ごとに実時刻で更新され、コア停止中に経過した時間も起動時に反映される。

## 能動発話

`ENGAWA_JUDGE_INTERVAL_SECONDS`（既定60秒）ごとに、確率的ゲート → 判定層（`ENGAWA_JUDGE_MODEL`）→ 生成層の順で
話しかけるかを決める。起きていれば対話ウィンドウが自動で開き、`ENGAWA_DIALOGUE_TIMEOUT_SECONDS`（既定180秒）
返事がなければ同じ内容が `# main` に送り直される。判定の理由は状態確認ビューのイベント欄に残る。
状態確認ビューの「今すぐ判定」で、ゲートを無視して判定層を呼べる（調整用）。

## 記憶

- **STM**：会話の発言（要約しない）と自己言及記憶。返事のたびに埋め込み（`ENGAWA_EMBED_MODEL`、既定 bge-m3）で
  意味検索し、関連する上位3件をプロンプトに入れて参照回数を数える。期限3日
- **会話の種**：STMとは別に持つ話題のストック。能動発話のときに1つ選ばれる（種があっても20%は即興）。期限3日
- **記憶調整**：判定層が選んだときに、最近の会話から会話の種と自己言及記憶を作る
- **夜間蒸留**：眠りに落ちたら、残っているSTM全件から LTM（2000字以内）を書き直し、
  その日のSTMは参照回数の上位10件だけ残す（残ったものは期限まで持ち越し）

状態確認ビューの「記憶」タブで中身を確認でき、「記憶調整を実行」「蒸留を実行」で手動実行もできる（調整用）。

## テスト

```powershell
.\.venv\Scripts\python.exe -m pytest
```
