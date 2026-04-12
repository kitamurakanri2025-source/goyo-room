import os
import requests
import anthropic
from datetime import datetime
import json
import time

# API Keys from environment variables
RAKUTEN_APP_ID = os.environ.get("RAKUTEN_APP_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


def fetch_rakuten_ranking(genre_id: str) -> list[dict]:
    """楽天市場のランキングAPIから商品を取得"""
    url = "https://app.rakuten.co.jp/services/api/IchibaItem/Ranking/20170628"
    params = {
        "applicationId": RAKUTEN_APP_ID,
        "genreId": genre_id,
        "hits": 10,
        "format": "json",
    }

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        items = data.get("Items", [])
        return [item["Item"] for item in items]
    except requests.RequestException as e:
        print(f"Rakuten API error: {e}")
        return []


def collect_candidate_products() -> list[dict]:
    """カテゴリから候補商品を収集"""
    genre_configs = [
        ("日用品・生活用品", "558944"),
        ("食品・飲料",       "100227"),
        ("メンズスキンケア", "558948"),
    ]

    candidates = []
    for category_name, genre_id in genre_configs:
        print(f"  カテゴリ「{category_name}」からランキングを取得中...")
        items = fetch_rakuten_ranking(genre_id)
        for item in items[:5]:
            item["_category"] = category_name
            candidates.append(item)
        time.sleep(0.5)  # API rate limiting

    return candidates


def select_products_with_claude(candidates: list[dict]) -> list[dict]:
    """Claude APIを使ってレビュー数の比率を保ちながら5商品を選定

    レビュー少なめ（50〜200件）: 3商品
    レビュー多め（200件超）    : 2商品
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # レビュー数でグループ分け
    few_reviews = [
        item for item in candidates
        if 50 <= item.get("reviewCount", 0) <= 200
    ]
    many_reviews = [
        item for item in candidates
        if item.get("reviewCount", 0) > 200
    ]

    def build_product_list(items: list[dict]) -> str:
        return "\n".join(
            f"{i}. [{item.get('_category', '不明')}] "
            f"{item.get('itemName', '不明')[:60]} "
            f"- ¥{item.get('itemPrice', 0):,} "
            f"(レビュー平均: {item.get('reviewAverage', 0)}点 / {item.get('reviewCount', 0)}件)"
            for i, item in enumerate(items, 1)
        )

    def select_from_group(items: list[dict], count: int) -> list[dict]:
        if not items:
            return []
        if len(items) <= count:
            return items[:count]

        prompt = f"""以下の商品リストから、紹介価値の高い{count}商品を選んでください。

選定基準:
- 幅広い人に役立つ実用的な商品
- バリエーションを持たせる（カテゴリが偏らないように）
- コストパフォーマンスが良い商品

商品リスト:
{build_product_list(items)}

回答形式: 選んだ商品の番号をカンマ区切りで返してください（例: 1,3,5）
番号のみを返してください。"""

        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}],
        )

        indices_str = message.content[0].text.strip()
        indices = [
            int(x.strip()) - 1
            for x in indices_str.split(",")
            if x.strip().isdigit()
        ]

        selected = []
        for idx in indices[:count]:
            if 0 <= idx < len(items):
                selected.append(items[idx])
        return selected

    # 各グループから選定してレビュー区分タグを付与
    selected_few = select_from_group(few_reviews, 3)
    for item in selected_few:
        item["_review_tier"] = "few"

    selected_many = select_from_group(many_reviews, 2)
    for item in selected_many:
        item["_review_tier"] = "many"

    return selected_few + selected_many


def generate_post_content(item: dict) -> dict:
    """Claude APIを使って楽天ROOM投稿文・動画台本・ハッシュタグを生成"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    item_name = item.get("itemName", "商品名不明")
    item_price = item.get("itemPrice", 0)
    item_url = item.get("itemUrl", "")
    review_average = item.get("reviewAverage", 0)
    review_count = item.get("reviewCount", 0)
    item_caption = item.get("itemCaption", "")[:200]
    category = item.get("_category", "日用品")
    review_tier = item.get("_review_tier", "many")

    if review_tier == "few":
        script_instruction = (
            "- 冒頭は必ず「楽天ユーザー必見！」から始める\n"
            "- 続けて「レビューが少なくて迷っている人も多いと思いますが、"
            "調べたらかなり良かったので紹介します」という流れで不安を払拭する内容にする\n"
            "- 商品の特徴を3点紹介\n"
            "- CTAで締める（「リンクはROOMから！」等）"
        )
    else:
        script_instruction = (
            "- 冒頭は必ず「楽天ユーザー必見！」から始める\n"
            "- 続けて「レビュー多数の実績ある商品をコスパ重視で紹介します」という流れにする\n"
            "- 商品の特徴を3点紹介\n"
            "- CTAで締める（「リンクはROOMから！」等）"
        )

    prompt = f"""楽天市場の以下の商品について、楽天ROOMの投稿コンテンツを作成してください。

商品情報:
- 商品名: {item_name}
- 価格: ¥{item_price:,}
- カテゴリ: {category}
- レビュー: {review_average}点（{review_count}件）
- 説明: {item_caption}
- URL: {item_url}

以下の3つを作成してください:

【楽天ROOM投稿文】
- 200〜300文字
- 商品の魅力を自然な言葉で伝える
- 誰にでも伝わる実用的なポイントを強調
- 絵文字を適度に使用

【30秒動画台本】
- ナレーション形式（30秒 = 約150文字）
{script_instruction}

【ハッシュタグ】
- 10〜15個
- 商品に関連するもの + ターゲット層向けのもの
- 楽天ROOM・楽天市場関連のタグを含める

回答は以下のJSON形式で返してください:
{{
  "room_post": "投稿文",
  "video_script": "動画台本",
  "hashtags": "#タグ1 #タグ2 ..."
}}"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}],
    )

    response_text = message.content[0].text.strip()

    # JSONの抽出
    json_start = response_text.find("{")
    json_end = response_text.rfind("}") + 1
    if json_start != -1 and json_end > json_start:
        json_str = response_text[json_start:json_end]
        content = json.loads(json_str)
    else:
        content = {
            "room_post": response_text,
            "video_script": "",
            "hashtags": "",
        }

    return content


def save_to_file(
    products: list[dict],
    contents: list[dict],
    output_file: str = "today_posts.txt",
):
    """生成したコンテンツをファイルに保存"""
    today = datetime.now().strftime("%Y年%m月%d日")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# 楽天ROOM 投稿コンテンツ - {today}\n")
        f.write("# 楽天市場 デイリーランキング厳選\n")
        f.write("=" * 60 + "\n\n")

        for i, (item, content) in enumerate(zip(products, contents), 1):
            item_name = item.get("itemName", "商品名不明")
            item_price = item.get("itemPrice", 0)
            item_url = item.get("itemUrl", "")
            category = item.get("_category", "不明")

            review_tier = item.get("_review_tier", "many")
            review_label = (
                "レビュー少なめ（50〜200件）" if review_tier == "few"
                else "レビュー多め（200件超）"
            )

            f.write(f"【商品 {i}】{item_name[:50]}\n")
            f.write(f"カテゴリ: {category}\n")
            f.write(f"レビュー区分: {review_label}\n")
            f.write(f"価格: ¥{item_price:,}\n")
            f.write(f"URL: {item_url}\n")
            f.write("-" * 40 + "\n\n")

            f.write("▼ 楽天ROOM 投稿文\n")
            f.write(content.get("room_post", "") + "\n\n")

            f.write("▼ 30秒動画台本\n")
            f.write(content.get("video_script", "") + "\n\n")

            f.write("▼ ハッシュタグ\n")
            f.write(content.get("hashtags", "") + "\n")
            f.write("\n" + "=" * 60 + "\n\n")

    print(f"today_posts.txt に保存しました（{len(products)}商品）")


def main():
    print("楽天ROOM自動投稿ツール 起動中...")
    print(f"実行日: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}")
    print()

    if not RAKUTEN_APP_ID:
        print("エラー: RAKUTEN_APP_ID が設定されていません")
        print("   export RAKUTEN_APP_ID='your_app_id' を実行してください")
        return

    if not ANTHROPIC_API_KEY:
        print("エラー: ANTHROPIC_API_KEY が設定されていません")
        print("   export ANTHROPIC_API_KEY='your_api_key' を実行してください")
        return

    # Step 1: 候補商品を収集
    print("Step 1: 楽天市場ランキングから候補商品を収集中...")
    candidates = collect_candidate_products()
    print(f"   → {len(candidates)}件の候補商品を取得しました")
    print()

    if not candidates:
        print("エラー: 候補商品の取得に失敗しました")
        return

    # Step 2: Claude APIで5商品を選定
    print("Step 2: Claude APIで紹介すべき5商品を選定中...")
    selected_products = select_products_with_claude(candidates)
    print(f"   → {len(selected_products)}商品を選定しました")
    for item in selected_products:
        tier_label = "【少】" if item.get("_review_tier") == "few" else "【多】"
        print(f"   {tier_label} [{item.get('_category')}] {item.get('itemName', '')[:40]}...")
    print()

    # Step 3: 各商品のコンテンツを生成
    print("Step 3: 楽天ROOM投稿文・動画台本・ハッシュタグを生成中...")
    generated_contents = []
    for i, item in enumerate(selected_products, 1):
        print(f"   [{i}/{len(selected_products)}] {item.get('itemName', '')[:40]}...")
        content = generate_post_content(item)
        generated_contents.append(content)
        time.sleep(1)  # API rate limiting
    print()

    # Step 4: ファイルに保存
    print("Step 4: today_posts.txt に保存中...")
    save_to_file(selected_products, generated_contents)
    print()
    print("完了！today_posts.txt を確認してください。")


if __name__ == "__main__":
    main()
