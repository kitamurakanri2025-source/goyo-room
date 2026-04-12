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
    """20〜30代男性向けカテゴリから候補商品を収集"""
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
    """Claude APIを使って20〜30代男性向けに最適な5商品を選定"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    product_list = []
    for i, item in enumerate(candidates, 1):
        product_list.append(
            f"{i}. [{item.get('_category', '不明')}] "
            f"{item.get('itemName', '不明')[:60]} "
            f"- ¥{item.get('itemPrice', 0):,} "
            f"(レビュー平均: {item.get('reviewAverage', 0)}点 / {item.get('reviewCount', 0)}件)"
        )

    prompt = f"""以下は楽天市場のデイリーランキングから取得した商品リストです。
20〜30代の男性をターゲットに、日用品・食品・スキンケアカテゴリから
最も紹介価値の高い商品を5つ選んでください。

選定基準:
- 20〜30代男性の生活に役立つ実用的な商品
- レビューが高評価または人気が高い商品
- バリエーションを持たせる（カテゴリが偏らないように）
- コストパフォーマンスが良い商品

商品リスト:
{chr(10).join(product_list)}

回答形式: 選んだ商品の番号をカンマ区切りで返してください（例: 1,5,8,12,15）
番号のみを返してください。"""

    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=100,
        messages=[{"role": "user", "content": prompt}],
    )

    selected_indices_str = message.content[0].text.strip()
    selected_indices = [
        int(x.strip()) - 1
        for x in selected_indices_str.split(",")
        if x.strip().isdigit()
    ]

    selected = []
    for idx in selected_indices[:5]:
        if 0 <= idx < len(candidates):
            selected.append(candidates[idx])

    return selected


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

    prompt = f"""楽天市場の以下の商品について、楽天ROOMの投稿コンテンツを作成してください。

商品情報:
- 商品名: {item_name}
- 価格: ¥{item_price:,}
- カテゴリ: {category}
- レビュー: {review_average}点（{review_count}件）
- 説明: {item_caption}
- URL: {item_url}

ターゲット: 20〜30代男性

以下の3つを作成してください:

【楽天ROOM投稿文】
- 200〜300文字
- 商品の魅力を自然な言葉で伝える
- 男性目線で実用的なポイントを強調
- 絵文字を適度に使用

【30秒動画台本】
- ナレーション形式（30秒 = 約150文字）
- 冒頭でフック（興味を引く一言）
- 商品の特徴を3点紹介
- CTAで締める（「リンクはROOMから！」等）

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
        f.write("# ターゲット: 20〜30代男性\n")
        f.write("=" * 60 + "\n\n")

        for i, (item, content) in enumerate(zip(products, contents), 1):
            item_name = item.get("itemName", "商品名不明")
            item_price = item.get("itemPrice", 0)
            item_url = item.get("itemUrl", "")
            category = item.get("_category", "不明")

            f.write(f"【商品 {i}】{item_name[:50]}\n")
            f.write(f"カテゴリ: {category}\n")
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
    print("Step 2: Claude APIで20〜30代男性向け5商品を選定中...")
    selected_products = select_products_with_claude(candidates)
    print(f"   → {len(selected_products)}商品を選定しました")
    for item in selected_products:
        print(f"   [{item.get('_category')}] {item.get('itemName', '')[:40]}...")
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
