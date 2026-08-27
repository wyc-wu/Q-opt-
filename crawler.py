import json
import time
import re
import sys
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

# Enforce UTF-8 encoding for standard streams to fix emoji printing on Windows
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# ── 載入環境變數 ──────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ── AI 菜名快取（避免相同標題重複呼叫 API）────────────────────────────────────
_ai_name_cache: dict = {}

def _ai_extract_meal_name(raw_title: str, price: int) -> str | None:
    """
    呼叫 Gemini API，從原始商品標題自動判斷：
    - 若是真實餐點 → 回傳簡短中文菜名（≤10 字）
    - 若是公告/廣告/非餐點 → 回傳空字串 ""
    快取結果避免重複呼叫。
    """
    cache_key = raw_title.strip()
    if cache_key in _ai_name_cache:
        return _ai_name_cache[cache_key]

    if not _GEMINI_API_KEY:
        return None  # 無 API key，跳過 AI 解析

    try:
        import urllib.request
        prompt = (
            "你是一個台灣便當店菜單解析助手。\n"
            "以下是一個商品的原始標題，請判斷：\n"
            "1. 若這是一道真實的餐點（便當、飯、麵、肉類料理等），請只輸出簡短的中文菜名（不超過12字，不加任何標點或說明）。\n"
            "2. 若這是廣告文案、公告、補貼說明、或非餐點商品，請只輸出 SKIP。\n\n"
            f"商品原始標題：{raw_title}\n"
            f"商品售價：NT${price}\n\n"
            "只輸出菜名或 SKIP，不要輸出任何其他內容。"
        )
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 30}
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={_GEMINI_API_KEY}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        answer = result["candidates"][0]["content"]["parts"][0]["text"].strip()
        # 若 AI 判斷為公告，回傳空字串（呼叫方用來跳過）
        meal_name = "" if answer.upper() == "SKIP" else answer
        _ai_name_cache[cache_key] = meal_name
        print(f"    🤖 AI 解析: {repr(raw_title[:30])} → {repr(meal_name) if meal_name else 'SKIP (略過)'}")
        return meal_name
    except Exception as e:
        print(f"    ⚠️ AI 菜名解析失敗: {e}")
        return None  # 失敗時回傳 None，讓後面的 fallback 邏輯處理


