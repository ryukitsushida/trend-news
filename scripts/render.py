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


def domain_of(url: str) -> str:
    """URLからドメイン名を取り出す。source を持たない古いアーカイブ用のフォールバック。"""
    from urllib.parse import urlsplit

    host = urlsplit(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def get_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["domain"] = domain_of
    return env


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


def _copy_digest_data(output_dir: Path, dates: list[str]) -> None:
    """公開するJSONは、ページを生成した日付の分だけに絞る。"""
    data_out = output_dir / "data" / "digests"
    if data_out.exists():
        shutil.rmtree(data_out)
    data_out.mkdir(parents=True, exist_ok=True)
    for date in dates:
        src = DIGESTS_DIR / f"{date}.json"
        if src.exists():
            shutil.copy(src, data_out / src.name)


def _date_neighbours(dates: list[str]) -> dict[str, tuple[str | None, str | None]]:
    """各日付の (前の日, 次の日) を返す。dates は新しい順なので next は1つ前の要素。"""
    return {
        date: (
            dates[i + 1] if i + 1 < len(dates) else None,
            dates[i - 1] if i > 0 else None,
        )
        for i, date in enumerate(dates)
    }


def _render_archive_index(tmpl, digests: list[dict], archive_dir: Path) -> None:
    entries = [
        {
            "date": d["date"],
            "generated_at": d.get("generated_at"),
            "topic_count": sum(len(c.get("topics", [])) for c in d.get("categories", [])),
        }
        for d in digests
    ]
    (archive_dir / "index.html").write_text(
        tmpl.render(entries=entries, root="../"), encoding="utf-8"
    )


def render_site(
    latest_digest: dict,
    output_dir: Path,
    archive_days: int | None = None,
    past_digests: list[dict] | None = None,
) -> None:
    """サイトを生成する。

    past_digests を渡すと過去号の再読み込みを省略できる(build.py は
    掲載済みURLの抽出で既に全件読んでいるため、そのまま使い回す)。
    """
    env = get_env()
    output_dir.mkdir(parents=True, exist_ok=True)

    all_digests = load_all_digests() if past_digests is None else list(past_digests)
    # 直近ビルド分を最新として先頭に反映(ファイル書き込み前でも一覧に含める)
    all_digests = [d for d in all_digests if d["date"] != latest_digest["date"]]
    all_digests.insert(0, latest_digest)
    all_digests.sort(key=lambda d: d["date"], reverse=True)
    # 公開するのは直近 archive_days 分だけ。元のJSONは data/digests に残る。
    if archive_days:
        all_digests = all_digests[:archive_days]

    digest_tmpl = env.get_template("digest.html")
    archive_index_tmpl = env.get_template("archive_index.html")

    dates = [d["date"] for d in all_digests]
    neighbours = _date_neighbours(dates)

    # index.html = 最新号。日付リンクはアーカイブ配下を指すので接頭辞を付ける。
    prev_date, next_date = neighbours.get(latest_digest["date"], (None, None))
    (output_dir / "index.html").write_text(
        digest_tmpl.render(
            digest=latest_digest,
            is_latest=True,
            root="",
            prev_date=f"archive/{prev_date}" if prev_date else None,
            next_date=f"archive/{next_date}" if next_date else None,
        ),
        encoding="utf-8",
    )

    # archive/YYYY-MM-DD.html = 各号(毎回全日程を再生成し、テンプレ更新を過去号にも反映)
    # 保持日数を縮めたときに古いページが残らないよう、毎回作り直す
    archive_dir = output_dir / "archive"
    if archive_dir.exists():
        shutil.rmtree(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    for digest in all_digests:
        prev_date, next_date = neighbours.get(digest["date"], (None, None))
        html = digest_tmpl.render(
            digest=digest,
            is_latest=digest["date"] == latest_digest["date"],
            root="../",
            prev_date=prev_date,
            next_date=next_date,
        )
        (archive_dir / f"{digest['date']}.html").write_text(html, encoding="utf-8")

    _render_archive_index(archive_index_tmpl, all_digests, archive_dir)

    _copy_static(output_dir)
    _copy_digest_data(output_dir, dates)
