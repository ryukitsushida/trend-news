"""エントリポイント: 収集 -> 要約(Bedrock) -> JSON保存 -> サイト生成 を実行する。"""

from __future__ import annotations

import argparse
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.collect import collect_all
from scripts.render import render_site, save_digest
from scripts.summarize import DigestResult, dry_run_digest, summarize_all

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "sources.yaml"
PUBLIC_DIR = BASE_DIR / "public"
JST = ZoneInfo("Asia/Tokyo")


def digest_result_to_dict(r: DigestResult, article_count: int) -> dict:
    return {
        "id": r.category_id,
        "label": r.label,
        "overview": r.overview,
        "topics": r.topics,
        "quick_hits": r.quick_hits,
        "ok": r.ok,
        "error": r.error,
        "article_count": article_count,
        "dropped_topics": r.dropped_topics,
        "truncated": r.truncated,
    }


def run(dry_run: bool = False, model_id: str | None = None, region: str | None = None) -> dict:
    now_jst = datetime.now(JST)
    categories = collect_all(CONFIG_PATH)

    if dry_run:
        results = [dry_run_digest(cat) for cat in categories]
    else:
        results = summarize_all(categories, model_id=model_id, region=region)

    collection_failures = []
    for cat in categories:
        for f in cat.failures:
            collection_failures.append(
                {
                    "category_id": f.category_id,
                    "source": f.source,
                    "url": f.url,
                    "error": f.error,
                }
            )

    article_counts = {cat.id: len(cat.articles) for cat in categories}

    digest = {
        "date": now_jst.strftime("%Y-%m-%d"),
        "generated_at": now_jst.strftime("%Y-%m-%d %H:%M JST"),
        "categories": [
            digest_result_to_dict(r, article_counts.get(r.category_id, 0)) for r in results
        ],
        "collection_failures": collection_failures,
    }

    save_digest(digest)
    render_site(digest, PUBLIC_DIR)
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description="トレンドダイジェスト生成")
    parser.add_argument(
        "--dry-run", action="store_true", help="Bedrockを呼ばず固定ダミー要約でサイトを生成する"
    )
    parser.add_argument("--model-id", default=os.environ.get("BEDROCK_MODEL_ID"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION"))
    args = parser.parse_args()

    digest = run(dry_run=args.dry_run, model_id=args.model_id, region=args.region)

    total_topics = sum(len(c["topics"]) for c in digest["categories"])
    print(f"生成完了: {digest['date']} / トピック合計 {total_topics} 件")

    for c in digest["categories"]:
        print(
            f"  {c['label']}: 記事 {c['article_count']} 件 -> トピック {len(c['topics'])} 件"
            + (f" / 除去 {c['dropped_topics']} 件" if c.get("dropped_topics") else "")
            + (" / 出力が途中で切れました" if c.get("truncated") else "")
        )

    failed = [c["label"] for c in digest["categories"] if not c["ok"]]
    if failed:
        print(f"生成に失敗したカテゴリ: {', '.join(failed)}")
        for c in digest["categories"]:
            if not c["ok"]:
                print(f"  {c['label']}: {c['error']}")
    if digest["collection_failures"]:
        print(f"取得失敗フィード: {len(digest['collection_failures'])} 件")


if __name__ == "__main__":
    main()
