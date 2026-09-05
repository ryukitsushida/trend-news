"""Bedrock への接続確認だけを行うスモークテスト。

フル実行(4カテゴリ)の前に、認証・モデルアクセス・tool use が通るかを
最小のリクエスト1回で確かめる。初回セットアップの切り分け用。

使い方:
    AWS_PROFILE=terraform-admin AWS_REGION=ap-northeast-1 python -m scripts.check_bedrock
"""

from __future__ import annotations

import os
import sys

from scripts.summarize import DEFAULT_MODEL_ID, DEFAULT_REGION

PING_TOOL = {
    "name": "report",
    "description": "動作確認の結果を返す。",
    "input_schema": {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}, "message": {"type": "string"}},
        "required": ["ok", "message"],
    },
}


def main() -> int:
    region = os.environ.get("AWS_REGION", DEFAULT_REGION)
    model_id = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
    print(f"リージョン: {region}")
    print(f"モデル    : {model_id}")
    print(f"プロファイル: {os.environ.get('AWS_PROFILE', '(既定の認証チェーン)')}")

    try:
        from anthropic import AnthropicBedrock
    except ImportError:
        print("\n失敗: anthropic[bedrock] が入っていません -> pip install -r requirements.txt")
        return 1

    client = AnthropicBedrock(aws_region=region)
    print(f"エンドポイント: {client.base_url}\n")

    try:
        res = client.messages.create(
            model=model_id,
            max_tokens=256,
            tools=[PING_TOOL],
            tool_choice={"type": "tool", "name": "report"},
            messages=[{"role": "user", "content": "接続確認です。okをtrue、messageに『疎通OK』と入れてreportツールを呼んでください。"}],
        )
    except Exception as exc:  # noqa: BLE001 - 原因を人間が読める形で出すのが目的
        print(f"失敗: {type(exc).__name__}: {exc}\n")
        text = str(exc)
        if "AccessDenied" in text or "not authorized" in text:
            print("→ IAMロールに bedrock:InvokeModel が付いているか確認してください。")
        elif "ExpiredToken" in text or "security token" in text:
            print("→ AWS認証が切れています。aws sso login --profile <プロファイル名> を実行してください。")
        elif "ValidationException" in text or "model" in text.lower():
            print(f"→ モデルID({model_id})とリージョン({region})の組み合わせ、")
            print("   およびBedrockコンソールでのモデルアクセス有効化を確認してください。")
        return 1

    tool_use = next((b for b in res.content if b.type == "tool_use"), None)
    print(f"stop_reason : {res.stop_reason}")
    print(f"入力トークン : {res.usage.input_tokens} / 出力トークン: {res.usage.output_tokens}")
    if tool_use is None:
        print("\n失敗: tool_use ブロックが返りませんでした(tool_choice が効いていない)")
        return 1
    print(f"tool_use    : {tool_use.input}")
    print("\n成功: 認証・モデルアクセス・tool use すべて疎通しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
