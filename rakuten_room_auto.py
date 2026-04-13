"""
楽天ROOM自動化システム
  - 毎朝7時: 安定枠1件 + 穴場枠1件（計2投稿）を選定・台本生成
  - イベント日: 計3投稿
  - 毎晩22時: ポイント高い日（翌日）・スーパーSALE 3日前をChatwork通知
  - 投稿管理: Googleスプレッドシートへ記録（重複投稿防止）
"""

import os
import random
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

import anthropic
import gspread
import requests
import schedule
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

# ============================================================
# 設定読み込み
# ============================================================

load_dotenv()

RAKUTEN_APP_ID       = os.environ.get("RAKUTEN_APP_ID", "")
RAKUTEN_ACCESS_KEY   = os.environ.get("RAKUTEN_ACCESS_KEY", "")
RAKUTEN_AFFILIATE_ID = os.environ.get("RAKUTEN_AFFILIATE_ID", "")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "")
CHATWORK_API_KEY     = os.environ.get("CHATWORK_API_KEY", "")

CHATWORK_ROOM_ID     = "433364514"
SPREADSHEET_ID       = "121ZKgULHpZoJG2v0RPif4GpRlBlyx6PkNUDPOziZxpw"
SHEET_NAME           = "投稿管理（楽天ROOM）"
GOOGLE_CREDENTIALS_FILE = "google_credentials.json"

# ジャンルID（日用品:食品:スキンケア = 1:1:1）
GENRE_CONFIGS = [
    ("日用品・生活用品", "558944"),
    ("食品・飲料",       "100227"),
    ("スキンケア",       "558948"),
]

# ポイントアップ日
POINT_DAYS = [5, 10, 15, 20, 25, 30]

# 楽天スーパーSALE 開始予定日リスト（YYYY-MM-DD で追記していく）
SUPER_SALE_DATES = [
    "2026-06-04",
    "2026-09-04",
    "2026-12-04",
]

# イベント日リスト（YYYY-MM-DD で追記すると当日3投稿になる）
EVENT_DAYS: list[str] = []


# ============================================================
# Chatwork 通知
# ============================================================

def send_chatwork_message(message: str) -> bool:
    """Chatwork ルームにメッセージを送信"""
    url = f"https://api.chatwork.com/v2/rooms/{CHATWORK_ROOM_ID}/messages"
    headers = {"X-ChatWorkToken": CHATWORK_API_KEY}
    payload = {"body": message, "self_unread": 1}
    try:
        response = requests.post(url, headers=headers, data=payload, timeout=10)
        response.raise_for_status()
        print("Chatwork 通知を送信しました")
        return True
    except requests.RequestException as e:
        print(f"Chatwork 送信エラー: {e}")
        return False


# ============================================================
# Google スプレッドシート
# ============================================================

def _get_sheet():
    """Google Sheets ワークシートに接続"""
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)


def load_posted_item_codes() -> set[str]:
    """スプレッドシートから投稿済み商品コードを取得"""
    try:
        sheet = _get_sheet()
        records = sheet.get_all_records()
        return {str(row["商品コード"]) for row in records if row.get("商品コード")}
    except Exception as e:
        print(f"スプレッドシート読み込みエラー: {e}")
        return set()


def save_to_spreadsheet(item: dict, post_type: str) -> bool:
    """商品情報をスプレッドシートに追記"""
    try:
        sheet = _get_sheet()
        today = datetime.now().strftime("%Y/%m/%d")
        row = [
            today,                              # 日付
            item.get("itemCode", ""),           # 商品コード
            item.get("itemName", "")[:50],      # 商品名
            item.get("_category", ""),          # ジャンル
            item.get("itemPrice", 0),           # 価格
            item.get("reviewCount", 0),         # レビュー件数
            item.get("reviewAverage", 0),       # 星評価
            post_type,                          # 安定/穴場フラグ
            "済",                               # 投稿済みフラグ
        ]
        sheet.append_row(row)
        return True
    except Exception as e:
        print(f"スプレッドシート書き込みエラー: {e}")
        return False


# ============================================================
# 楽天 API
# ============================================================

def _rakuten_get(url: str, params: dict) -> dict:
    """楽天 API GET（共通）"""
    params["applicationId"] = RAKUTEN_APP_ID
    params["accessKey"]     = RAKUTEN_ACCESS_KEY
    params["affiliateId"]   = RAKUTEN_AFFILIATE_ID
    params["format"]        = "json"
    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    return response.json()