def crawl_slk9898():
    """
    爬取 slk9898.com.tw 網站上「主餐」分類的全部產品。
    """
    base_url = "https://www.slk9898.com.tw"
    category_urls = [
        f"{base_url}/product-category/%e4%b8%bb%e9%a4%90/",
        f"{base_url}/product-category/%E4%B8%BB%E9%A4%90/page/2/",
    ]
    menu_data = []

    print("=" * 60)
    print("🍱 正連線至尚琳廚苑 (slk9898.com.tw) 爬取真實菜單...")
    print("=" * 60)

    try:
        with sync_playwright() as p:
            # 開啟無頭瀏覽器
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            for page_url in category_urls:
                print(f"\n📄 正在爬取: {page_url}")
                page.goto(page_url, wait_until="networkidle", timeout=30000)

                # 等待 WooCommerce 產品列表載入完成
                page.wait_for_selector("ul.products li.product", timeout=15000)
                time.sleep(2)

                # 抓取所有產品 <li> 元素 (WooCommerce 標準結構)
                products = page.query_selector_all(
                    "ul.products > li.product.type-product"
                )

                for item in products:
                    idx = len(menu_data) + 1

                    # --- 名稱 ---
                    title_elem = item.query_selector("h2.woocommerce-loop-product__title")
                    raw_title = title_elem.inner_text().strip() if title_elem else f"餐點 {idx}"

                    # --- 價格 ---
                    sale_price_elem = item.query_selector(".price ins .woocommerce-Price-amount")
                    regular_price_elem = item.query_selector(".price .woocommerce-Price-amount")

                    price = 0
                    original_price = None
                    if sale_price_elem:
                        sale_text = sale_price_elem.inner_text().strip()
                        match = re.search(r"[\d,]+", sale_text)
                        price = int(match.group().replace(",", "")) if match else 0
                        orig_elem = item.query_selector(".price del .woocommerce-Price-amount")
                        if orig_elem:
                            orig_text = orig_elem.inner_text().strip()
                            orig_match = re.search(r"[\d,]+", orig_text)
                            if orig_match:
                                original_price = int(orig_match.group().replace(",", ""))
                    elif regular_price_elem:
                        price_text = regular_price_elem.inner_text().strip()
                        match = re.search(r"[\d,]+", price_text)
                        price = int(match.group().replace(",", "")) if match else 0

                    if price <= 0:
                        print(f"    ⏭️ 略過無標價/價格為0商品: {raw_title[:40]}")
                        continue

                    # --- 圖片 ---
                    img_elem = item.query_selector(".astra-shop-thumbnail-wrap img")
                    img_url = img_elem.get_attribute("src") if img_elem else ""

                    # --- WooCommerce product ID ---
                    product_id = int(item.query_selector("[data-product_id]").get_attribute("data-product_id") if item.query_selector("[data-product_id]") else 0)

                    # --- 真實品名翻譯字典 (覆蓋官網的行銷術語) ---
                    NAME_OVERRIDES = {
                        69395: "泰式檸檬雞腿排",
                        11331: "港式蔥油雞飯",
                        83669: "紅燒無骨滷排飯",
                        91945: "川味家常肉絲飯",
                        98414: "炙火炭烤戰斧豬排",
                        103396: "豆鼓蒸嫩排飯",
                        16931: "明火炭烤挪威鯖魚飯",
                        13058: "菩提蔬食便當（蛋奶素）",
                        14626: "超夯酥炸大雞腿飯",
                        54520: "碳烤溫體大雞腿",
                        95253: "西西里島厚切里肌嫩豬扒",
                        102656: "椒麻腱子肉飯",
                    }

                    # --- 判斷商品名稱與類型 ---
                    if product_id in NAME_OVERRIDES:
                        short_name = NAME_OVERRIDES[product_id]
                        raw_title = short_name # 同步改寫 raw_title 讓後方的蛋白質猜測也能精準命中
                    else:
                        # ── Step 0: 呼叫 Gemini AI 自動判斷菜名 ──────────────────────────
                        ai_result = _ai_extract_meal_name(raw_title, price)

                        if ai_result == "":
                            # AI 明確判定為廣告/公告/非餐點，直接略過此商品
                            print(f"    ⏭️ 略過非餐點商品: {raw_title[:40]}")
                            continue

                        if ai_result:
                            # AI 成功解析出菜名，直接採用
                            short_name = ai_result
                        else:
                            # AI 失敗 / 無 API Key → fallback 到規則解析
                            # ── Step 1: 優先提取括號中的真實菜名 ────────────────────────
                            bracket_match = re.search(r'【([^】]+)】', raw_title)
                            slash_match   = re.search(r'/\*([^*]+)\*/', raw_title)
                            tilde_match   = re.search(r'~~([^~]+)~~', raw_title)

                            if bracket_match:
                                short_name = bracket_match.group(1).strip()
                            elif slash_match:
                                short_name = slash_match.group(1).strip()
                            elif tilde_match:
                                short_name = tilde_match.group(1).strip()
                            else:
                                # ── Step 2: 移除開頭行銷前綴詞及後方連接符 ──────────────
                                _MARKETING_PREFIXES = (
                                    r'最新商品', r'熱銷推薦', r'熱門推薦', r'今日推薦',
                                    r'限時特賣', r'超夯熱銷', r'每日精選', r'本日特餐',
                                )
                                _prefix_pat = re.compile(
                                    r'^(?:' + '|'.join(_MARKETING_PREFIXES) + r')'
                                    r'\s*[-~|/＊＋★•·]\s*',
                                    re.UNICODE
                                )
                                cleaned = _prefix_pat.sub('', raw_title).strip()

                                # ── Step 3: 切段後選最佳段 ───────────────────────────────
                                _MARKETING_WORDS = {'最新商品', '熱銷推薦', '熱門推薦', '今日推薦',
                                                    '限時特賣', '超夯熱銷', '每日精選', '本日特餐'}
                                parts = re.split(r'[-~|/]+', cleaned)
                                valid_parts = [
                                    p.strip('/*! ').strip()
                                    for p in parts
                                    if p.strip('/*! ').strip() and p.strip() not in _MARKETING_WORDS
                                ]
                                if valid_parts:
                                    short_name = max(valid_parts, key=len)
                                else:
                                    short_name = cleaned.strip('/*! ').strip() or raw_title[:30]

                    # 簡單猜測蛋白質含量，因為官網沒有標示
                    protein = 25
                    if "大雞腿" in raw_title: protein = 45
                    elif "雞腿" in raw_title or "雞胸" in raw_title: protein = 35
                    elif "鮭魚" in raw_title or "鱸魚" in raw_title: protein = 28
                    elif "豬排" in raw_title or "排骨" in raw_title: protein = 30
                    elif "蔬食" in raw_title or "素" in raw_title: protein = 15

                    # --- 建立資料項 (取消缺貨判斷，全數預設為可供點餐) ---
                    entry = {
                        "id": idx,
                        "name": short_name if len(short_name) > 2 else raw_title[:30],
                        "price": price,
                        "protein": protein,
                        "image": img_url,
                        "tags": ["尚琳美味"],
                        "product_id": product_id,
                        "is_available": True
                    }

                    if original_price: entry["original_price"] = original_price
                    if "sale" in (item.get_attribute("class") or ""): entry["tags"].append("特價")
                    
                    # 判斷是否為輕食低負擔 (嚴格定義：清蒸、蔬食/素食、或低油脂魚類；排除炸物、滷排、油蔥、重油肉類)
                    is_heavy = any(k in short_name for k in ["炸", "油蔥", "滷排", "豬扒", "肉絲", "大雞腿"])
                    is_light = ("蒸" in short_name or "素" in short_name or "蔬" in short_name or "鯖魚" in short_name or "魚" in short_name)
                    if is_light and not is_heavy:
                        entry["tags"].append("light")
                        
                    # 加入肉類標籤供 AI 推薦系統篩選
                    if "雞" in short_name: 
                        entry["tags"].append("chicken")
                    elif "豬" in short_name or "排骨" in short_name or "肉絲" in short_name or "滷排" in short_name or "嫩排" in short_name or "豬扒" in short_name: 
                        entry["tags"].append("pork")
                    elif "魚" in short_name or "海鮮" in short_name or "鯖魚" in short_name: 
                        entry["tags"].append("seafood")
                    elif "素" in short_name or "蔬" in short_name: 
                        entry["tags"].append("veggie")

                    menu_data.append(entry)
                    print(f"  [{idx:02d}] {entry['name'][:20]:20s} NT${price:>5d}")

            browser.close()

        # 將真實資料寫入 menu.json
        out_file = Path(__file__).parent / "menu.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(menu_data, f, ensure_ascii=False, indent=2)

        print(f"\n{'=' * 60}")
        print(f"🎉 成功爬取 {len(menu_data)} 項餐點！")
        print(f"📁 已儲存至 {out_file}")
        print(f"{'=' * 60}")

        # 自動執行 Gemini AI 營養精算
        try:
            import subprocess
            print("\n🤖 正在調用 Gemini AI 進行精確蛋白質與熱量分析...")
            analyzer_script = Path(__file__).parent / "analyze_menu_protein.py"
            if analyzer_script.exists():
                subprocess.run([sys.executable, str(analyzer_script)], check=False)
        except Exception as ai_err:
            print(f"AI Nutrition Analysis Warning: {ai_err}")

        return menu_data
    except Exception as e:
        print(f"Crawler failed: {e}")
        return []

if __name__ == "__main__":
    crawl_slk9898()
