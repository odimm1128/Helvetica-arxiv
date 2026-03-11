"""arXiv CS RSS の24時間以内論文をスイス式トーナメントで上位4本選出するスクリプト。"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import math
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Set, Tuple

try:
    import feedparser
except Exception as exc:
    raise SystemExit(f"依存エラー: feedparserが必要です。pip install feedparser を実行してください。 ({exc})")

try:
    import openai
except Exception:
    openai = None

try:
    from bs4 import BeautifulSoup

    HAS_BS4 = True
except Exception:
    BeautifulSoup = None
    HAS_BS4 = False


DEFAULT_FEED_URL = "https://rss.arxiv.org/rss/eess+stat"
DEFAULT_HOURS = 24
DEFAULT_TOP_N = 4
DEFAULT_MODEL = "gpt-5.4" # gpt-4o-mini
DEFAULT_MAX_ROUNDS = None
DEFAULT_PROGRESS_STEP = 25
PROGRESS_PREFIX = "[進捗]"

VERBOSE = False
OPENAI_DEBUG = False


@dataclass
class Paper:
    paper_id: str
    title: str
    summary: str
    published: datetime


@dataclass
class Competitor:
    paper: Paper
    points: float = 0.0
    wins: int = 0
    played: Set[str] = field(default_factory=set)
    had_bye: bool = False
    base_hint_score: float = 0.0


NOVELTY_KEYWORDS = {
    "novel",
    "new",
    "novelty",
    "state-of-the-art",
    "benchmark",
    "framework",
    "transformer",
    "graph",
    "quantum",
    "robotics",
    "vision",
}


def log_progress(message: str, *, force: bool = False) -> None:
    if force or VERBOSE:
        print(f"{PROGRESS_PREFIX} {message}")


def log_openai_debug(message: str) -> None:
    if OPENAI_DEBUG:
        print(f"{PROGRESS_PREFIX} [OpenAI] {message}")

def clean_text(raw: str) -> str:
    if not raw:
        return ""
    raw = html.unescape(raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if HAS_BS4 and BeautifulSoup is not None:
        try:
            return BeautifulSoup(raw, "html.parser").get_text(separator="\n").strip()
        except Exception:
            pass
    return re.sub(r"<[^>]+>", "", raw).strip()




def extract_abstract(entry: Dict[str, object]) -> str:
    """RSS item から要約/アブストを抽出する."""

    def first_nonempty_text(value: object) -> str:
        if not value:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    return item
                if isinstance(item, dict):
                    v = item.get("value") or item.get("body") or item.get("summary")
                    if isinstance(v, str):
                        return v
            return ""
        if isinstance(value, dict):
            v = value.get("value") or value.get("body") or value.get("summary")
            if isinstance(v, str):
                return v
        return ""

    raw = (
        first_nonempty_text(entry.get("description"))
        or first_nonempty_text(entry.get("summary"))
        or first_nonempty_text(entry.get("content"))
    )
    raw = clean_text(raw)
    if not raw:
        return ""

    m = re.search(r"(?is)Abstract:\s*(.*)", raw)
    if m:
        return clean_text(m.group(1))
    return raw


def parse_published(entry: Dict[str, object]) -> Optional[datetime]:
    for key in ("published", "updated", "created", "pubDate"):
        raw = entry.get(key)
        if isinstance(raw, str):
            parsed = parsedate_to_datetime(raw)
            if parsed is not None:
                if parsed.tzinfo is None:
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)

    parsed_raw = entry.get("published_parsed")
    if parsed_raw:
        try:
            return datetime.fromtimestamp(calendar.timegm(parsed_raw), tz=timezone.utc)
        except Exception:
            return None
    return None


def compute_hint_score(text: str, summary: str) -> float:
    t = clean_text(text).lower()
    s = clean_text(summary).lower()
    title_words = re.findall(r"[A-Za-z0-9]+", t)
    summary_words = re.findall(r"[A-Za-z0-9]+", s)

    title_score = min(len(title_words), 20) * 0.25
    summary_score = min(len(summary_words), 300) * 0.01
    keyword_score = sum(1.2 for kw in NOVELTY_KEYWORDS if kw in t or kw in s)
    return round(title_score + summary_score + keyword_score, 4)


def fetch_recent_papers(url: str, hours: int) -> List[Paper]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    log_progress(f"RSS取得開始: {url}", force=True)
    feed = feedparser.parse(url)
    log_progress(f"取得済みエントリ数: {len(feed.entries)}", force=True)

    if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
        raise RuntimeError(f"RSS取得に失敗しました: {url}")

    papers: List[Paper] = []
    checked = 0
    skipped_notime = 0
    skipped_outside = 0
    skipped_empty = 0

    for idx, entry in enumerate(feed.entries, start=1):
        checked = idx
        dt = parse_published(entry)
        if dt is None:
            skipped_notime += 1
            if VERBOSE:
                log_progress(f"skip({idx}件目): publishedを解析できません")
            continue
        if dt < cutoff:
            skipped_outside += 1
            if VERBOSE:
                log_progress(f"skip({idx}件目): 24時間外 ({dt.isoformat()})")
            continue

        title = clean_text(entry.get("title", "") or "")
        summary = extract_abstract(entry)
        if not title or not summary:
            skipped_empty += 1
            if VERBOSE:
                log_progress(f"skip({idx}件目): タイトルまたは要約が空")
            continue

        paper_id = (
            str(entry.get("id") or entry.get("link") or entry.get("guid") or "").strip()
            or f"arxiv-unknown-{len(papers)+1}"
        )
        papers.append(
            Paper(
                paper_id=paper_id,
                title=title,
                summary=summary,
                published=dt,
            )
        )

    log_progress(
        f"抽出完了: 24時間以内 {len(papers)}件 / チェック {checked}件 "
        f"(published欠損:{skipped_notime}, 期限外:{skipped_outside}, 内容不足:{skipped_empty})",
        force=True,
    )
    return papers


def rank_competitors(comps: List[Competitor]) -> List[Competitor]:
    return sorted(
        comps,
        key=lambda c: (c.points, c.wins, c.base_hint_score, c.paper.paper_id),
        reverse=True,
    )


def choose_bye(ordered: List[Competitor]) -> Tuple[Optional[Competitor], List[Competitor]]:
    if not ordered or len(ordered) % 2 == 0:
        return None, ordered

    bye: Optional[Competitor] = None
    for cand in reversed(ordered):
        if not cand.had_bye:
            bye = cand
            break
    if bye is None:
        bye = ordered[-1]

    bye.had_bye = True
    bye.points += 1.0
    log_progress(f"Bye付与: {bye.paper.title[:50]}", force=VERBOSE)
    return bye, [c for c in ordered if c.paper.paper_id != bye.paper.paper_id]


def make_pairs(ordered: List[Competitor]) -> List[Tuple[Competitor, Competitor]]:
    remaining = ordered[:]
    pairs: List[Tuple[Competitor, Competitor]] = []

    while remaining:
        p = remaining.pop(0)
        opponent_index = None
        for i, q in enumerate(remaining):
            if q.paper.paper_id not in p.played:
                opponent_index = i
                break
        if opponent_index is None:
            opponent_index = 0

        q = remaining.pop(opponent_index)
        pairs.append((p, q))

    return pairs


def heuristic_decide(a: Competitor, b: Competitor) -> str:
    if a.base_hint_score > b.base_hint_score:
        return "A"
    if b.base_hint_score > a.base_hint_score:
        return "B"
    if a.paper.title != b.paper.title:
        return "A" if a.paper.title < b.paper.title else "B"
    return "A" if random.random() >= 0.5 else "B"


def parse_ai_decision(content: str) -> Optional[str]:
    if not content:
        return None
    try:
        parsed = json.loads(content.strip())
        winner = str(parsed.get("winner", "")).strip().upper()
        if winner in {"A", "B", "TIE"}:
            return winner
    except Exception:
        pass

    cleaned = content.strip().upper()
    if re.search(r"\b(A|1)\b", cleaned):
        return "A"
    if re.search(r"\b(B|2)\b", cleaned):
        return "B"
    return None


def ask_openai(a: Competitor, b: Competitor, model: str, timeout_s: int = 15) -> Optional[str]:
    if openai is None:
        log_openai_debug("openaiライブラリ未インストール")
        return None

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log_openai_debug("OPENAI_API_KEY未設定")
        return None

    system_prompt = (
        "あなたは論文評価エージェントです。2つの論文を対戦形式で評価し、"
        "どちらがarXiv CSコミュニティでより注目される可能性が高いかを判断してください。"
    )
    user_prompt = f"""
