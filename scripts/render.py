"""生成済みダイジェストJSONから静的サイト(public/)を組み立てる。

GitHub Pages はプロジェクトサイト(サブパス配信)になるため、テンプレート内の
アセット参照はすべて相対パス(root変数経由)で解決する。ルート絶対パスは使わない。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
DIGESTS_DIR = BASE_DIR / "data" / "digests"


def get_env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def load_all_digests() -> list[dict]:
    """data/digests/*.json を日付降順で読み込む。

    過去の1ファイルが壊れているだけで以降のビルドが全滅しないよう、
    読めないファイルは警告を出して読み飛ばす。
    """
    digests = []
    if not DIGESTS_DIR.exists():
        return digests
    for path in sorted(DIGESTS_DIR.glob("*.json"), reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"警告: アーカイブを読み飛ばしました {path.name}: {exc}")
            continue
        if isinstance(data, dict) and data.get("date"):
            digests.append(data)
        else:
            print(f"警告: dateを持たないアーカイブを読み飛ばしました {path.name}")
    return digests


def save_digest(digest: dict, digests_dir: Path = DIGESTS_DIR) -> Path:
    digests_dir.mkdir(parents=True, exist_ok=True)
    path = digests_dir / f"{digest['date']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)
    return path


def _copy_static(output_dir: Path) -> None:
    static_out = output_dir / "static"
    if static_out.exists():
        shutil.rmtree(static_out)
    shutil.copytree(STATIC_DIR, static_out)

    # PWA関連はスコープを全ページに広げるためサイトルートへ複製する
    for name in ("manifest.webmanifest", "sw.js", "icon-192.png", "icon-512.png"):
        src = STATIC_DIR / name
        if src.exists():
            shutil.copy(src, output_dir / name)


def _copy_digest_data(output_dir: Path) -> None:
    data_out = output_dir / "data" / "digests"
    data_out.mkdir(parents=True, exist_ok=True)
    if DIGESTS_DIR.exists():
        for path in DIGESTS_DIR.glob("*.json"):
            shutil.copy(path, data_out / path.name)


def render_site(latest_digest: dict, output_dir: Path) -> None:
    env = get_env()
    output_dir.mkdir(parents=True, exist_ok=True)

    all_digests = load_all_digests()
    # 直近ビルド分を最新として先頭に反映(ファイル書き込み前でも一覧に含める)
    all_digests = [d for d in all_digests if d["date"] != latest_digest["date"]]
    all_digests.insert(0, latest_digest)
    all_digests.sort(key=lambda d: d["date"], reverse=True)

    digest_tmpl = env.get_template("digest.html")
    archive_index_tmpl = env.get_template("archive_index.html")

    # index.html = 最新号
    (output_dir / "index.html").write_text(
        digest_tmpl.render(digest=latest_digest, is_latest=True, root=""),
        encoding="utf-8",
    )

    # archive/YYYY-MM-DD.html = 各号(毎回全日程を再生成し、テンプレ更新を過去号にも反映)
    archive_dir = output_dir / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    for digest in all_digests:
        is_latest = digest["date"] == latest_digest["date"]
        html = digest_tmpl.render(digest=digest, is_latest=is_latest, root="../")
        (archive_dir / f"{digest['date']}.html").write_text(html, encoding="utf-8")

    # archive/index.html = 日付一覧
    archive_entries = [
        {
            "date": d["date"],
            "generated_at": d.get("generated_at"),
            "topic_count": sum(len(c.get("topics", [])) for c in d.get("categories", [])),
        }
        for d in all_digests
    ]
    (archive_dir / "index.html").write_text(
        archive_index_tmpl.render(entries=archive_entries, root="../"),
        encoding="utf-8",
    )

    _copy_static(output_dir)
    _copy_digest_data(output_dir)
