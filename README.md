# arXiv CS 24時間トーナメント選定ツール

arXiv の CS RSS を取得して、直近24時間（時間数は変更可）に投稿された論文を抽出し、スイス式トーナメント形式で上位論文を選びます。  
ランキングの比較は可能なら OpenAI API で実施し、未利用時はヒューリスティック（タイトル/要約ベース）へフォールバックします。

## 概要

本ツール（`arxiv_swiss_tournament.py`）は以下を行います。

1. RSS 取得（デフォルト: `https://rss.arxiv.org/rss/eess+stat`）
2. 過去一定時間以内（デフォルト 24 時間）に投稿されたエントリを抽出
3. タイトル・要約からヒントスコアを計算
4. スイス式トーナメントを実施して勝敗を累積
5. 上位 N 件を表示

## 必要環境

- Python 3.10 以上（推奨）
- pip

## インストール

```bash
pip install -r requirements.txt
```

## 実行方法

```bash
python arxiv_swiss_tournament.py
```

## テスト実行

```bash
pytest -q
```

`pytest.ini` により `tests/` 配下のみを収集するため、リポジトリ直下でそのまま実行できます。

### 主な引数

- `--url`  
  取得する RSS URL。省略時は `ARXIV_RSS_URL` 環境変数、なければデフォルト URL を使用
- `--hours`  
  抽出対象の時間窓（時間）。デフォルト: 24
- `--top-n`  
  表示する上位数。デフォルト: 4
- `--model`  
  OpenAI 比較に使うモデル名。デフォルト: `gpt-5.4`
- `--rounds`  
  トーナメント回数（省略時は参加数から `ceil(log2(n))`）
- `--timeout`  
  OpenAI API タイムアウト秒（デフォルト: 15）
- `--progress-step`  
  詳細ログOFF時の進捗表示間隔（既定: 25）
- `--verbose`  
  対戦ごとの詳細ログを表示
- `--openai-debug`  
  OpenAI 判定失敗時のデバッグ情報を表示

## 環境変数

- `ARXIV_RSS_URL`  
  RSS URL のデフォルト値として利用
- `ARXIV_HOURS`  
  `--hours` のデフォルト値として利用
- `ARXIV_TOP_N`  
  `--top-n` のデフォルト値として利用
- `OPENAI_MODEL`  
  `--model` のデフォルト値として利用
- `OPENAI_TIMEOUT_SECONDS`  
  `--timeout` のデフォルト値として利用
- `OPENAI_API_KEY`  
  OpenAI API を使う場合に必須。未設定時はヒューリスティック判定のみで進行

## 仕様・動作の補足

- OpenAI SDK が入っておらず、あるいは API キー未設定でも、`feedparser` のみでランキング処理は進みます（比較はヒューリスティック）。
- 各試合の結果は `{"winner":"A"}` / `{"winner":"B"}` / `{"winner":"TIE"}` を返すことを前提にパースし、解析不能時はヒューリスティック決定。
- 対象論文が0件の場合は「24時間以内の新規投稿が見つかりませんでした。」で終了します。

## 例

```bash
python arxiv_swiss_tournament.py --hours 48 --top-n 6 --verbose
```