def fetch_ranking_paged(genre_id: str, max_items: int = 100) -> list[dict]:
    """ランキング API をページングして最大 max_items 件取得"""
    url = "https://openapi.rakuten.co.jp/ichibaranking/api/IchibaItem/Ranking/20220601"
    all_items: list[dict] = []
    page = 1
    per_page = 30

    while len(all_items) < max_items:
        try:
            data = _rakuten_get(url, {"genreId": genre_id, "hits": per_page, "page": page})
            items = [i["Item"] for i in data.get("Items", [])]
            if not items:
                break
            all_items.extend(items)
            page += 1
            time.sleep(0.5)
        except requests.RequestException as e:
            print(f"  ランキング API エラー (p{page}): {e}")
            break

    return all_items[:max_items]


def search_items(genre_id: str, hits: int = 100) -> list[dict]:
    """商品検索 API（穴場枠用・更新日新しい順）"""
    url = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20220601"
    try:
        data = _rakuten_get(url, {
            "genreId": genre_id,
            "hits": hits,
            "sort": "-updateTimestamp",
        })
        return [i["Item"] for i in data.get("Items", [])]
    except requests.RequestException as e:
        print(f"  検索 API エラー: {e}")
        return []


# ============================================================
# 商品フィルタリング
# ============================================================

_THREE_MONTHS_AGO = (datetime.now() - timedelta(days=90)).strftime("%Y%m%d%H%M%S")


def _is_recently_updated(item: dict) -> bool:
    ts = item.get("updateTimestamp", "")
    if not ts:
        return True
    ts_norm = ts.replace("-", "").replace(" ", "").replace(":", "")
    return ts_norm >= _THREE_MONTHS_AGO


def _passes_common_filter(item: dict, posted_codes: set[str],
                           min_reviews: int, max_reviews: int) -> bool:
    code         = item.get("itemCode", "")
    review_count = item.get("reviewCount", 0)
    review_avg   = float(item.get("reviewAverage", 0))

    if code in posted_codes:
        return False
    if not (min_reviews <= review_count <= max_reviews):
        return False
    if not (4.0 <= review_avg < 4.9):
        return False
    if not _is_recently_updated(item):
        return False
    return True


def filter_stable(items: list[dict], posted_codes: set[str]) -> list[dict]:
    """安定枠フィルタ: レビュー 51〜300件、星 4.0〜4.9、直近3ヶ月更新"""
    return [i for i in items if _passes_common_filter(i, posted_codes, 51, 300)]


def filter_hidden_gem(items: list[dict], posted_codes: set[str]) -> list[dict]:
    """穴場枠フィルタ: レビュー 10〜50件、星 4.0〜4.9、直近3ヶ月更新
    優先順位: 更新日新しい順 → 星高い順 → レビュー件数多い順"""
    filtered = [i for i in items if _passes_common_filter(i, posted_codes, 10, 50)]
    filtered.sort(
        key=lambda x: (
            x.get("updateTimestamp", ""),
            float(x.get("reviewAverage", 0)),
            x.get("reviewCount", 0),
        ),
        reverse=True,
    )
    return filtered


# ============================================================
# 商品選定
# ============================================================

def select_stable_product(posted_codes: set[str]) -> Optional[dict]:
    """安定枠: ジャンルをシャッフルして最初に条件を満たす1件を返す"""
    genres = list(GENRE_CONFIGS)
    random.shuffle(genres)

    for category_name, genre_id in genres:
        print(f"  安定枠: 「{category_name}」を検索中...")
        items = fetch_ranking_paged(genre_id, max_items=100)
        for item in items:
            item["_category"] = category_name

        filtered = filter_stable(items, posted_codes)
        if filtered:
            selected = filtered[0]
            selected["_post_type"] = "安定"
            return selected

    return None


def select_hidden_gem_product(stable_item: dict, posted_codes: set[str]) -> Optional[dict]:
    """穴場枠: 安定枠と同ジャンルを優先し、なければ他ジャンルに拡張"""
    # 安定枠のジャンルを先頭に、残りを後ろに並べる
    stable_category = stable_item.get("_category", "")
    ordered_genres = sorted(
        GENRE_CONFIGS,
        key=lambda g: 0 if g[0] == stable_category else 1,
    )

    for category_name, genre_id in ordered_genres:
        print(f"  穴場枠: 「{category_name}」を検索中...")
        items = search_items(genre_id, hits=100)
        for item in items:
            item["_category"] = category_name

        filtered = filter_hidden_gem(items, posted_codes)
        if filtered:
            selected = filtered[0]
            selected["_post_type"] = "穴場"
            return selected

    return None


