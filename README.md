# Amazon 買い物 MCP サーバー（日本版）

Amazon.co.jp の商品を検索したり、商品ページの詳細を取り出したりするための MCP サーバーです。
Claude などの AI 助手から、自然な言葉で「Amazon でこれを探して」と頼めるようになります。

ログインもキーの登録も不要で、公開されている商品ページの情報を読み取って返します。

> 本家（英語・Amazon.com 向け）の [r123singh/amazon-mcp-server](https://github.com/r123singh/amazon-mcp-server) を、
> 日本の Amazon.co.jp（日本語・円表示）向けに作り直したものです。

## できること

- **商品を探す** — キーワードで検索して、上位の商品名・価格（円）・評価・商品リンクを一覧で返します。
- **商品の詳細を見る** — 商品ページの URL を渡すと、名前・価格・評価・レビュー数・在庫状況・説明を取り出します。

## 本家との違い（日本向けの変更点）

- 接続先を Amazon.co.jp に変更
- 価格を円（￥）表示に変更
- 送信情報を日本語ページ優先（`Accept-Language: ja-JP`）に変更
- 検索結果のリンクを、広告用の長いリンクではなく `/dp/商品番号` のきれいなリンクに変更
- **ボット判定（自動アクセス拒否）対策**を追加
  - 先にトップページを開いて手続き用の記録（Cookie）を受け取ってから本番の読み取りを行う
  - 拒否画面（確認画面）に当たったら、少し間を空けてやり直す（最大 4 回）
  - それでも駄目なときは、空の結果ではなく「今は拒否されている」旨をはっきり返す

## 準備するもの

- Python 3.10 以上

## 導入手順

1. このリポジトリを取得します。

   ```bash
   git clone https://github.com/kakuteki/amazon-mcp-server.git
   cd amazon-mcp-server
   ```

2. 専用の Python 環境を作って、必要な部品を入れます。

   ```bash
   python -m venv .venv
   # Windows の場合
   .venv\Scripts\activate
   # macOS / Linux の場合
   source .venv/bin/activate

   pip install -r requirements.txt
   ```

3. AI 助手側に、このサーバーの場所を教えます（設定ファイルの例）。

   ```json
   {
     "mcpServers": {
       "amazon-jp": {
         "command": "（このフォルダの絶対パス）/.venv/Scripts/python.exe",
         "args": ["（このフォルダの絶対パス）/server.py"]
       }
     }
   }
   ```

   Claude Code を使っている場合は、次のコマンドでも登録できます。

   ```bash
   claude mcp add amazon-jp -- "（絶対パス）/.venv/Scripts/python.exe" "（絶対パス）/server.py"
   ```

4. AI 助手を再起動すると、道具（ツール）が使えるようになります。

## 使い方の例

- 「Amazon で『ワイヤレスイヤホン』を 3 件探して」
- 「この商品の詳細を教えて: https://www.amazon.co.jp/dp/XXXXXXXXXX 」

## 用意されている道具

- `search_products(query, max_results)` — キーワードで商品を検索します。
- `scrape_product(product_url)` — 商品ページの URL から詳細を取り出します。

## 注意点

- これは Amazon 公式のものではありません。公開ページを読み取る方式のため、Amazon 側の作りが変わると動かなくなることがあります。
- Amazon には自動アクセスを拒否する仕組みがあり、短時間に何度も呼ぶと一時的に弾かれます。特に商品詳細ページは弾かれやすいので、少し時間を置いて試してください。
- 個人的な調べ物の範囲で、常識的な回数で使ってください。

## ライセンス

MIT ライセンス。本家 [r123singh/amazon-mcp-server](https://github.com/r123singh/amazon-mcp-server) に基づく派生物です。
