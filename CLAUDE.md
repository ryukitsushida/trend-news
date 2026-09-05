# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## このプロジェクトについて

GitHub Actions が毎朝 RSS/Atom フィードを収集し、Amazon Bedrock 上の Claude に
カテゴリ単位で要約させ、GitHub Pages に静的サイトとして公開する。通勤中にスマホで
読む用途を想定した PWA。公開先は `https://ryukitsushida.github.io/trend-news/`。

## コマンド

```bash
source .venv/bin/activate

python -m scripts.collect --print-summary   # 収集のみ(AWS不要)
python -m scripts.build --dry-run           # Bedrockを呼ばずサイト生成(見た目確認用)
python -m scripts.check_bedrock             # Bedrock疎通確認(APIコール1回のみ)
python -m scripts.build                     # 本番と同じフル生成
python -m http.server -d public 8000        # 生成物の確認

python -m pyflakes scripts/*.py             # 静的チェック
```

テストフレームワークは導入していない。検証は上記の dry-run とブラウザ確認で行う。

`--dry-run` は Bedrock を呼ばず固定のダミー要約を返す(`summarize.dry_run_digest`)。
テンプレートや CSS を変更したときはこれで確認する。

## アーキテクチャ

`build.py` が `collect → summarize → save → render` を順に呼ぶ。

**collect.py** — 20フィードを並列取得。`window_hours`(既定30)以内の記事のみ採用し、
正規化URL/正規化タイトルで重複排除する。重複排除は**カテゴリ内でのみ**行うため、
同じ記事が別カテゴリに出ることはある。1フィードの失敗は握りつぶして続行し、
`CategoryResult.failures` に積んでページ下部とログに出す。

**summarize.py** — カテゴリごとに1リクエスト(計4回)。1カテゴリが失敗しても他は生きる。

**render.py** — 毎回 `data/digests/*.json` を全部読み、過去号も含めて全ページを再生成する。
テンプレートを変えると過去号にも反映される。壊れたJSONは警告を出して読み飛ばす。

生成結果は `data/digests/YYYY-MM-DD.json` にコミットして履歴を保持する。
`public/` はビルド成果物で gitignore 対象(Pagesにデプロイされるだけ)。

## 落とし穴(いずれも実際に踏んだもの)

### Bedrock は web検索と構造化出力に非対応

サーバーサイドツール(web_search / web_fetch / code_execution)と
`output_config.format` が使えない。だから:

- **記事本文は Python 側で取得して渡す**(AIに直接サイトを読ませることはできない)
- **JSON は tool use + `tool_choice` の強制で取り出す**。`strict: true` も使えない

### LLM出力は必ずサニタイズしてから描画する

`_sanitize_and_filter()` を通さずにテンプレートへ渡さないこと。実際に、
`importance` が `3` ではなく `"3"` で返ってビルド全体がクラッシュした。
型の正規化に加えて、**sources/quick_hits のURLを入力記事のURL集合と突合し、
一致しないものを除去する**。これは捏造リンクの公開防止と、`javascript:` などの
不正スキーム遮断を兼ねている。

### OIDC の sub クレームには数値IDが入る

GitHub が送る sub は `repo:<owner>@<ownerID>/<repo>@<repoID>:ref:refs/heads/main`。
記事等でよく見る `repo:<owner>/<repo>:ref:...` ではない。IAMの文字列条件は完全一致
なので、IDを省くと `AssumeRoleWithWebIdentity` が AccessDenied になる。
**AWSは理由を伏せて一律 `Not authorized` しか返さない**ため、原因究明が難しい。

正しい値の取得:
```bash
gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix
```
失敗理由の確認は CloudTrail で `AssumeRoleWithWebIdentity` を検索し、
`userIdentity.userName` に載っている実際の sub を見る。

### モデルの初回有効化は人がやる必要がある

Bedrock の「モデルアクセス」ページは廃止され、初回呼び出しで自動有効化される方式に
変わった。ただし自動有効化には AWS Marketplace 権限が必要で、CI用ロールは最小権限
(`bedrock:InvokeModel` のみ)しか持たないため**自力で有効化できない**。
権限を持つ人がコンソールのプレイグラウンドで1回呼ぶ必要がある。

### GitHub Pages はサブパス配信

`/trend-news/` 配下で配信されるため、**アセット参照はすべて相対パス**にすること。
ルート絶対パス(`/static/...`)は404になる。テンプレートは `root` 変数で解決しており、
トップは `""`、アーカイブ配下は `"../"` を渡している。

manifest と sw.js は全ページをスコープに入れるためサイトルートにも複製している
(`render._copy_static`)。Service Worker の `respondWith` に `undefined` を渡すと
TypeError になるので、オフライン時は必ず Response を返すこと。

## 運用上の制約

- cron は `0 20 * * *` UTC = 05:00 JST。日付は JST 基準で扱う(`build.py` の `ZoneInfo`)
- モデルIDは環境変数 `BEDROCK_MODEL_ID` で差し替えられる。品質を上げたいときは
  `global.anthropic.claude-sonnet-4-5-20250929-v1:0` などに変更する
- ワークフローの `AWS_ROLE_ARN` は Secret / Variable どちらでも読めるようにしてある
- パブリックリポジトリの cron は60日間の無活動で自動停止する

## 作業の進め方

**コミットは勝手に実行しない。** 変更内容の差分を提示してユーザーのレビューを待ち、
明示的に指示されたときだけコミット・push する。