# ============================================================
# Claude API: 特徴一言生成（安定枠台本用）
# ============================================================

def generate_feature_one_liner(item: dict) -> str:
    """商品の特徴を15文字以内の一言で生成"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    item_name    = item.get("itemName", "")[:60]
    item_caption = item.get("itemCaption", "")[:200]

    prompt = (
        f"以下の商品の特徴を15文字以内の一言で表してください。\n"
        f"商品名: {item_name}\n"
        f"説明: {item_caption}\n"
        f"一言のみ返してください。例:「毎日使えるコスパ最強品」"
    )
    try:
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        print(f"  Claude API エラー: {e}")
        return "毎日の生活に役立つ一品"


# ============================================================
# 台本生成
# ============================================================

def generate_script_stable(item: dict) -> str:
    """安定枠の台本（特徴一言は Claude API で生成）"""
    feature = generate_feature_one_liner(item)
    return (
        f"楽天ユーザー必見！\n"
        f"楽天で売れ続けている実力派商品です。\n"
        f"{item.get('itemName', '')[:30]}、{item.get('itemPrice', 0):,}円、"
        f"星{item.get('reviewAverage', 0)}。\n"
        f"{feature}。買って損なし。\n"
        f"楽天ROOMのリンクからどうぞ。"
    )


def generate_script_hidden_gem(item: dict) -> str:
    """穴場枠の台本（固定テンプレート）"""
    return (
        f"楽天ユーザー必見！\n"
        f"楽天で売れてるのにまだ知られていない\n"
        f"良品を見つけました。\n"
        f"{item.get('itemName', '')[:30]}、{item.get('itemPrice', 0):,}円、"
        f"星{item.get('reviewAverage', 0)}。\n"
        f"気になる人は楽天ROOMのリンクからどうぞ。"
    )


# ============================================================
# 通知チェック（毎晩 22:00 実行）
# ============================================================

def check_point_day_notification():
    """翌日がポイント高い日なら Chatwork 通知"""
    tomorrow_day = (datetime.now() + timedelta(days=1)).day
    if tomorrow_day in POINT_DAYS:
        message = (
            f"[楽天ROOM] ポイントアップ前日のお知らせ\n"
            f"明日（{tomorrow_day}日）はポイントアップの日です！\n"
            f"楽天ROOMへの投稿を積極的に行いましょう。"
        )
        send_chatwork_message(message)


def check_super_sale_notification():
    """スーパーSALE 3日前なら Chatwork 通知"""
    three_days_later = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    if three_days_later in SUPER_SALE_DATES:
        message = (
            f"[楽天ROOM] 楽天スーパーSALE 3日前のお知らせ\n"
            f"スーパーSALE 開始まであと3日です（{three_days_later} 開始予定）。\n"
            f"事前に投稿コンテンツを準備しておきましょう。"
        )
        send_chatwork_message(message)


def run_evening_check():
    """毎晩 22:00 に実行"""
    print(f"[{datetime.now().strftime('%H:%M')}] 夜間チェック実行中...")
    check_point_day_notification()
    check_super_sale_notification()


# ============================================================
# ファイル保存
# ============================================================

def save_posts_to_file(posts: list[dict], output_file: str = "today_posts.txt"):
    today = datetime.now().strftime("%Y年%m月%d日")

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# 楽天ROOM 投稿コンテンツ - {today}\n")
        f.write(f"# 投稿数: {len(posts)}件\n")
        f.write("=" * 60 + "\n\n")

        for i, post in enumerate(posts, 1):
            item      = post["item"]
            script    = post["script"]
            post_type = item.get("_post_type", "不明")

            f.write(f"【投稿 {i}】{item.get('itemName', '')[:50]}\n")
            f.write(f"種別: {post_type}枠\n")
            f.write(f"カテゴリ: {item.get('_category', '')}\n")
            f.write(f"価格: ¥{item.get('itemPrice', 0):,}\n")
            f.write(f"レビュー: {item.get('reviewAverage', 0)}点 / {item.get('reviewCount', 0)}件\n")
            f.write(f"URL: {item.get('itemUrl', '')}\n")
            f.write("-" * 40 + "\n\n")
            f.write("▼ 台本\n")
            f.write(script + "\n")
            f.write("\n" + "=" * 60 + "\n\n")

    print(f"today_posts.txt に保存しました（{len(posts)}件）")


# ============================================================
# メイン処理（毎朝 7:00）
# ============================================================

def run_daily_job():
    """商品選定・台本生成・スプレッドシート記録・Chatwork 通知"""
    print(f"\n{'='*60}")
    print(f"楽天ROOM 自動投稿 開始: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}")
    print(f"{'='*60}\n")

    today_str   = datetime.now().strftime("%Y-%m-%d")
    is_event    = today_str in EVENT_DAYS
    target_count = 3 if is_event else 2

    # 投稿済みコードを取得
    print("投稿済み商品コードを取得中...")
    posted_codes = load_posted_item_codes()
    print(f"  → {len(posted_codes)}件の投稿済み商品コードを確認\n")

    posts: list[dict] = []

    # ── 安定枠 ──────────────────────────────────────────────
    print("安定枠を選定中...")
    stable_item = select_stable_product(posted_codes)
    if stable_item:
        script = generate_script_stable(stable_item)
        posts.append({"item": stable_item, "script": script})
        posted_codes.add(stable_item.get("itemCode", ""))
        print(f"  → 選定: {stable_item.get('itemName', '')[:40]}")
    else:
        print("  → 安定枠の商品が見つかりませんでした")
    print()

    # ── 穴場枠 ──────────────────────────────────────────────
    print("穴場枠を選定中...")
    hidden_item = select_hidden_gem_product(stable_item or {}, posted_codes)
    if hidden_item:
        script = generate_script_hidden_gem(hidden_item)
        posts.append({"item": hidden_item, "script": script})
        posted_codes.add(hidden_item.get("itemCode", ""))
        print(f"  → 選定: {hidden_item.get('itemName', '')[:40]}")
    else:
        print("  → 穴場枠の商品が見つかりませんでした")
    print()

    # ── イベント日: 3件目（安定枠から追加）──────────────────
    if is_event and len(posts) < target_count:
        print("イベント日: 3件目の商品を選定中...")
        extra_item = select_stable_product(posted_codes)
        if extra_item:
            script = generate_script_stable(extra_item)
            posts.append({"item": extra_item, "script": script})
            print(f"  → 選定: {extra_item.get('itemName', '')[:40]}")
        print()

    if not posts:
        print("投稿できる商品がありませんでした。終了します。")
        return

    # ── ファイル保存 ────────────────────────────────────────
    save_posts_to_file(posts)

    # ── スプレッドシート記録 ────────────────────────────────
    print("\nスプレッドシートに記録中...")
    for post in posts:
        save_to_spreadsheet(post["item"], post["item"].get("_post_type", "不明"))
    print(f"  → {len(posts)}件を記録しました")

    # ── Chatwork 通知 ────────────────────────────────────────
    summary = "\n".join(
        f"・[{p['item'].get('_post_type')}枠] "
        f"{p['item'].get('itemName', '')[:30]} "
        f"¥{p['item'].get('itemPrice', 0):,}"
        for p in posts
    )
    send_chatwork_message(
        f"[楽天ROOM] 本日の投稿コンテンツが生成されました\n"
        f"日付: {today_str} / 投稿数: {len(posts)}件\n\n"
        f"{summary}\n\n"
        f"詳細は today_posts.txt を確認してください。"
    )

    print(f"\n完了！ {len(posts)}件の投稿コンテンツを生成しました。")


# ============================================================
# 設定チェック
# ============================================================

def validate_config() -> bool:
    missing = [
        key for key, val in [
            ("RAKUTEN_APP_ID",    RAKUTEN_APP_ID),
            ("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY),
            ("CHATWORK_API_KEY",  CHATWORK_API_KEY),
        ]
        if not val
    ]
    if missing:
        print("エラー: 以下の環境変数が設定されていません")
        for key in missing:
            print(f"  - {key}")
        print(".env ファイルを確認してください。")
        return False
    return True


# ============================================================
# エントリーポイント
# ============================================================

def main():
    print("楽天ROOM 自動投稿システム 起動中...")

    if not validate_config():
        sys.exit(1)

    # --run-now フラグで即時実行（テスト・手動実行用）
    if len(sys.argv) > 1 and sys.argv[1] == "--run-now":
        run_daily_job()
        return

    # スケジュール設定
    schedule.every().day.at("07:00").do(run_daily_job)
    schedule.every().day.at("22:00").do(run_evening_check)

    print("スケジュール設定完了:")
    print("  毎朝 07:00 → 商品選定・台本生成・スプレッドシート記録・Chatwork通知")
    print("  毎晩 22:00 → ポイント高い日・スーパーSALE 3日前通知")
    print("\n待機中... (Ctrl+C で終了)\n")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
