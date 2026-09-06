"""エントリポイント: 収集 -> 分類 -> 要約 -> 5選 -> サイト生成 を実行する。"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.collect import Article, collect_all, load_config, normalize_url
from scripts.render import load_all_digests, render_site, save_digest
from scripts.summarize import (
    CategoryDigest,
    classify_articles,
    dry_run_digest,
    get_client,
    pick_highlights,
    summarize_category,
)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "sources.yaml"
PUBLIC_DIR = BASE_DIR / "public"
JST = ZoneInfo("Asia/Tokyo")


def published_urls() -> set[str]:
    """過去のダイジェストで既に掲載したURL。

    更新頻度の低いフィードは収集ウィンドウを広げているため、これを除外しないと
    同じ記事が何日も出続けてしまう。
    """
    urls: set[str] = set()
    for digest in load_all_digests():
        for cat in digest.get("categories", []):
            for topic in cat.get("topics", []):
                for src in topic.get("sources", []):
                    if isinstance(src, dict) and src.get("url"):
                        urls.add(normalize_url(src["url"]))
            for hit in cat.get("quick_hits", []):
                if isinstance(hit, dict) and hit.get("url"):
                    urls.add(normalize_url(hit["url"]))
    return urls


def group_by_category(
    articles: list[Article], categories: list[dict], assigned: dict[int, str]
) -> dict[str, list[Article]]:
    grouped: dict[str, list[Article]] = {c["id"]: [] for c in categories}
    for i, article in enumerate(articles):
        cid = article.category_id or assigned.get(i)
        if cid in grouped:
            grouped[cid].append(article)
    return grouped


def digest_to_dict(d: CategoryDigest) -> dict:
    return {
        "id": d.id,
        "label": d.label,
        "overview": d.overview,
        "topics": d.topics,
        "quick_hits": d.quick_hits,
        "ok": d.ok,
        "error": d.error,
        "article_count": d.article_count,
        "dropped_topics": d.dropped_topics,
        "truncated": d.truncated,
    }


def run(dry_run: bool = False, model_id: str | None = None, region: str | None = None) -> dict:
    now_jst = datetime.now(JST)
    config = load_config(CONFIG_PATH)
    categories = config["categories"]
    topic_count = config.get("topics_per_category", 5)
    quick_hit_count = config.get("quick_hits_per_category", 5)
    highlight_count = config.get("highlight_count", 5)

    collection = collect_all(CONFIG_PATH, exclude_urls=published_urls())
    print(f"収集: {len(collection.articles)} 件 / 取得失敗 {len(collection.failures)} フィード")

    client = None if dry_run else get_client(region)
    model = model_id  # None ならパスごとの環境変数で解決される

    # --- 分類 ---
    assigned: dict[int, str] = {}
    classify_error = None
    if dry_run:
        # カテゴリ固定でない記事は順番に配って見た目を確認できるようにする
        ids = [c["id"] for c in categories]
        unfixed = [i for i, a in enumerate(collection.articles) if not a.category_id]
        assigned = {idx: ids[n % len(ids)] for n, idx in enumerate(unfixed)}
    else:
        try:
            assigned = classify_articles(collection.articles, categories, client, model)
        except Exception as exc:  # noqa: BLE001 - 分類が落ちても固定カテゴリ分は出す
            print(f"[classify] 分類に失敗: {exc}")
            classify_error = str(exc)

    grouped = group_by_category(collection.articles, categories, assigned)
    unclassified = len(collection.articles) - sum(len(v) for v in grouped.values())

    # --- カテゴリ別要約 ---
    digests: list[CategoryDigest] = []
    for category in categories:
        articles = grouped[category["id"]]
        if dry_run:
            digests.append(dry_run_digest(category, articles, topic_count))
        else:
            digests.append(
                summarize_category(
                    category, articles, client, model, topic_count, quick_hit_count
                )
            )

    # --- 今日の5選 ---
    if dry_run:
        lead = "(dry-run) 本日の注目トピックです。"
        highlights = [
            {
                "category_id": d.id,
                "category_label": d.label,
                "reason": "(dry-run)",
                "topic": d.topics[0],
            }
            for d in digests
            if d.topics
        ][:highlight_count]
    else:
        lead, highlights = pick_highlights(digests, client, model, highlight_count)

    digest = {
        "date": now_jst.strftime("%Y-%m-%d"),
        "generated_at": now_jst.strftime("%Y-%m-%d %H:%M JST"),
        "lead": lead,
        "highlights": highlights,
        "categories": [digest_to_dict(d) for d in digests],
        "collection_failures": [
            {"source": f.source, "url": f.url, "error": f.error} for f in collection.failures
        ],
        "stats": {
            "collected": len(collection.articles),
            "unclassified": unclassified,
            "classify_error": classify_error,
        },
    }

    save_digest(digest)
    render_site(digest, PUBLIC_DIR)
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description="トレンドダイジェスト生成")
    parser.add_argument(
        "--dry-run", action="store_true", help="Bedrockを呼ばず固定ダミー要約でサイトを生成する"
    )
    parser.add_argument(
        "--model-id",
        default=None,
        help="全パスのモデルを明示指定する。未指定なら BEDROCK_{CLASSIFY,DIGEST,HIGHLIGHT}_MODEL_ID "
        "→ BEDROCK_MODEL_ID の順に解決される。",
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"))
    args = parser.parse_args()

    digest = run(dry_run=args.dry_run, model_id=args.model_id, region=args.region)

    total = sum(len(c["topics"]) for c in digest["categories"])
    print(f"\n生成完了: {digest['date']} / 5選 {len(digest['highlights'])}件 / トピック計 {total}件")
    for c in digest["categories"]:
        print(
            f"  {c['label']}: 記事{c['article_count']}件 → トピック{len(c['topics'])}件"
            f" / 短報{len(c['quick_hits'])}件"
            + (f" / 除去{c['dropped_topics']}件" if c["dropped_topics"] else "")
            + (" / 出力が途中で切れました" if c["truncated"] else "")
            + ("" if c["ok"] else f"  [失敗: {c['error']}]")
        )
    stats = digest["stats"]
    if stats["unclassified"]:
        print(f"  未分類で除外: {stats['unclassified']}件")
    if digest["collection_failures"]:
        print(f"  取得失敗フィード: {len(digest['collection_failures'])}件")


if __name__ == "__main__":
    main()
