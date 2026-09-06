"""RSS/Atom フィードの収集・正規化・重複排除。

Bedrock はサーバーサイドの web_search / web_fetch に対応していないため、
「AI にサイトを読ませる」部分はこのモジュールが担う。ここで集めた記事の
タイトル・URL・スニペットだけを summarize.py が Claude に渡す。

カテゴリは記事の内容で決まるため、ここでは全フィードを1つのプールにまとめる。
話題が1つに定まるフィードだけ設定で category を固定し、AI分類をスキップする。
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

# タグ名で始まるものだけをタグとみなす。単純な <[^>]+> だと
# 「a < b かつ c > d」のような地の文まで丸ごと削ってしまう。
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>|<!--.*?-->", re.S)
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
    # 設定でカテゴリを固定しているフィード由来ならその id。None なら分類パスに回す。
    category_id: Optional[str] = None
    # コミュニティでの注目度(はてブのブックマーク数、HNのポイント数)。
    # 取れないフィードもあるので None を許容する。
    popularity: Optional[int] = None
    popularity_label: Optional[str] = None


@dataclass
class FeedFailure:
    source: str
    url: str
    error: str


@dataclass
class Collection:
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
    """重複排除用キー。空白・記号差を無視して比較する。"""
    return re.sub(r"[^\w]+", "", title.strip().lower())


def clean_snippet(raw_html: str, limit: int = SNIPPET_MAX_CHARS) -> str:
    """RSSの概要をプロンプトと表示に使える素のテキストにする。

    エンティティを戻した結果また擬似タグになる場合があるため、
    タグ除去はエンティティ復元の後にもう一度かける。
    """
    if not raw_html:
        return ""
    text = _TAG_RE.sub(" ", raw_html)
    text = _TAG_RE.sub(" ", html.unescape(text))
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


_HN_POINTS_RE = re.compile(r"Points:\s*(\d+)")
# Hacker News の summary は「Article URL / Comments URL / Points / # Comments」の
# 定型文だけで内容を含まない。そのまま渡してもトークンの無駄なので落とす。
_HN_BOILERPLATE_RE = re.compile(r"(Article URL|Comments URL|Points|# Comments)\s*:", re.I)


def _extract_popularity(entry) -> tuple[Optional[int], Optional[str]]:
    """コミュニティでの注目度を取り出す。取れないフィードでは (None, None)。"""
    count = entry.get("hatena_bookmarkcount")
    if count is not None:
        try:
            n = int(count)
            return n, f"{n} users"
        except (TypeError, ValueError):
            pass

    m = _HN_POINTS_RE.search(getattr(entry, "summary", "") or "")
    if m:
        n = int(m.group(1))
        return n, f"{n} points"

    return None, None


def _entry_published(entry) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        value = getattr(entry, key, None)
        if value:
            return datetime.fromtimestamp(timegm(value), tz=timezone.utc)
    return None


def _open(url: str, timeout: int):
    """http(s) 以外は開かない。

    URLは設定ファイル由来だが、配信元のドメインが乗っ取られた場合に
    file: など別スキームへリダイレクトされる経路を塞ぐ。
    """
    from urllib.parse import urlsplit

    if urlsplit(url).scheme not in ("http", "https"):
        raise ValueError(f"http(s) 以外のURLは取得しない: {url[:80]}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)  # nosec B310 - 上でスキーム検証済み


def fetch_feed_bytes(url: str, timeout: int = FETCH_TIMEOUT_SECONDS) -> bytes:
    with _open(url, timeout) as resp:
        return resp.read()


# --------------------------------------------------------------------------
# JSON API ソース
#
# RSSで人気度を返すのは、はてブ(bookmarkcount)と Hacker News(Points)だけ。
# Qiita / Zenn / Lobsters は公開JSON APIで LGTM・いいね・スコアを返すので、
# そちらから取って「いま何が読まれているか」の実測値を得る。
# --------------------------------------------------------------------------

def fetch_json(url: str, timeout: int = FETCH_TIMEOUT_SECONDS):
    import json

    with _open(url, timeout) as resp:
        return json.loads(resp.read())


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _adapter_qiita(cfg: dict) -> list[Article]:
    """Qiita: 直近数日のうちストック数が閾値以上の記事(=実際に読まれた記事)。"""
    from urllib.parse import quote

    days = cfg.get("days", 5)
    min_stocks = cfg.get("min_stocks", 20)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    query = quote(f"created:>={since} stocks:>={min_stocks}")
    items = fetch_json(
        f"https://qiita.com/api/v2/items?page=1&per_page=20&query={query}"
    )
    out = []
    for it in items:
        likes = it.get("likes_count") or 0
        out.append(
            Article(
                title=it.get("title", "").strip(),
                url=it.get("url", ""),
                source=cfg["name"],
                published=_parse_iso(it.get("created_at")),
                snippet=clean_snippet(it.get("body", ""))[:300],
                category_id=cfg.get("category"),
                popularity=likes,
                popularity_label=f"{likes} LGTM",
            )
        )
    return out


def _adapter_zenn(cfg: dict) -> list[Article]:
    """Zenn: トレンド(order=daily)。RSSは新着順で人気度が取れないためAPIを使う。"""
    data = fetch_json("https://zenn.dev/api/articles?order=daily&count=30")
    out = []
    for a in data.get("articles", []):
        liked = a.get("liked_count") or 0
        path = a.get("path") or ""
        out.append(
            Article(
                title=a.get("title", "").strip(),
                url=f"https://zenn.dev{path}" if path.startswith("/") else path,
                source=cfg["name"],
                published=_parse_iso(a.get("published_at")),
                snippet="",
                category_id=cfg.get("category"),
                popularity=liked,
                popularity_label=f"{liked} いいね",
            )
        )
    return out


def _adapter_lobsters(cfg: dict) -> list[Article]:
    """Lobsters: hottest。テキスト投稿は url が空なので議論ページで代替する。"""
    items = fetch_json("https://lobste.rs/hottest.json")
    out = []
    for it in items:
        score = it.get("score") or 0
        url = it.get("url") or it.get("short_id_url") or ""
        out.append(
            Article(
                title=it.get("title", "").strip(),
                url=url,
                source=cfg["name"],
                published=_parse_iso(it.get("created_at")),
                snippet=clean_snippet(it.get("description", "")),
                category_id=cfg.get("category"),
                popularity=score,
                popularity_label=f"{score} score",
            )
        )
    return out


JSON_ADAPTERS = {
    "qiita": _adapter_qiita,
    "zenn": _adapter_zenn,
    "lobsters": _adapter_lobsters,
}


def _fetch_json_source(cfg: dict):
    try:
        return cfg, JSON_ADAPTERS[cfg["kind"]](cfg), None
    except Exception as exc:  # noqa: BLE001 - 1ソース失敗で全体を止めない
        return cfg, [], str(exc)


def _fetch_feed(feed_cfg: dict):
    try:
        parsed = feedparser.parse(fetch_feed_bytes(feed_cfg["url"]))
        if parsed.bozo and not parsed.entries:
            raise RuntimeError(str(parsed.bozo_exception))
        return feed_cfg, parsed, None
    except Exception as exc:  # noqa: BLE001 - 1フィード失敗で全体を止めない
        return feed_cfg, None, str(exc)


def _entries_to_articles(entries, feed_cfg: dict) -> list[Article]:
    """feedparser のエントリを Article に変換する。期間や重複の判定は呼び出し側。"""
    articles = []
    for entry in entries:
        popularity, popularity_label = _extract_popularity(entry)
        snippet = clean_snippet(getattr(entry, "summary", ""))
        if _HN_BOILERPLATE_RE.search(snippet):
            snippet = ""
        articles.append(
            Article(
                title=getattr(entry, "title", "").strip(),
                url=getattr(entry, "link", "").strip(),
                source=feed_cfg["name"],
                published=_entry_published(entry),
                snippet=snippet,
                category_id=feed_cfg.get("category"),
                popularity=popularity,
                popularity_label=popularity_label,
            )
        )
    return articles


def _sort_key(a: Article) -> datetime:
    return a.published or datetime.min.replace(tzinfo=timezone.utc)


def _take(articles, keep, cutoff, limit, order):
    """期間内の記事を order の降順で limit 件まで採る。"""
    picked = [a for a in articles if keep(a, cutoff)]
    picked.sort(key=order, reverse=True)
    return picked[:limit]


def collect_all(
    config_path: Path,
    now: Optional[datetime] = None,
    exclude_urls: Optional[set[str]] = None,
) -> Collection:
    """全フィードを1つのプールに集める。

    公式ブログや注意喚起は更新頻度が低く、ニュース系と同じ窓では拾えない。
    そこで `slow: true` のフィードだけ窓を広げ、代わりに `exclude_urls`
    (過去に掲載済みのURL)を除くことで同じ記事が何日も出続けるのを防ぐ。
    """
    config = load_config(config_path)
    now = now or datetime.now(timezone.utc)
    fast_cutoff = now - timedelta(hours=config.get("window_hours", 30))
    slow_cutoff = now - timedelta(hours=config.get("slow_window_hours", 240))
    per_feed_cap = config.get("max_items_per_feed", 10)
    excluded = exclude_urls or set()

    def cutoff_for(cfg: dict) -> datetime:
        return slow_cutoff if cfg.get("slow") else fast_cutoff

    def keep(article: Article, cutoff: datetime) -> bool:
        if not article.title or not article.url:
            return False
        if normalize_url(article.url) in excluded:
            return False
        # 日付が取れない場合は除外せず採用する(取りこぼしより重複の方が実害が小さい)
        return article.published is None or article.published >= cutoff

    result = Collection()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        json_futures = [
            pool.submit(_fetch_json_source, c) for c in config.get("json_sources", [])
        ]
        feed_futures = [pool.submit(_fetch_feed, f) for f in config["feeds"]]

        for fut in as_completed(json_futures):
            cfg, articles, error = fut.result()
            if error:
                result.failures.append(
                    FeedFailure(source=cfg["name"], url=cfg["kind"], error=error)
                )
                continue
            # 人気度が取れるソースなので、新着順ではなく人気順に上限まで取る
            result.articles.extend(
                _take(articles, keep, cutoff_for(cfg), per_feed_cap,
                      lambda a: a.popularity or 0)
            )

        for fut in as_completed(feed_futures):
            feed_cfg, parsed, error = fut.result()
            if error:
                result.failures.append(
                    FeedFailure(source=feed_cfg["name"], url=feed_cfg["url"], error=error)
                )
                continue
            # 更新の多いフィードが枠を独占しないよう、新しい順に上限まで
            result.articles.extend(
                _take(_entries_to_articles(parsed.entries, feed_cfg), keep,
                      cutoff_for(feed_cfg), per_feed_cap, _sort_key)
            )

    result.articles.sort(key=_sort_key, reverse=True)
    result.articles = _dedupe(result.articles)
    return result


def _dedupe(articles: list[Article]) -> list[Article]:
    """正規化URL / 正規化タイトルのどちらかが一致したら同じ記事とみなす。

    カテゴリ分けの前に行うので、同じ記事が複数カテゴリに現れることはない。
    """
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    deduped: list[Article] = []
    for article in articles:
        url_key = normalize_url(article.url)
        title_key = normalize_title_key(article.title)
        if url_key in seen_urls or (title_key and title_key in seen_titles):
            continue
        seen_urls.add(url_key)
        if title_key:
            seen_titles.add(title_key)
        deduped.append(article)
    return deduped


def main() -> None:
    parser = argparse.ArgumentParser(description="RSS収集の単体確認用CLI")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "config" / "sources.yaml",
    )
    parser.add_argument("--print-summary", action="store_true")
    args = parser.parse_args()

    start = time.time()
    col = collect_all(args.config)
    elapsed = time.time() - start

    fixed = sum(1 for a in col.articles if a.category_id)
    print(f"合計 {len(col.articles)} 件(カテゴリ固定 {fixed} / 要分類 {len(col.articles) - fixed})")
    print(f"取得失敗 {len(col.failures)} フィード / {elapsed:.1f}秒\n")

    by_source: dict[str, int] = {}
    for a in col.articles:
        by_source[a.source] = by_source.get(a.source, 0) + 1
    for name, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {n:3d}  {name}")

    for fail in col.failures:
        print(f"   !   取得失敗: {fail.source} -> {fail.error}")

    if args.print_summary:
        print("\n--- 最新10件 ---")
        for a in col.articles[:10]:
            print(f"  [{a.source}] {a.title}")


if __name__ == "__main__":
    main()
