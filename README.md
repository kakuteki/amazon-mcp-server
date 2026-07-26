# Amazon 買い物 MCP サーバー（日本版）

Amazon.co.jp の商品を検索したり、商品ページの詳細を取り出したりするための MCP サーバーです。
Claude などの AI 助手から、自然な言葉で「Amazon でこれを探して」と頼めるようになります。

ログインもキーの登録も不要で、公開されている商品ページの情報を読み取って返します。

> 本家（英語・Amazon.com 向け）の [r123singh/amazon-mcp-server](https://github.com/r123singh/amazon-mcp-server) を、
> 日本の Amazon.co.jp（日本語・円表示）向けに作り直したものです。

## できること

- **商品を探す** — キーワードで検索して、商品名・価格（円）・評価・レビュー件数・お届け日・在庫・リンクを一覧で返します。
  複数ページをまたいで探せます。並び順・価格帯・広告除外・**到着日数の上限**で絞り込めます。
- **商品の詳細を見る** — 商品ページの URL を渡すと、価格・送料・合計・お届け日・出荷元/販売元・
  輸入品かどうか・**仕様表の全項目**・**説明の箇条書き全部**・商品画像を取り出します。
- **並べて比べる** — 複数の商品をまとめて取得し、合計金額・到着日・販売元・在庫を一覧表にします。

### 買い物の判断に効く点

- **お届け日を日付として返します**（`[2026-07-29 / 2日後]`）。「3日以内に届くか」を目で数えなくて済みます。
  有料の最短便に引きずられないよう、判定は通常配送の日付で行います。
- **郵便番号を指定できます**。お届け日は住所で変わるため、指定しないと別地域の推定日を見ることになります。
- **送料込みの合計**を出します。本体価格だけで並べると、送料の高い出品を安いと誤認します。
- **出荷元・販売元・輸入品表示**を出します。「在庫あり」でも海外発送だとお届け日が表示されず、
  何週間もかかることがあります。
- **在庫僅少（残りN点）を数値で警告**します。

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

- `search_products(query, max_results, sort, min_price, max_price, hide_sponsored, pages, max_delivery_days, postal_code)`
  キーワードで検索します。`pages` を増やすと複数ページを走査します（1ページは16〜24件で、
  これは「Amazon 全部」ではありません）。`max_delivery_days=3` で到着が遅いものを除きます。
- `scrape_product(product_url, postal_code)` — 1商品の詳細をすべて取り出します。
- `compare_products(product_urls, postal_code)` — 複数商品を1回で取得し、比較表を作ります。
  1件ずつ呼ぶより往復が減り、ボット判定にも当たりにくくなります。

## 動作確認（テスト）

保存した HTML に対して読み取り処理を検証します。通信は発生しません。

```bash
.venv\Scripts\python.exe tests\test_extract.py
```

Amazon の作りが変わって読み取れなくなったときに、**空欄を正常として返すのではなく落ちる**ようにするためのものです。
Amazon 側のページ構成が実際に変わったときだけ、`tests\refresh_fixtures.py` で保存済み HTML を取り直してください
（テストを通すために取り直すと、検知したかった壊れ方を隠すことになります）。

## 注意点

- これは Amazon 公式のものではありません。公開ページを読み取る方式のため、Amazon 側の作りが変わると動かなくなることがあります。
- Amazon には自動アクセスを拒否する仕組みがあり、短時間に何度も呼ぶと一時的に弾かれます。特に商品詳細ページは弾かれやすいので、少し時間を置いて試してください。
- 個人的な調べ物の範囲で、常識的な回数で使ってください。

## ライセンス

MIT ライセンス。本家 [r123singh/amazon-mcp-server](https://github.com/r123singh/amazon-mcp-server) に基づく派生物です。
