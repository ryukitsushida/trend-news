"""生成結果が満たすべき条件(不変条件)を検証する。

画面に表示されていても内部で壊れているケースを拾うのが目的。
使い方: python -m scripts.check_invariants [data/digests/YYYY-MM-DD.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DIGESTS_DIR = BASE_DIR / "data" / "digests"

_failures: list[str] = []
_checks = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if ok:
        print(f"  OK   {name}")
    else:
        print(f"  NG   {name}" + (f"  → {detail}" if detail else ""))
        _failures.append(name)


def check_digest(digest: dict) -> None:
    cats = digest.get("categories", [])
    topics = [(c, t) for c in cats for t in c.get("topics", [])]
    config_ids = {c["id"] for c in cats}

    print(f"\n--- {digest.get('date')} の内容 ---")

    # トピックは必ず実在する記事に紐づくべき
    bad_url = [
        (c["id"], s.get("url"))
        for c, t in topics
        for s in t.get("sources", [])
        if not str(s.get("url", "")).startswith(("http://", "https://"))
    ]
    check("全ソースURLが http(s) である", not bad_url, str(bad_url[:3]))

    check("ソースが0件のトピックが無い", all(t.get("sources") for _, t in topics))
    check(
        "全トピックに見出しと要約がある",
        all(t.get("headline") and t.get("summary") for _, t in topics),
    )
    check(
        "importance が全て1〜5の整数",
        all(isinstance(t.get("importance"), int) and 1 <= t["importance"] <= 5 for _, t in topics),
    )

    # 同じ記事が複数トピックに現れると読者は重複を読むことになる
    urls = [s["url"] for _, t in topics for s in t["sources"]]
    dup = {u for u in urls if urls.count(u) > 1}
    check("同一URLが複数トピックに重複していない", not dup, f"{len(dup)}件 {list(dup)[:2]}")

    # 短報とトピックの重複も同様
    hit_urls = {h["url"] for c in cats for h in c.get("quick_hits", [])}
    overlap = hit_urls & set(urls)
    check("短報とトピックでURLが重複していない", not overlap, f"{len(overlap)}件")

    # 5選は実在するトピックを指しているべき
    highlights = digest.get("highlights", [])
    heads = {t["headline"] for _, t in topics}
    check(
        "5選が実在トピックを参照している",
        all(h.get("topic", {}).get("headline") in heads for h in highlights),
    )
    check("5選のカテゴリIDが定義済み", all(h.get("category_id") in config_ids for h in highlights))
    refs = [h.get("topic", {}).get("headline") for h in highlights]
    check("5選内で重複が無い", len(refs) == len(set(refs)))

    # 生成に失敗したカテゴリは、失敗として記録されているべき
    for c in cats:
        if not c.get("ok"):
            check(f"[{c['id']}] 失敗時にエラーが記録されている", bool(c.get("error")))

    # 記事があるのにトピックが0なのは、静かな失敗の可能性がある
    silent = [c["id"] for c in cats if c.get("ok") and c.get("article_count", 0) >= 10 and not c.get("topics")]
    check("記事十分なのにトピック0のカテゴリが無い", not silent, str(silent))

    # 地の文の < > を消してしまうタグ除去バグの再発検知
    snippets = [t.get("summary", "") for _, t in topics]
    check(
        "要約にHTMLタグが残っていない",
        not [x for x in snippets if "<" in x and ">" in x],
        str([x[:40] for x in snippets if "<" in x and ">" in x][:2]),
    )

    # CSSの必須ルールが消えていないか(文字列置換で巻き込み削除した実績あり)
    css = (BASE_DIR / "static" / "style.css").read_text(encoding="utf-8")
    for rule in ("main {", ".topic-card {", ".site-header {", ".generated-at {", ".site-footer {"):
        check(f"CSS に {rule} がある", rule in css)

    # 公開物に認証情報やアカウントIDが混ざっていないこと
    blob = json.dumps(digest, ensure_ascii=False)
    import re

    leaked = re.findall(r"arn:aws[\w-]*:[^\s\"']+|\b\d{12}\b", blob)
    check("ARN/アカウントIDが含まれていない", not leaked, str(leaked[:2]))


def check_site(public: Path) -> None:
    print("\n--- 生成サイトの整合性 ---")
    if not public.exists():
        check("public/ が生成されている", False, "先に build を実行してください")
        return

    import re

    archive = public / "archive"
    pages = {p.stem for p in archive.glob("*.html")} - {"index"}

    # アーカイブ一覧に載っている日付は、必ずページが存在するべき
    listed = set(re.findall(r'href="([0-9]{4}-[0-9]{2}-[0-9]{2})\.html"', (archive / "index.html").read_text()))
    check("一覧の全日付にページが存在する", listed <= pages, str(listed - pages))

    # 前後移動のリンク切れが無いこと
    broken = []
    for page in archive.glob("*.html"):
        html = page.read_text(encoding="utf-8")
        for target in re.findall(r'<a class="date-arrow" href="([^"]+)"', html):
            if not (archive / target).exists():
                broken.append(f"{page.name} -> {target}")
    check("日付移動リンクが全て有効", not broken, str(broken[:3]))

    # トップの前後リンクは archive/ 配下を指すべき
    idx = (public / "index.html").read_text(encoding="utf-8")
    idx_links = re.findall(r'<a class="date-arrow" href="([^"]+)"', idx)
    check(
        "トップの日付リンクが archive/ を指している",
        all(link.startswith("archive/") for link in idx_links),
        str(idx_links),
    )
    for link in idx_links:
        check(f"トップの日付リンク {link} が存在する", (public / link).exists())

    # PWAに必要なファイルがサイトルートに揃っていること
    for name in ("manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png"):
        check(f"{name} がサイトルートにある", (public / name).exists())

    # Service Worker が事前キャッシュするファイルが実在すること
    sw = (public / "sw.js").read_text(encoding="utf-8")
    shell = re.findall(r'"([^"]+)",', sw[sw.index("APP_SHELL"): sw.index("];", sw.index("APP_SHELL"))])
    missing = [f for f in shell if f not in ("./",) and not (public / f).exists()]
    check("SWの事前キャッシュ対象が全て存在する", not missing, str(missing))

    # 公開JSONとページの日付が一致すること
    json_dates = {p.stem for p in (public / "data" / "digests").glob("*.json")}
    check("公開JSONとページの日付が一致", json_dates == pages, f"json={json_dates - pages} page={pages - json_dates}")


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else max(DIGESTS_DIR.glob("*.json"))
    print(f"検証対象: {target}")
    check_digest(json.loads(target.read_text(encoding="utf-8")))
    check_site(BASE_DIR / "public")

    print(f"\n{'=' * 50}")
    if _failures:
        print(f"NG {len(_failures)}件 / 全{_checks}件")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"全{_checks}件パス")
    return 0


if __name__ == "__main__":
    sys.exit(main())