以下の2つの論文を比較し、勝者を1つだけ選んでください。

論文A:
タイトル: {a.paper.title}
要約: {a.paper.summary}

論文B:
タイトル: {b.paper.title}
要約: {b.paper.summary}

以下の観点で比較してください:
- 新規性
- 技術的な新しさ
- 論文としての完成度

結果は厳密なJSONのみを返してください。
{{"winner":"A"}} または {{"winner":"B"}} または {{"winner":"TIE"}} のみ。
"""

    if VERBOSE:
        log_progress(f"AI判定: {a.paper.title[:30]} vs {b.paper.title[:30]}")
    try:
        client = openai.OpenAI(api_key=api_key, timeout=timeout_s)
        kwargs = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
        }
        try:
            resp = client.chat.completions.create(**kwargs, response_format={"type": "json_object"})
        except TypeError:
            # 古いSDK互換
            resp = client.chat.completions.create(**kwargs)

        content = None
        if resp and getattr(resp, 'choices', None):
            message = resp.choices[0].message
            if message is not None:
                content = getattr(message, 'content', None)

        if content is None:
            log_openai_debug("APIレスポンス本文が空でした")
            return None

        winner = parse_ai_decision(content)
        if winner is None:
            preview = str(content).strip().replace("\n", " ")
            if len(preview) > 240:
                preview = preview[:240]
            log_openai_debug(f"AI応答をパースできません: {preview}")
            return None

        if VERBOSE:
            log_progress(f"AI結果: {winner}")
        return winner
    except Exception as exc:
        log_openai_debug(f"API呼び出し例外: {type(exc).__name__}: {exc}")
        return None


def decide_match(a: Competitor, b: Competitor, model: str, timeout_s: int) -> Tuple[str, bool]:
    winner = ask_openai(a, b, model=model, timeout_s=timeout_s)
    if winner in {"A", "B", "TIE"}:
        return winner, False

    return heuristic_decide(a, b), True


def update_standings(a: Competitor, b: Competitor, winner: str) -> None:
    a.played.add(b.paper.paper_id)
    b.played.add(a.paper.paper_id)

    if winner == "A":
        a.points += 1.0
        a.wins += 1
    elif winner == "B":
        b.points += 1.0
        b.wins += 1
    else:
        a.points += 0.5
        b.points += 0.5


def run_tournament(
    papers: List[Paper],
    rounds: int,
    model: str,
    timeout_s: int,
    progress_step: int,
) -> List[Competitor]:
    competitors = [
        Competitor(
            paper=p,
            base_hint_score=compute_hint_score(p.title, p.summary),
        )
        for p in papers
    ]

    total_rounds = max(1, rounds)
    fallback_count = 0

    for rn in range(1, total_rounds + 1):
        ordered = rank_competitors(competitors)
        log_progress(f"ラウンド {rn}/{total_rounds} 開始（参加者 {len(ordered)}）", force=True)
        _, active = choose_bye(ordered)
        if len(active) < 2:
            log_progress("対戦可能人数不足。ラウンドを終了します", force=True)
            continue

        pairs = make_pairs(active)
        log_progress(f"対戦ペア数: {len(pairs)}", force=True)

        for idx, (left, right) in enumerate(pairs, start=1):
            winner, fallback = decide_match(
                left,
                right,
                model=model,
                timeout_s=timeout_s,
            )
            if fallback:
                fallback_count += 1
            update_standings(left, right, winner)

            if VERBOSE:
                winner_name = (
                    left.paper.title if winner == "A" else right.paper.title if winner == "B" else "TIE"
                )
                log_progress(
                    f"対戦結果: {left.paper.title[:35]} ... vs {right.paper.title[:35]} ... => {winner_name}"
                )
            elif idx % progress_step == 0 or idx == len(pairs):
                log_progress(
                    f"ラウンド {rn}: 対戦進捗 {idx}/{len(pairs)}"
                )

    ranked = rank_competitors(competitors)
    log_progress(f"最終ランキング確定（AIフォールバック回数: {fallback_count}）", force=True)
    return ranked


def print_top(comp: List[Competitor], top_n: int) -> None:
    if not comp:
        print("対象論文が見つかりませんでした。")
        return

    top = comp[:top_n]
    print(f"\n上位{len(top)}件")
    for i, c in enumerate(top, 1):
        print(f"\n{i}位: {c.paper.title}")
        print(f"スコア: {c.points:.2f}")
        print(f"アブスト: {c.paper.summary}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="arXiv CS RSS Swiss Tournament")
    parser.add_argument("--url", default=os.getenv("ARXIV_RSS_URL", DEFAULT_FEED_URL))
    parser.add_argument("--hours", type=int, default=int(os.getenv("ARXIV_HOURS", DEFAULT_HOURS)))
    parser.add_argument("--top-n", type=int, default=int(os.getenv("ARXIV_TOP_N", DEFAULT_TOP_N)))
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    parser.add_argument("--timeout", type=int, default=int(os.getenv("OPENAI_TIMEOUT_SECONDS", 15)))
    parser.add_argument("--verbose", action="store_true", help="対戦1件ごとの詳細ログを表示")
    parser.add_argument("--openai-debug", action="store_true", help="OpenAI判定失敗の原因を詳細表示")
    parser.add_argument(
        "--progress-step",
        type=int,
        default=DEFAULT_PROGRESS_STEP,
        help="対戦進捗ログの間隔（詳細ログ無効時）",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.hours <= 0:
        raise SystemExit("hours は正の整数である必要があります。")
    if args.top_n <= 0:
        raise SystemExit("top-n は1以上です。")
    if args.progress_step <= 0:
        args.progress_step = DEFAULT_PROGRESS_STEP

    global VERBOSE
    global OPENAI_DEBUG
    VERBOSE = bool(args.verbose)
    OPENAI_DEBUG = bool(args.openai_debug)

    log_progress("スクリプト開始", force=True)
    if openai is None:
        print("警告: openaiライブラリがインストールされていません。ヒューリスティックフォールバックのみで進めます。")
    if not os.getenv("OPENAI_API_KEY"):
        print("警告: OPENAI_API_KEY が未設定です。ヒューリスティックフォールバックで進めます。")

    papers = fetch_recent_papers(args.url, args.hours)
    if not papers:
        print("24時間以内の新規投稿が見つかりませんでした。")
        return

    rounds = args.rounds
    if rounds is None:
        rounds = math.ceil(math.log2(len(papers)))

    result = run_tournament(
        papers,
        rounds=rounds,
        model=args.model,
        timeout_s=args.timeout,
        progress_step=args.progress_step,
    )
    print(f"入力論文数: {len(result)} / 24時間以内抽出")
    print(f"実施ラウンド数: {max(1, rounds)}")
    print(f"使用モデル: {args.model}")
    print_top(result, args.top_n)


if __name__ == "__main__":
    main()


