"""
求人ボックス 自動返信システム
  - 1時間ごとに新着応募をチェック
  - 未返信の応募者へテンプレートメッセージを自動送信
  - 送信済み応募者IDをJSONファイルで管理（重複送信防止）
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import schedule
from dotenv import load_dotenv
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# ============================================================
# 設定読み込み
# ============================================================

load_dotenv()

KYUJINBOX_EMAIL    = os.environ.get("KYUJINBOX_EMAIL", "")
KYUJINBOX_PASSWORD = os.environ.get("KYUJINBOX_PASSWORD", "")

BASE_DIR          = Path(__file__).parent
SENT_LOG_FILE     = BASE_DIR / "kyujinbox_sent.json"

LOGIN_URL     = "https://kyujinbox.com/admin/sessions/new"
APPLICANTS_URL = "https://kyujinbox.com/admin/entries"

MESSAGE_TEMPLATE = """\
この度は、数ある求人の中から弊社へご応募いただき、誠にありがとうございます。
採用担当でございます。

ご応募いただいた内容にて【一次選考（書類選考）通過】となりましたので、
次のステップへ進んでいただきたく、ご連絡いたしました。

つきましては、次回以降の面談（選考）をよりスムーズで有意義な時間とするため、
皆様に「事前アンケート」へのご回答をお願いしております。

お手数ですが、以下のURLより内容をご確認いただき、
フォームご回答をお願いできますでしょうか。
ご回答の確認が取れ次第、担当者よりご連絡させて頂きます。

▼【約3分で完了】アンケート回答URL
https://forms.gle/1UMrv7PnFH24XQ7M9


