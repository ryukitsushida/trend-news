# Trend News

GitHub Actions が毎朝、RSS/Atomフィードで指定サイトの記事を収集し、Amazon Bedrock 上の
Claude(Haiku 4.5)にカテゴリ単位で要約させて、GitHub Pages に静的サイトとして公開します。
通勤中にスマホで読む用途を想定し、PWA(オフライン閲覧)対応です。

- 公開URL: `https://ryukitsushida.github.io/trend-news/`(Pages有効化後)
- 構成: 冒頭に「今日の5選」、以下6カテゴリ(AI・LLM / セキュリティ / クラウド・インフラ /
  開発・エンジニアリング / プロダクト・ビジネス / 一般ニュース)を各5トピック+短報5件
- モデル: `global.anthropic.claude-haiku-4-5-20251001-v1:0`(Bedrock InvokeModel, ap-northeast-1)

## ローカル開発

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 1) 収集だけ試す(AWS不要)
python -m scripts.collect --print-summary

# 2) Bedrockを呼ばずにサイト生成を試す(見た目確認用)
python -m scripts.build --dry-run
python -m http.server -d public 8000   # http://localhost:8000

# 3) Bedrockへの疎通確認だけを行う(APIコール1回だけ。初回の切り分け用)
aws sso login --profile terraform-admin
AWS_PROFILE=terraform-admin AWS_REGION=ap-northeast-1 python -m scripts.check_bedrock

# 4) 実際にBedrockを呼んで生成する(4カテゴリ分=4コール)
AWS_PROFILE=terraform-admin AWS_REGION=ap-northeast-1 python -m scripts.build
```

`check_bedrock` は認証・モデルアクセス・tool use のどこで失敗したかを切り分けて表示します。
`ExpiredToken` が出たら `aws sso login --profile terraform-admin` を実行してください
(SSOトークンは数時間で切れます)。

PWAアイコンは `scripts/make_icons.py` で生成済み(`static/icon-192.png` / `icon-512.png`)。
デザインを変えたい場合のみ `pip install pillow && python scripts/make_icons.py` を再実行してください。

---

## セットアップ手順(初回のみ・手動)

### A. AWS側: モデルの初回有効化

Bedrockの「モデルアクセス」ページは廃止され、**サーバーレス基盤モデルは初回呼び出し時に
自動で有効化される**方式に変わりました。ただし自動有効化には AWS Marketplace の権限が必要で、
本プロジェクトのGitHub Actionsロールは最小権限(`bedrock:InvokeModel` のみ)しか持たないため、
**権限を持つ人が1回だけ手で呼んでアカウント全体を有効化する**必要があります。

1. Bedrockコンソール(**ap-northeast-1**)→ 「モデルカタログ」→ **Claude Haiku 4.5**
2. 「プレイグラウンドで開く」→ 適当なメッセージを送信

初回利用時に利用用途(use case)の入力を求められた場合は、そこで入力してください。
会社サイトが無い個人開発者は、GitHubプロフィールのURLで代用できます。

一度成功すればアカウント全体で有効になり、以後はActionsのロールからも呼べます。

### B. AWS側: GitHub Actions用のOIDCロールを作る

GitHub Actionsに長期のAWSキーを持たせず、OIDC(短命トークン)でロールを引き受けさせます。

**1. IDプロバイダを登録**(まだ無ければ)

IAM > IDプロバイダ > プロバイダを追加

- プロバイダのタイプ: OpenID Connect
- プロバイダのURL: `https://token.actions.githubusercontent.com`
- 対象者(Audience): `sts.amazonaws.com`

**2. IAMロールを作成**

信頼ポリシー(このリポジトリの `main` ブランチからの実行のみ許可):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "GitHubActionsOIDC",
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::<AWSアカウントID>:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
          "token.actions.githubusercontent.com:sub": "repo:ryukitsushida@181846498/trend-news@1353737192:ref:refs/heads/main"
        }
      }
    }
  ]
}
```

> **`sub` に数値IDが入る点に注意。** GitHubが実際に送るsubは
> `repo:<owner>@<ownerID>/<repo>@<repoID>:ref:refs/heads/main` という形式で、
> 多くの記事にある `repo:<owner>/<repo>:...` ではありません。IAMの文字列条件は
> 完全一致なので、IDを省くと `Not authorized to perform sts:AssumeRoleWithWebIdentity`
> で失敗します。AWSは理由を伏せてこのエラーしか返さないため、原因の特定が難しい点に注意。
>
> 自分のリポジトリの正しい値は次で取得できます:
>
> ```bash
> gh api repos/<owner>/<repo>/actions/oidc/customization/sub --jq .sub_claim_prefix
> ```
>
> 失敗の実際の理由はCloudTrailで確認できます(`AssumeRoleWithWebIdentity` を検索し、
> `userIdentity.userName` に載っている実際のsubを見る)。

権限ポリシー(Bedrockのモデル呼び出しのみ許可):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeClaudeOnBedrock",
      "Effect": "Allow",
      "Action": ["bedrock:InvokeModel"],
      "Resource": "*"
    }
  ]
}
```

ロール名は任意(例: `github-actions-trend-news`)。作成後、**ロールのARN** を控える。

> `<AWSアカウントID>` は `aws sts get-caller-identity` で確認できます(ローカルのAWS認証が
> 現在すべて期限切れのため、`aws sso login --profile <profile>` 等で再認証してから実行してください)。

### C. GitHub側: ロールARNを登録

リポジトリ > Settings > Secrets and variables > Actions

| Name | Value |
|---|---|
| `AWS_ROLE_ARN` | 上で作成したIAMロールのARN |

Secret / Variable のどちらに登録してもワークフローは動きます
(`${{ secrets.AWS_ROLE_ARN || vars.AWS_ROLE_ARN }}` で両対応)。
ロールARN自体は認証情報ではなく、信頼ポリシーでこのリポジトリのmainブランチに
限定されているため、Variableでも実害はありません。

### D. GitHub側: Pagesを有効化

リポジトリ > Settings > Pages > Build and deployment > Source を **GitHub Actions** に設定。

### E. 初回実行

Actions タブ > "Build daily digest" > Run workflow(`workflow_dispatch`)で手動実行し、
`build` → `deploy` の両ジョブが成功することを確認する。以後は毎日 05:00 JST 頃に自動実行される。

---

## 構成

```
config/sources.yaml     収集対象フィード(カテゴリ定義)
scripts/collect.py       RSS収集・正規化・重複排除
scripts/summarize.py     Bedrock呼び出し(分類→カテゴリ別要約→今日の5選 の3パス)
scripts/render.py        Jinja2テンプレート -> public/ に静的サイト出力
scripts/build.py         上記をまとめて実行するエントリポイント
scripts/check_bedrock.py Bedrockへの疎通確認のみ行うスモークテスト
data/digests/*.json      生成結果の履歴(コミットして保持)
public/                  ビルド成果物(gitignore対象、Pagesにデプロイされるのみ)
```

## 運用上の注意

**パブリックリポジトリのcronは60日間の無活動で自動停止します。** 本ワークフローは毎日
`data/digests` へコミットするため通常は動き続けますが、もし定期実行が止まったら
Actionsタブから再有効化してください。

**オフライン閲覧は「事前に一度オンラインで開いていること」が前提です。** Service Workerは
訪問したページをキャッシュするため、電車に乗る前に一度開いておけば圏外でも読めます。
一度も開かずに圏外で開くと、前回キャッシュした号(前日分)が表示されます。

## コスト目安

Haiku 4.5(Bedrock)で1日4カテゴリ分の要約を生成した場合、概算で **月あたり数百円程度**。
