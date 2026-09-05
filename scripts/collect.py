"""RSS/Atom フィードの収集・正規化・重複排除。

Bedrock はサーバーサイドの web_search / web_fetch に対応していないため、
「AI にサイトを読ませる」部分はこのモジュールが担う。ここで集めた記事の
タイトル・URL・スニペットだけを summarize.py が Claude に渡す。
"""

from __future__ import annotations

import argparse
import html
import re
import time
import urllib.request
from calendar import timegm
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import feedparser
import yaml

USER_AGENT = (
    "Mozilla/5.0 (compatible; TrendNewsBot/1.0; "
    "+https://github.com/ryukitsushida/trend-news)"
)
FETCH_TIMEOUT_SECONDS = 15
MAX_WORKERS = 8
SNIPPET_MAX_CHARS = 400

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TRACKING_PARAM_RE = re.compile(
    r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$|ref$|ref_src$|spm$)", re.IGNORECASE
)


@dataclass
class Article:
    title: str
    url: str
    source: str
    published: Optional[datetime]
    snippet: str
    category_id: str


@dataclass
class FeedFailure:
    category_id: str
    source: str
    url: str
    error: str


@dataclass
class CategoryResult:
    id: str
    label: str
    articles: list[Article] = field(default_factory=list)
    failures: list[FeedFailure] = field(default_factory=list)


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _strip_tracking_params(query: str) -> str:
    if not query:
        return ""
    kept = []
    for part in query.split("&"):
        if not part:
            continue
        key = part.split("=", 1)[0]
        if _TRACKING_PARAM_RE.match(key):
            continue
        kept.append(part)
    return "&".join(kept)


def normalize_url(url: str) -> str:
    """トラッキングパラメータと末尾スラッシュを除去し、重複判定に使う正規形にする。"""
    if not url:
        return url
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    query = _strip_tracking_params(parts.query)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def normalize_title_key(title: str) -> str:
    """重複排除用キー。空白・記号差を無視して比較する。判定はカテゴリ内で行う。"""
    lowered = title.strip().lower()
    return re.sub(r"[^\w぀-ヿ一-鿿]+", "", lowered)


def clean_snippet(raw_html: str, limit: int = SNIPPET_MAX_CHARS) -> str:
    if not raw_html:
        return ""
    text = _TAG_RE.sub(" ", raw_html)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _entry_published(entry) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            return datetime.fromtimestamp(timegm(value), tz=timezone.utc)
    return None


def fetch_feed_bytes(url: str, timeout: int = FETCH_TIMEOUT_SECONDS) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def collect_category(
    category: dict, window_hours: int, now: Optional[datetime] = None
) -> CategoryResult:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=window_hours)
    result = CategoryResult(id=category["id"], label=category["label"])

    def fetch_one(feed_cfg: dict):
        name, url = feed_cfg["name"], feed_cfg["url"]
        try:
            raw = fetch_feed_bytes(url)
            parsed = feedparser.parse(raw)
            if parsed.bozo and not parsed.entries:
                raise RuntimeError(str(parsed.bozo_exception))
            return name, url, parsed, None
        except Exception as exc:  # noqa: BLE001 - 1フィード失敗で全体を止めない
            return name, url, None, str(exc)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, f) for f in category["feeds"]]
        for fut in as_completed(futures):
            name, url, parsed, error = fut.result()
            if error:
                result.failures.append(
                    FeedFailure(category_id=category["id"], source=name, url=url, error=error)
                )
                continue
            for entry in parsed.entries:
                title = getattr(entry, "title", "").strip()
                link = getattr(entry, "link", "").strip()
                if not title or not link:
                    continue
                published = _entry_published(entry)
                # 日付が取れない場合は除外せず採用する(取りこぼしより重複の方が実害が小さい)
                if published is not None and published < cutoff:
                    continue
                snippet = clean_snippet(getattr(entry, "summary", ""))
                result.articles.append(
                    Article(
                        title=title,
                        url=link,
                        source=name,
                        published=published,
                        snippet=snippet,
                        category_id=category["id"],
                    )
                )

    # 新しい順にソート(日付なしは末尾)
    result.articles.sort(
        key=lambda a: a.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    # カテゴリ内重複排除(正規化URL / 正規化タイトルのどちらかが一致したら重複とみなす)
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[Article] = []
    for article in result.articles:
        url_key = normalize_url(article.url)
        title_key = normalize_title_key(article.title)
        if url_key in seen_urls or (title_key and title_key in seen_titles):
            continue
        seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        deduped.append(article)

    max_items = category.get("max_items")
    result.articles = deduped[:max_items] if max_items else deduped
    return result


def collect_all(config_path: Path, now: Optional[datetime] = None) -> list[CategoryResult]:
    config = load_config(config_path)
    window_hours = config.get("window_hours", 30)
    return [
        collect_category(cat, window_hours, now=now) for cat in config["categories"]
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="RSS収集の単体確認用CLI")
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parent.parent / "config" / "sources.yaml"
    )
    parser.add_argument("--print-summary", action="store_true")
    args = parser.parse_args()

    start = time.time()
    results = collect_all(args.config)
    elapsed = time.time() - start

    for cat in results:
        print(f"[{cat.id}] {cat.label}: {len(cat.articles)} 件")
        if cat.failures:
            for fail in cat.failures:
                print(f"  ! 取得失敗: {fail.source} ({fail.url}) -> {fail.error}")
        if args.print_summary:
            for a in cat.articles[:5]:
                pub = a.published.isoformat() if a.published else "日付なし"
                print(f"    - [{a.source}] {a.title} ({pub})")
                print(f"      {a.url}")

    total = sum(len(c.articles) for c in results)
    total_failures = sum(len(c.failures) for c in results)
    print(f"\n合計 {total} 件 / 失敗フィード {total_failures} 件 / {elapsed:.1f}秒")


if __name__ == "__main__":
    main()
