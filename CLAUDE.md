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

`build.py` が `collect → classify → summarize → highlights → render` を順に呼ぶ。

**カテゴリは記事の「内容」で決まる。** 配信元では決まらない。はてブや Hacker News の
ような話題が横断するフィードは1つのプールに集められ、分類パスで振り分けられる。
`config/sources.yaml` で `category:` を指定したフィード(JPCERT→security など)だけは
分類をスキップして直接割り当てる(確実かつ安価)。

**collect.py** — 全フィードを1プールに集約。RSSとJSON APIの2経路がある。

人気度(コミュニティでの注目度)を取れるソースは限られる。RSSで返すのは
はてブ(`hatena_bookmarkcount`)と Hacker News(summary内の `Points: N`)だけ。
Qiita / Zenn / Lobsters は公開JSON APIがLGTM・いいね・スコアを返すので、
`json_sources` として別経路で取得し、新着順ではなく**人気順**に採用する。
アダプタは `JSON_ADAPTERS` に登録する。人気度は `Article.popularity` に入り、
プロンプトで「注目度」としてClaudeに渡される。

Hacker News の RSS summary は「Article URL / Comments URL / Points」の定型文
だけで内容を含まないため、スニペットからは落としている(トークンの無駄)。

`slow: true` のフィード(公式ブログ・
注意喚起など低頻度)は収集ウィンドウを広げる(`slow_window_hours`)。代わりに
`exclude_urls` で過去に掲載済みのURLを除くため、同じ記事が何日も出続けない。
1フィードの失敗は握りつぶして続行し、`Collection.failures` に積む。

**summarize.py** — 3パス構成。
1. `classify_articles` — 未確定の記事をカテゴリへ振り分け(1回)
2. `summarize_category` — カテゴリごとにトピックと短報を生成(カテゴリ数だけ)
3. `pick_highlights` — 生成済みトピックから「今日の5選」を選ぶ(1回)

3を元記事ではなく生成済みトピックから選ぶのは、全カテゴリを俯瞰した編集判断が
できるうえ入力が小さいため。参照IDが実在しない場合は捨て、全滅したら重要度順の
機械的フォールバックに落ちる。

**render.py** — 毎回 `data/digests/*.json` を全部読み、過去号も含めて全ページを再生成する。
テンプレートを変えると過去号にも反映される。壊れたJSONは警告を出して読み飛ばす。

生成結果は `data/digests/YYYY-MM-DD.json` にコミットして履歴を保持する。これは
アーカイブ表示だけでなく、掲載済みURLの除外にも使われるので消さないこと。
`public/` はビルド成果物で gitignore 対象。

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

### フィードはHTTP 200でも死んでいることがある

`www3.nhk.or.jp` の RSS は 200 を返すが更新が1か月止まっていた(現行は `www.nhk.or.jp`)。
フィードを追加・変更したら、ステータスコードではなく**最新記事の日時**を確認すること。
`config/sources.yaml` の全フィードを監査するには、各フィードの最新 `published` と
現在時刻の差を出せばよい。

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
