"""
RACONNE 求人一覧 取得プログラム
  - .env の RACONNE_ID / RACONNE_PASSWORD でログイン
  - 求人一覧（全てタブ）を page=0,1,2... と順に読み、1ページごとに CSV へ追記
  - 途中で止まっても、もう一度実行すれば続きのページから再開
  - 最後に「取得件数」と画面の「該当件数」を比較して表示

使い方:
  python raconne_jobs.py --pages 2     # 最初の2ページだけ試す
  python raconne_jobs.py               # 全ページ取得（続きから再開）
  python raconne_jobs.py --reset       # 最初からやり直す（CSVと進捗を消す）
"""

import argparse
import csv
import io
import json
import os
import random
import re
import sys
import time
from datetime import datetime
from urllib.parse import quote

from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# ============================================================
# 設定
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_NAMES = [".env", ".env.txt", "env", "env.txt"]  # メモ帳で .txt が付いても読めるように


def load_env_file():
    """.env を探して読み込む（メモ帳の保存形式の違いにも対応）。見つかったパスを返す"""
    for name in ENV_NAMES:
        path = os.path.join(BASE_DIR, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as f:
            raw = f.read()
        encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            text = raw.decode("cp932", errors="ignore")
        load_dotenv(stream=io.StringIO(text), override=True)
        return path
    return None


ENV_PATH = load_env_file()

RACONNE_ID       = os.environ.get("RACONNE_ID", "")
RACONNE_PASSWORD = os.environ.get("RACONNE_PASSWORD", "")
HEADLESS         = os.environ.get("HEADLESS", "0") == "1"  # 1 にすると画面を出さずに動く

LOGIN_URL = "https://business.raconne.app/summary-dashboard"
LIST_URL  = "https://business.raconne.app/job-search?page={page}&search={search}&sort={sort}&tab=1"
SEARCH    = '{"isPublic":1}'
SORT      = '{"order":"desc","field":"last_updated_at"}'

CSV_FILE      = os.path.join(BASE_DIR, "raconne_jobs.csv")
PROGRESS_FILE = os.path.join(BASE_DIR, "raconne_progress.json")
DEBUG_DIR     = os.path.join(BASE_DIR, "raconne_debug")

COLUMNS = ["求人番号", "公開・非公開", "要素タイプ", "形態", "職種", "就業場所",
           "勤務時間", "年収例", "月収例", "紹介", "戻る", "取得日時"]

# 画面の見出し → CSV の列名（見出しの表記ゆれに対応）
HEADER_ALIASES = {
    "求人番号":   ["求人番号", "求人No", "求人ID", "No"],
    "公開・非公開": ["公開・非公開", "公開/非公開", "公開", "ステータス"],
    "要素タイプ": ["要素タイプ"],
    "形態":       ["形態", "雇用形態"],
    "職種":       ["職種"],
    "就業場所":   ["就業場所", "勤務地"],
    "勤務時間":   ["勤務時間"],
    "年収例":     ["年収例", "年収"],
    "月収例":     ["月収例", "月収"],
    "紹介":       ["紹介"],
    "戻る":       ["戻る"],
}

WAIT_MIN, WAIT_MAX = 2.0, 3.0  # 1ページごとの待ち時間（秒）

# 画面から表の見出しと各行の文字を取り出す（table 形式・role="grid" 形式の両方に対応）
EXTRACT_JS = r"""
() => {
  const clean = s => (s || "").replace(/\s+/g, " ").trim();
  const cellText = c => {
    let t = clean(c.innerText);
    if (!t) {
      const cb = c.querySelector('input[type="checkbox"], [role="switch"]');
      if (cb) t = (cb.checked || cb.getAttribute("aria-checked") === "true") ? "公開" : "非公開";
    }
    return t;
  };
  let headers = [], rows = [];
  const table = [...document.querySelectorAll("table")].find(t => t.querySelector("tbody tr"));
  if (table) {
    headers = [...table.querySelectorAll("thead th, thead td")].map(c => clean(c.innerText));
    rows = [...table.querySelectorAll("tbody tr")].map(r => [...r.querySelectorAll("td, th")].map(cellText));
  } else {
    headers = [...document.querySelectorAll('[role="columnheader"]')].map(c => clean(c.innerText));
    rows = [...document.querySelectorAll('[role="row"]')]
      .map(r => [...r.querySelectorAll('[role="cell"], [role="gridcell"]')].map(cellText))
      .filter(r => r.length);
  }
  rows = rows.filter(r => r.some(x => x));
  return { headers, rows, body: document.body.innerText };
}
"""

ROW_SELECTOR = 'table tbody tr, [role="row"] [role="cell"], [role="row"] [role="gridcell"]'


# ============================================================
# 進捗・CSV
# ============================================================

def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"next_page": 0, "total_on_screen": None}


def save_progress(progress: dict) -> None:
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


def load_existing_ids() -> set:
    """すでに CSV にある求人番号（再開時の重複防止用）"""
    if not os.path.exists(CSV_FILE):
        return set()
    with open(CSV_FILE, encoding="utf-8-sig", newline="") as f:
        return {row["求人番号"] for row in csv.DictReader(f) if row.get("求人番号")}