ご回答を心よりお待ちしております！
何卒よろしくお願い申し上げます。"""

WAIT_TIMEOUT = 15  # 秒


# ============================================================
# 送信済みログ管理
# ============================================================

def load_sent_ids() -> set[str]:
    if not SENT_LOG_FILE.exists():
        return set()
    with open(SENT_LOG_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return set(data.get("sent_ids", []))


def save_sent_ids(sent_ids: set[str]):
    with open(SENT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump({"sent_ids": sorted(sent_ids)}, f, ensure_ascii=False, indent=2)


# ============================================================
# WebDriver セットアップ
# ============================================================

def create_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,800")
    options.add_argument("--lang=ja-JP")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


# ============================================================
# ログイン
# ============================================================

def login(driver: webdriver.Chrome) -> bool:
    print(f"  ログイン中... ({LOGIN_URL})")
    driver.get(LOGIN_URL)
    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    try:
        # メールアドレス入力
        email_field = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email'], input[name='email'], #email"))
        )
        email_field.clear()
        email_field.send_keys(KYUJINBOX_EMAIL)

        # パスワード入力
        pw_field = driver.find_element(By.CSS_SELECTOR, "input[type='password'], input[name='password'], #password")
        pw_field.clear()
        pw_field.send_keys(KYUJINBOX_PASSWORD)

        # ログインボタンクリック
        submit_btn = driver.find_element(
            By.CSS_SELECTOR,
            "input[type='submit'], button[type='submit'], .login-btn, .btn-login"
        )
        submit_btn.click()

        # ログイン完了を待機（ダッシュボードへ遷移）
        wait.until(EC.url_changes(LOGIN_URL))
        current_url = driver.current_url

        if "sessions/new" in current_url or "login" in current_url:
            print("  ログイン失敗: メールアドレスまたはパスワードが間違っています")
            return False

        print("  ログイン成功")
        return True

    except TimeoutException:
        print("  ログイン失敗: ページの読み込みがタイムアウトしました")
        return False
    except NoSuchElementException as e:
        print(f"  ログイン失敗: 要素が見つかりません - {e}")
        return False


# ============================================================
# 応募者一覧の取得
# ============================================================

def get_applicant_entries(driver: webdriver.Chrome) -> list[dict]:
    """応募管理ページから未対応の応募者リストを取得する"""
    print(f"  応募一覧を取得中... ({APPLICANTS_URL})")
    driver.get(APPLICANTS_URL)
    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
        time.sleep(2)  # JS レンダリング待機
    except TimeoutException:
        print("  応募一覧ページの読み込みがタイムアウトしました")
        return []

    applicants = []

    # 応募カード/行を探す（複数のセレクタを試みる）
    candidate_selectors = [
        "tr.entry-row",
        ".entry-item",
        ".applicant-row",
        "tbody tr",
        ".entry-list-item",
    ]

    rows = []
    for selector in candidate_selectors:
        rows = driver.find_elements(By.CSS_SELECTOR, selector)
        if rows:
            print(f"  応募行を {len(rows)} 件検出 (selector: {selector})")
            break

    if not rows:
        print("  応募者が見つかりませんでした（応募ゼロ or セレクタ要確認）")
        return []

    for row in rows:
        try:
            # 応募IDを data 属性またはリンクから取得
            entry_id = (
                row.get_attribute("data-entry-id")
                or row.get_attribute("data-id")
                or row.get_attribute("id")
            )

            # 詳細リンクを探す
            detail_link_el = None
            for link_selector in ["a.entry-detail-link", "a[href*='/entries/']", "a[href*='/applicants/']", "a"]:
                try:
                    detail_link_el = row.find_element(By.CSS_SELECTOR, link_selector)
                    break
                except NoSuchElementException:
                    continue

            detail_url = detail_link_el.get_attribute("href") if detail_link_el else None

            # entry_id が取れていない場合は URL から抽出
            if not entry_id and detail_url:
                parts = [p for p in detail_url.rstrip("/").split("/") if p.isdigit()]
                entry_id = parts[-1] if parts else None

            if not entry_id:
                continue

            applicants.append({
                "entry_id": str(entry_id),
                "detail_url": detail_url,
            })
        except Exception:
            continue

    print(f"  応募者 {len(applicants)} 件を取得")
    return applicants


# ============================================================
# メッセージ送信
# ============================================================

def send_message_to_applicant(driver: webdriver.Chrome, applicant: dict) -> bool:
    """応募詳細ページを開き、テンプレートメッセージを送信する"""
    detail_url = applicant.get("detail_url")
    entry_id   = applicant["entry_id"]

    if not detail_url:
        detail_url = f"https://kyujinbox.com/admin/entries/{entry_id}"

    print(f"    [{entry_id}] 詳細ページを開く: {detail_url}")
    driver.get(detail_url)
    wait = WebDriverWait(driver, WAIT_TIMEOUT)

    try:
        wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "body")))
        time.sleep(1)

        # メッセージ入力欄を探す
        textarea = None
        for selector in [
            "textarea[name='message']",
            "textarea.message-input",
            "textarea#message",
            "textarea[placeholder*='メッセージ']",
            "textarea[placeholder*='message']",
            "textarea",
        ]:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                textarea = elements[0]
                break

        if not textarea:
            print(f"    [{entry_id}] メッセージ入力欄が見つかりません")
            return False

        textarea.clear()
        textarea.send_keys(MESSAGE_TEMPLATE)

        # 送信ボタンを探す
        send_btn = None
        for selector in [
            "input[type='submit'][value*='送信']",
            "button[type='submit']",
            ".send-btn",
            ".message-send",
            "button.btn-primary",
            "input[type='submit']",
        ]:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                send_btn = elements[0]
                break

        if not send_btn:
            print(f"    [{entry_id}] 送信ボタンが見つかりません")
            return False

        send_btn.click()
        time.sleep(2)

        print(f"    [{entry_id}] メッセージを送信しました")
        return True

    except TimeoutException:
        print(f"    [{entry_id}] タイムアウト")
        return False
    except Exception as e:
        print(f"    [{entry_id}] エラー: {e}")
        return False


# ============================================================
# メインジョブ（1時間ごとに実行）
# ============================================================

def run_check_job():
    print(f"\n{'='*60}")
    print(f"求人ボックス 応募チェック開始: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}")
    print(f"{'='*60}")

    sent_ids = load_sent_ids()
    print(f"送信済み件数: {len(sent_ids)} 件\n")

    driver = create_driver()
    new_sent = 0

    try:
        # ログイン
        if not login(driver):
            print("ログインに失敗しました。終了します。")
            return

        # 応募者一覧取得
        applicants = get_applicant_entries(driver)

        if not applicants:
            print("新着応募はありません。")
            return

        # 未送信の応募者へ送信
        for applicant in applicants:
            entry_id = applicant["entry_id"]

            if entry_id in sent_ids:
                print(f"  [{entry_id}] 送信済みのためスキップ")
                continue

            print(f"  [{entry_id}] 未送信 → メッセージを送信します")
            success = send_message_to_applicant(driver, applicant)

            if success:
                sent_ids.add(entry_id)
                save_sent_ids(sent_ids)
                new_sent += 1
                time.sleep(2)  # 連続送信を避けるため待機

    finally:
        driver.quit()

    if new_sent > 0:
        print(f"\n完了: {new_sent} 件の応募者へメッセージを送信しました。")
    else:
        print("\n新規送信はありませんでした。")


# ============================================================
# 設定チェック
# ============================================================

def validate_config() -> bool:
    missing = [
        key for key, val in [
            ("KYUJINBOX_EMAIL",    KYUJINBOX_EMAIL),
            ("KYUJINBOX_PASSWORD", KYUJINBOX_PASSWORD),
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
    print("求人ボックス 自動返信システム 起動中...")

    if not validate_config():
        sys.exit(1)

    # --run-now フラグで即時実行（テスト・手動実行用）
    if len(sys.argv) > 1 and sys.argv[1] == "--run-now":
        run_check_job()
        return

    schedule.every(1).hours.do(run_check_job)

    print("スケジュール設定完了:")
    print("  1時間ごと → 新着応募チェック＆テンプレート送信")
    print("\n待機中... (Ctrl+C で終了)\n")

    # 起動直後に1回実行
    run_check_job()

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