def append_csv(records: list[dict]) -> None:
    is_new = not os.path.exists(CSV_FILE)
    # 新規作成時だけ BOM 付き（utf-8-sig）、追記時は BOM なしで続ける
    with open(CSV_FILE, "a", encoding="utf-8-sig" if is_new else "utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerows(records)


def count_csv_rows() -> int:
    if not os.path.exists(CSV_FILE):
        return 0
    with open(CSV_FILE, encoding="utf-8-sig", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


# ============================================================
# 画面操作
# ============================================================

def login(page) -> None:
    print("ログイン中...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    pw = page.locator('input[type="password"]').first
    if pw.count() == 0:
        print("  ログイン画面が出なかったため、ログイン済みとして続けます")
        return

    user = page.locator(
        'input[type="email"], input[name*="mail" i], input[name*="id" i], '
        'input[name*="user" i], input[name*="login" i], input[type="text"]'
    ).first
    user.fill(RACONNE_ID)
    pw.fill(RACONNE_PASSWORD)

    button = page.locator(
        'button[type="submit"], button:has-text("ログイン"), input[type="submit"]'
    ).first
    if button.count():
        button.click()
    else:
        pw.press("Enter")

    try:
        page.wait_for_selector('input[type="password"]', state="detached", timeout=20000)
    except PlaywrightTimeoutError:
        save_debug(page, "login_failed")
        sys.exit("ログインに失敗しました。.env の ID / パスワードを確認してください。")
    print("  ログイン成功")


def save_debug(page, name: str) -> None:
    os.makedirs(DEBUG_DIR, exist_ok=True)
    page.screenshot(path=os.path.join(DEBUG_DIR, f"{name}.png"), full_page=True)
    with open(os.path.join(DEBUG_DIR, f"{name}.html"), "w", encoding="utf-8") as f:
        f.write(page.content())
    print(f"  ※ 画面の記録を保存しました: {DEBUG_DIR}")


def find_total(body_text: str):
    """画面の「該当件数」を読む（例: 該当件数 3,737件 / 3737件）"""
    for pattern in (r"該当件数[^\d]{0,10}([\d,]+)", r"([\d,]+)\s*件"):
        m = re.search(pattern, body_text)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


def map_columns(headers: list[str]) -> dict:
    """CSV の列名 → 画面の何列目か"""
    mapping = {}
    for col, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            idx = next((i for i, h in enumerate(headers) if h == alias), None)
            if idx is None:
                idx = next((i for i, h in enumerate(headers) if alias in h), None)
            if idx is not None and idx not in mapping.values():
                mapping[col] = idx
                break
    return mapping


def scrape_page(page, page_no: int) -> tuple[list[dict], int | None]:
    url = LIST_URL.format(page=page_no, search=quote(SEARCH), sort=quote(SORT))
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_selector(ROW_SELECTOR, timeout=20000)
        page.wait_for_load_state("networkidle", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    data = page.evaluate(EXTRACT_JS)
    if page_no == 0:
        save_debug(page, "page0")
        print(f"  画面の見出し: {data['headers']}")

    mapping = map_columns(data["headers"])
    missing = [c for c in HEADER_ALIASES if c not in mapping]
    if missing and data["rows"]:
        if page_no == 0:
            print(f"  ※ 見出しから見つからない列: {missing}（左から順番に当てはめます）")
        mapping = {}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    records = []
    for cells in data["rows"]:
        if mapping:
            rec = {col: cells[i] if i < len(cells) else "" for col, i in mapping.items()}
        else:
            rec = dict(zip(HEADER_ALIASES.keys(), cells))
        rec = {col: rec.get(col, "") for col in COLUMNS}
        rec["取得日時"] = now
        records.append(rec)
    return records, find_total(data["body"])


# ============================================================
# メイン
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="RACONNE 求人一覧を CSV に保存")
    parser.add_argument("--pages", type=int, help="今回読むページ数（お試し用）")
    parser.add_argument("--reset", action="store_true", help="CSV と進捗を消して最初から")
    args = parser.parse_args()

    if not RACONNE_ID or not RACONNE_PASSWORD:
        print(f"探したフォルダ: {BASE_DIR}")
        if ENV_PATH:
            print(f"見つかったファイル: {ENV_PATH}")
            print("→ ファイルはありましたが、RACONNE_ID / RACONNE_PASSWORD の行が読めませんでした。")
        else:
            print("→ このフォルダに .env が見つかりませんでした。")
            print("  フォルダ内のファイル:", ", ".join(sorted(os.listdir(BASE_DIR))))
        sys.exit(".env の RACONNE_ID と RACONNE_PASSWORD を入力してから実行してください。")

    if args.reset:
        for path in (CSV_FILE, PROGRESS_FILE):
            if os.path.exists(path):
                os.remove(path)
        print("CSV と進捗をリセットしました")

    progress = load_progress()
    existing_ids = load_existing_ids()
    start = progress["next_page"]
    if start > 0:
        print(f"前回の続き（page={start}）から再開します。保存済み: {len(existing_ids)}件")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        page = browser.new_context(locale="ja-JP").new_page()
        login(page)

        page_no, done = start, 0
        while args.pages is None or done < args.pages:
            print(f"page={page_no} を読み込み中...")
            records, total = scrape_page(page, page_no)
            if total is not None:
                progress["total_on_screen"] = total

            if not records:
                print("  求人がないため終了します（最終ページ）")
                break

            new = [r for r in records if not r["求人番号"] or r["求人番号"] not in existing_ids]
            append_csv(new)
            existing_ids.update(r["求人番号"] for r in new)
            page_no += 1
            done += 1
            progress["next_page"] = page_no
            save_progress(progress)
            print(f"  {len(records)}件 読み取り / {len(new)}件 追記（累計 {len(existing_ids)}件）")

            time.sleep(random.uniform(WAIT_MIN, WAIT_MAX))

        browser.close()

    got = count_csv_rows()
    total = progress.get("total_on_screen")
    print("\n========== 結果 ==========")
    print(f"取得件数　: {got}件")
    print(f"該当件数　: {total if total is not None else '画面から読めませんでした'}件")
    if total is not None:
        print("→ 一致しました" if got == total else f"→ 差: {total - got}件")
    print(f"保存先　　: {CSV_FILE}")


if __name__ == "__main__":
    main()
