# /// script
# dependencies = [
#   "playwright",
#   "fastapi",
#   "uvicorn",
#   "sqlalchemy",
#   "pymysql",
#   "pydantic"
# ]
# ///

import os
import sys
import time
import json
import asyncio
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse, parse_qs
import hashlib
import requests
import crawler

# ==========================================
# 0. Load .env file at startup
# ==========================================
def load_env_file():
    """在伺服器啟動時加載 .env 文件"""
    env_file = Path(__file__).resolve().parent / ".env"
    if env_file.exists():
        try:
            with open(env_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        # 只設置不存在的環境變數（優先使用已有的系統環境變數）
                        if key and not os.environ.get(key):
                            os.environ[key] = value
            print(f"✅ 已加載 .env 文件: {env_file}")
        except Exception as e:
            print(f"⚠️ 加載 .env 文件失敗: {e}")
    else:
        print(f"⚠️ 找不到 .env 文件: {env_file}")

# 在導入其他模組前加載環境變數
load_env_file()

# Enforce UTF-8 encoding for standard streams
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

PORT = 8000
DIRECTORY = Path(__file__).resolve().parent
menu_cache = []
menu_cache_lock = threading.Lock()
admin_alert_cache = []
admin_alert_cache_lock = threading.Lock()
group_recommendation_cache = []
group_recommendation_cache_lock = threading.Lock()

def get_menu_cache():
    with menu_cache_lock:
        return [dict(item) for item in menu_cache]

def log_debug(message):
    try:
        log_file = DIRECTORY / "playwright_debug.log"
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except Exception as e:
        print(f"Failed to write log: {e}")

# ==========================================
# 1. Database Setup (MySQL with SQLite Fallback)
# ==========================================
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

class EmployeeDB(Base):
    __tablename__ = "employees"

    id = Column(String(50), primary_key=True, index=True)
    name = Column(String(100), nullable=False)
    password_hash = Column(String(100), nullable=False, default="")
    created_at = Column(DateTime, default=datetime.utcnow)

class OrderDB(Base):
    __tablename__ = "employee_orders"

    id = Column(Integer, primary_key=True, index=True)
    order_code = Column(String(100), unique=True, index=True)
    employee_id = Column(String(50), ForeignKey("employees.id"), nullable=False)
    worker_id = Column(String(50), nullable=False)
    order_date = Column(String(20), nullable=False)
    order_time = Column(String(20), nullable=False)
    meals_json = Column(Text, nullable=False)
    total_price = Column(Integer, nullable=False)
    total_protein = Column(Integer, nullable=False)
    has_ordered_today = Column(Boolean, default=True)
    subsidy_applied = Column(Integer, default=30)
    status = Column(String(50), default="已下單")
    created_at = Column(DateTime, default=datetime.utcnow)

def log_admin_alert(alert_type: str, details: str, worker_id: str = ""):
    global admin_alert_cache
    new_alert = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": alert_type,
        "worker_id": worker_id,
        "details": details,
        "status": "UNREAD"
    }
    try:
        with admin_alert_cache_lock:
            admin_alert_cache = [new_alert] + admin_alert_cache[:49]
    except Exception as e:
        log_debug(f"Failed to log admin alert: {e}")

def get_db_engine():
    mysql_url = os.getenv("MYSQL_URL", "mysql+pymysql://root:password@localhost:3306/slk_orders")
    sqlite_url = f"sqlite:///{DIRECTORY / 'orders.db'}"
    try:
        engine = create_engine(mysql_url, pool_pre_ping=True)
        with engine.connect() as conn:
            pass
        log_debug("Connected to MySQL Database.")
        print("✅ 成功連線至 MySQL 資料庫！")
        return engine
    except Exception as e:
        log_debug(f"MySQL connection failed ({e}). Falling back to SQLite.")
        print("ℹ️ 未偵測到 MySQL 或連線失敗，已自動啟用 SQLite 資料庫備援。")
        engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        return engine

engine = get_db_engine()
Base.metadata.create_all(bind=engine)

def migrate_db_columns(engine):
    """防呆自動升級資料庫欄位，若舊 orders.db 缺欄位則自動增補"""
    from sqlalchemy import inspect, text
    try:
        inspector = inspect(engine)
        if "employee_orders" in inspector.get_table_names():
            columns = [c["name"] for c in inspector.get_columns("employee_orders")]
            with engine.connect() as conn:
                if "has_ordered_today" not in columns:
                    log_debug("Migrating DB: Adding column 'has_ordered_today'...")
                    conn.execute(text("ALTER TABLE employee_orders ADD COLUMN has_ordered_today BOOLEAN DEFAULT 1"))
                    conn.commit()
                if "subsidy_applied" not in columns:
                    log_debug("Migrating DB: Adding column 'subsidy_applied'...")
                    conn.execute(text("ALTER TABLE employee_orders ADD COLUMN subsidy_applied INTEGER DEFAULT 30"))
                    conn.commit()
            log_debug("Database schema migration completed.")
    except Exception as e:
        log_debug(f"DB Migration tip: {e}")

migrate_db_columns(engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def check_employee_subsidy_status(emp_id: str):
    """檢查工號今日是否已下過單，以限制每日一次 $30 員工補助"""
    db = SessionLocal()
    try:
        today_str = datetime.now().strftime("%Y-%m-%d")
        existing_order = db.query(OrderDB).filter(
            OrderDB.worker_id == emp_id,
            OrderDB.order_date == today_str
        ).first()

        if existing_order:
            return {
                "has_ordered_today": True,
                "eligible_for_subsidy": False,
                "subsidy_amount": 0,
                "message": f"工號 {emp_id} 今日已有點餐紀錄，本次訂單不適用 $30 補助。"
            }
        else:
            return {
                "has_ordered_today": False,
                "eligible_for_subsidy": True,
                "subsidy_amount": 30,
                "message": f"工號 {emp_id} 今日首次點餐，享有 $30 員工補助！"
            }
    finally:
        db.close()

def save_order_to_db(employee_id: str, items: list, total_price: int, total_protein: int, status: str = "已下單"):
    db = SessionLocal()
    try:
        emp_id = (employee_id or "11112345").strip()
        now = datetime.now()
        # order_date 永遠存今天（使用者下單當天），供前端今日點餐紀錄使用
        # Playwright 刪除時會另外判斷尚琳官網的日期（超過 13:00 則查隔天）
        order_date = now.strftime("%Y-%m-%d")
        order_time = now.strftime("%H:%M:%S")

        # 1. 自動檢查並補齊/建立員工紀錄，避免外鍵約束失敗
        emp = db.query(EmployeeDB).filter(EmployeeDB.id == emp_id).first()
        if not emp:
            log_debug(f"Employee {emp_id} not found in DB. Auto-creating employee record...")
            new_emp = EmployeeDB(id=emp_id, name=f"員工 ({emp_id})")
            db.add(new_emp)
            db.commit()
            log_debug(f"Successfully auto-created employee {emp_id} in DB.")

        # 2. 檢查重複下單風險（每日補助一次限制）
        existing_today = db.query(OrderDB).filter(
            OrderDB.worker_id == emp_id,
            OrderDB.order_date == order_date
        ).first()

        has_ordered = True if existing_today else False
        subsidy_amount = 0 if has_ordered else 30

        order_code = f"ORD-{now.strftime('%Y%m%d%H%M%S')}-{emp_id}"

        new_order = OrderDB(
            order_code=order_code,
            employee_id=emp_id,
            worker_id=emp_id,
            order_date=order_date,
            order_time=order_time,
            meals_json=json.dumps(items, ensure_ascii=False),
            total_price=total_price,
            total_protein=total_protein,
            has_ordered_today=True,
            subsidy_applied=subsidy_amount,
            status=status
        )

        # 3. 寫入與 Commit / Refresh
        db.add(new_order)
        db.commit()          # 確保寫入提交
        db.refresh(new_order) # 重新整理取得資料庫產生的 ID
        
        log_debug(f"Order saved to DB successfully: ID={new_order.id}, Code={new_order.order_code}, Employee={emp_id}, SubsidyApplied=${subsidy_amount}")
        return new_order
    except Exception as e:
        db.rollback()
        log_debug(f"Database commit failed: {e}\n{traceback.format_exc()}")
        raise e
    finally:
        db.close()

PRODUCT_NAME_ID_MAP = [
    ("檸檬雞", 69395),
    ("蔥油雞", 11331),
    ("滷排", 83669),
    ("紅燒", 83669),
    ("排骨", 83669),
    ("肉絲", 91945),
    ("戰斧", 98414),
    ("蔬食", 13058),
    ("素食", 13058),
    ("鯖魚", 16931),
    ("碳烤", 54520),
    ("雞腿", 14626),
    ("豬扒", 95253),
    ("豆鼓", 103396),
    ("蒸嫩排", 103396),
]

def get_product_id_for_item(item):
    if isinstance(item, dict):
        if item.get("product_id"):
            return item.get("product_id")
        name = item.get("name", "")
    elif isinstance(item, str):
        name = item
    else:
        name = ""

    for key, pid in PRODUCT_NAME_ID_MAP:
        if key in name:
            return pid
    return 14626

def send_teams_notification(worker_id, order_code, items_dict, total_price, action_type="ORDER_SUCCESS"):
    teams_url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if not teams_url:
        return

    try:
        import requests
        if isinstance(items_dict, list):
            items_summary = ", ".join([f"{it.get('name', '餐點')} x{it.get('qty', 1)}" for it in items_dict])
        elif isinstance(items_dict, str):
            try:
                parsed_items = json.loads(items_dict)
                items_summary = ", ".join([f"{it.get('name', '餐點')} x{it.get('qty', 1)}" for it in parsed_items])
            except Exception:
                items_summary = items_dict
        else:
            items_summary = str(items_dict)
        
        if action_type == "ORDER_SUCCESS":
            title = f"✅ 工號 {worker_id} 下單成功！"
            color = "Good"
            status_text = "✅ 已成功下單並轉送官網"
        else:
            title = "🗑️ 尚琳廚苑 - 訂單取消通知"
            color = "Attention"
            status_text = "⚠️ 訂單已取消"

        card = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptivecard.json",
                        "type": "AdaptiveCard",
                        "version": "1.2",
                        "body": [
                            {
                                "type": "TextBlock",
                                "text": title,
                                "weight": "Bolder",
                                "size": "Medium",
                                "color": color
                            },
                            {
                                "type": "FactSet",
                                "facts": [
                                    {"title": "取餐工號", "value": str(worker_id)},
                                    {"title": "訂單單號", "value": str(order_code)},
                                    {"title": "訂購餐點", "value": items_summary},
                                    {"title": "總金額", "value": f"NT$ {total_price}"},
                                    {"title": "處理狀態", "value": status_text}
                                ]
                            }
                        ]
                    }
                }
            ]
        }
        res = requests.post(teams_url, json=card, headers={"Content-Type": "application/json"}, timeout=10)
        log_debug(f"📡 Teams 通知發送狀態 ({res.status_code}): {action_type} - 工號 {worker_id}")
    except Exception as e:
        log_debug(f"⚠️ 發送 Teams 通知失敗: {e}")

# ==========================================
# 2. Playwright Automation Runner
# ==========================================
def run_playwright(checkout_url, worker_id, items_list=None, product_id=None, quantity=1, order_code=None, total_price=0):
    log_debug(f"run_playwright thread started. URL: {checkout_url}, ID: {worker_id}, items: {items_list}")
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    except Exception as e:
        log_debug(f"Event loop setup warning: {e}")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled"
                ]
            )
            context = browser.new_context(no_viewport=True)
            page = context.new_page()
            page.bring_to_front()

            # 步驟 1：依序開啟尚琳各餐點產品頁面，並在單品頁自動輸入「請輸入訂餐工號或貴賓名稱」以套用員工補助
            if items_list and len(items_list) > 0:
                for item in items_list:
                    pid = get_product_id_for_item(item)
                    qty = item.get("qty", 1) if isinstance(item, dict) else 1
                    item_name = item.get("name", "") if isinstance(item, dict) else str(item)
                    
                    product_page_url = f"https://www.slk9898.com.tw/?p={pid}"
                    log_debug(f"[步驟 1] 開啟單品頁面以帶入工號與套用員工補助: {item_name} (ID: {pid}) -> {product_page_url}")
                    page.goto(product_page_url, wait_until="domcontentloaded", timeout=60000)
                    time.sleep(1.5)

                    # 自動將工號填入單品頁面的「請輸入訂餐工號或貴賓名稱」欄位 (YITH WAPO 插件欄位)
                    try:
                        wapo_inputs = page.locator("form.cart input[type='text'], input[name*='yith_wapo'], input[id*='yith-wapo'], div.product input[type='text']").all()
                        for winp in wapo_inputs:
                            if winp.is_visible():
                                winp.fill(worker_id)
                                log_debug(f"✅ 已成功將工號 {worker_id} 自動填入單品頁『請輸入訂餐工號或貴賓名稱』欄位")
                    except Exception as wapo_err:
                        log_debug(f"WAPO autofill info: {wapo_err}")

                    time.sleep(0.5)

                    # 按單品頁面的「加入購物車」按鈕
                    add_cart_btn = page.locator("button.single_add_to_cart_button, button:has-text('加入購物車'), form.cart button[type='submit']").first
                    if add_cart_btn.count() > 0 and add_cart_btn.is_visible():
                        log_debug(f"[步驟 1] 按下單品頁『加入購物車』按鈕...")
                        add_cart_btn.click(timeout=10000)
                        time.sleep(2)
            else:
                pid = product_id if product_id else 14626
                product_page_url = f"https://www.slk9898.com.tw/?p={pid}"
                log_debug(f"[步驟 1] 開啟單品頁面: {product_page_url}")
                page.goto(product_page_url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(1.5)
                try:
                    wapo_inputs = page.locator("form.cart input[type='text'], input[name*='yith_wapo'], input[id*='yith-wapo'], div.product input[type='text']").all()
                    for winp in wapo_inputs:
                        if winp.is_visible():
                            winp.fill(worker_id)
                            log_debug(f"✅ 已成功將工號 {worker_id} 自動填入單品頁『請輸入訂餐工號或貴賓名稱』欄位")
                except Exception:
                    pass
                add_cart_btn = page.locator("button.single_add_to_cart_button, button:has-text('加入購物車'), form.cart button[type='submit']").first
                if add_cart_btn.count() > 0 and add_cart_btn.is_visible():
                    add_cart_btn.click(timeout=10000)
                    time.sleep(2)

            # 步驟 2：在此購物車頁面自動按下「前往結帳 / 立即前往訂餐」
            log_debug("[步驟 2] 準備在此購物車頁面按下『前往結帳』按鈕...")
            time.sleep(1)
            page.evaluate("window.scrollTo(0, 1000)") # 向下滾動確保「購物車總計」與「前往結帳」按鈕可被可視與定位
            time.sleep(1)

            # 定位「前往結帳」/「立即前往結帳」按鈕元素
            checkout_btn = page.locator("a.checkout-button, a[href*='checkout'], a[href*='%e7%b5%90%e5%b7%b3'], .wc-proceed-to-checkout a, a:has-text('立即前往結帳'), a:has-text('前往結帳'), a:has-text('結帳')").first
            
            if checkout_btn.count() > 0:
                try:
                    log_debug("[步驟 2] 找到『前往結帳』按鈕，模擬點擊中...")
                    checkout_btn.scroll_into_view_if_needed()
                    checkout_btn.click(force=True, timeout=8000)
                    log_debug("[步驟 2] ✅ 已成功按下『前往結帳』按鈕！")
                except Exception as e:
                    log_debug(f"[步驟 2] 點擊按鈕提示: {e}")

            time.sleep(2)
            # 若點擊後仍留在購物車頁面，自動導向乾淨結帳頁面 https://www.slk9898.com.tw/checkout/
            current_url = page.url
            if "checkout" not in current_url and "%e7%b5%90%e5%b7%b3" not in current_url:
                log_debug(f"[步驟 2] 網址 ({current_url}) 仍在購物車，自動跳轉至乾淨結帳頁: https://www.slk9898.com.tw/checkout/")
                page.goto("https://www.slk9898.com.tw/checkout/", wait_until="domcontentloaded", timeout=60000)

            time.sleep(2)

            # 步驟 3：跳轉至結帳頁面後，自動將工號填入「取餐員工工號」與相關欄位
            log_debug(f"[步驟 3] 於結帳頁面自動將綁定工號輸入至欄位: {worker_id}")
            try:
                target_selectors = [
                    "#pickup_employee_id",
                    "#employee_id",
                    "#billing_employee_id",
                    "input[name='pickup_employee_id']",
                    "input[name='employee_id']",
                    "input[placeholder*='工號']",
                    "input[placeholder*='取餐']",
                    "#billing_first_name",
                    "#billing_company",
                    "#order_comments"
                ]

                for sel in target_selectors:
                    elem = page.locator(sel)
                    if elem.count() > 0:
                        for i in range(elem.count()):
                            try:
                                target_item = elem.nth(i)
                                if target_item.is_visible():
                                    is_readonly = target_item.get_attribute("readonly") is not None
                                    if not is_readonly:
                                        target_item.fill(worker_id)
                                        log_debug(f"✅ 已成功將取餐工號 {worker_id} 自動填入: {sel}")
                                    else:
                                        log_debug(f"ℹ️ 欄位 {sel} 已自動代入並設為唯讀")
                            except Exception as ex:
                                log_debug(f"Fill warning ({sel}): {ex}")

                # 以標籤文字 (Label) 進行雙重精準定位：「取餐員工工號」 / 「工號」
                try:
                    label_inputs = page.locator("label:has-text('工號'), label:has-text('取餐'), label:has-text('員工')").locator("..").locator("input, textarea")
                    if label_inputs.count() > 0:
                        for i in range(label_inputs.count()):
                            inp = label_inputs.nth(i)
                            if inp.is_visible() and inp.get_attribute("readonly") is None:
                                inp.fill(worker_id)
                                log_debug(f"✅ 已透過標籤定位將取餐工號 {worker_id} 自動填入欄位")
                except Exception as label_ex:
                    log_debug(f"Label locator info: {label_ex}")

                page.evaluate("window.scrollTo(0, 450)")
                log_debug("🚀 Playwright 已自動完成選菜、帶入工號與跳轉結帳頁，準備自動按下『下訂單』按鈕...")
            except Exception as e:
                log_debug(f"Autofill info: {e}")

            # 步驟 4：自動按下「下訂單」按鈕完成最終結帳
            success_status = False
            fail_reason = "尚琳官網目前非開放點餐時段，或結帳按鈕無法點擊"
            try:
                time.sleep(2)
                place_order_btn = page.locator("#place_order, button[name='woocommerce_checkout_place_order'], button:has-text('下單購買'), button:has-text('下訂單'), input[name='woocommerce_checkout_place_order']").first
                if place_order_btn.count() > 0:
                    place_order_btn.scroll_into_view_if_needed()
                    time.sleep(1)
                    place_order_btn.click(force=True, timeout=15000)
                    log_debug(f"✅ [步驟 4] 已自動按下『下訂單』按鈕！工號 {worker_id} 的訂單已送出！")
                    time.sleep(5)  # 等待訂單處理完成
                    final_url = page.url
                    log_debug(f"✅ 訂單流程全部完成！最終頁面網址: {final_url}")

                    # 檢查是否真正進入尚琳官網訂單完成頁 (order-received)
                    page_text = page.inner_text("body") if page else ""
                    if "order-received" in final_url or "order_received" in final_url or "已經收到您的訂單" in page_text or "收到您的訂單" in page_text:
                        code_str = order_code if order_code else f"ORD-{worker_id}"
                        send_teams_notification(worker_id, code_str, items_list, total_price, "ORDER_SUCCESS")
                        success_status = True
                        fail_reason = "OK"
                    else:
                        log_debug(f"⚠️ 未能確認完成訂單頁，可能為非開放點餐時段: {final_url}")
                        fail_reason = "尚琳官網目前非開放點餐時段，或訂單尚未成立"
                else:
                    log_debug("⚠️ [步驟 4] 找不到或無法點擊『下訂單』按鈕（可能非開放點餐時段）")
                    fail_reason = "尚琳官網目前非開放點餐時段（結帳按鈕未開啟）"
            except Exception as e:
                log_debug(f"⚠️ [步驟 4] 按下『下訂單』按鈕時發生錯誤: {e}")
                fail_reason = f"尚琳官網目前非開放點餐時段或下單異常"

            browser.close()
            log_debug(f"🏁 Playwright 瀏覽器已自動關閉，下單結果: {success_status}")
            return success_status, fail_reason
    except Exception as e:
        err_msg = f"Playwright Exception (DOM結構可能已變更或逾時): {e}"
        log_debug(f"{err_msg}\n{traceback.format_exc()}")
        # 發送告警通知至管理者日誌檔
        log_admin_alert(
            alert_type="DOM_STRUCTURE_CHANGE_WARNING",
            details=f"尚琳官網 Playwright 定位異常: {str(e)}。已自動引導員工直連官網手動填單。",
            worker_id=worker_id
        )
        return False, "尚琳官網目前非開放點餐時段或連線異常"

def run_playwright_delete_order(worker_id, order_date=None):
    log_debug(f"run_playwright_delete_order thread started. Worker ID: {worker_id}, Date: {order_date}")
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    except Exception as e:
        log_debug(f"Event loop setup warning: {e}")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            context = browser.new_context(no_viewport=True)
            page = context.new_page()

            # 自動接受所有彈出的 confirm 確認對話框
            page.on("dialog", lambda dialog: dialog.accept())

            query_url = "https://www.slk9898.com.tw/%e8%a8%82%e9%a4%90%e6%9f%a5%e8%a9%a2/"
            log_debug(f"[取消流程] 開啟尚琳訂餐查詢頁面: {query_url}")
            page.goto(query_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)

            # 填入工號
            if page.locator("#search-order-id").count() > 0:
                page.locator("#search-order-id").fill(str(worker_id))
                log_debug(f"[取消流程] 已輸入工號: {worker_id}")

            # 填入查詢日期；訂單資料已保存實際下單日期，因此不自行改成隔天。
            if order_date:
                target_date = order_date
            else:
                now = datetime.now()
                target_date = now.strftime("%Y-%m-%d")

            page.locator("#start-date").fill(target_date)
            if page.locator("#end-date").count() > 0:
                page.locator("#end-date").fill(target_date)
            log_debug(f"[取消流程] 已選擇查詢日期: {target_date}")

            time.sleep(0.5)

            # 按下「查詢」按鈕
            search_btn = page.locator("#search-order-btn").first
            if search_btn.count() > 0:
                search_btn.click(force=True)
                log_debug("[取消流程] 已按下『查詢』按鈕，等待搜尋結果...")
                page.wait_for_timeout(4000)

            # 尚琳頁面會在查詢完成後才動態產生刪除按鈕，不能在查詢前快照元素。
            delete_selector = ".delete-item-btn, .delete-order-btn, [id*='delete'], button:has-text('刪'), button:has-text('刪除'), a:has-text('刪除')"
            delete_btns = page.locator(delete_selector)
            delete_count = delete_btns.count()
            if delete_count > 0:
                log_debug(f"[取消流程] 找到 {delete_count} 個刪除按鈕，準備依序自動刪除...")
                for index in range(delete_count):
                    try:
                        btn = page.locator(delete_selector).nth(index)
                        if btn.is_visible():
                            btn.click(force=True)
                            log_debug(f"✅ [取消流程] 已點擊尚琳官網『刪除』按鈕！")
                            page.wait_for_timeout(2000)
                    except Exception as btn_err:
                        log_debug(f"Click delete btn info: {btn_err}")
                remaining = page.locator(delete_selector).count()
                success = remaining < delete_count
                if success:
                    log_debug(f"🎉 [取消流程] 已成功於尚琳官網『訂餐查詢』自動刪除工號 {worker_id} 的訂單！")
                else:
                    log_debug("⚠️ [取消流程] 刪除按鈕點擊後仍存在，無法確認尚琳官網刪除成功")
            else:
                log_debug(f"ℹ️ [取消流程] 未在尚琳官網搜尋結果中找到可刪除的按鈕（可能已過可取消時段或無訂單）")
                success = False

            browser.close()
            return success
    except Exception as e:
        log_debug(f"run_playwright_delete_order error: {e}\n{traceback.format_exc()}")
        return False

# ==========================================
# 3. FastAPI Server Setup
# ==========================================
try:
    from fastapi import FastAPI, HTTPException, Response
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse
    from pydantic import BaseModel

    app = FastAPI(title="SLK Order Database & Playwright API")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/")
    async def serve_index():
        return FileResponse(DIRECTORY / "index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    @app.get("/api/menu")
    async def get_menu():
        return get_menu_cache()

    @app.get("/api/subsidy_status")
    async def get_subsidy_status(worker_id: str):
        return check_employee_subsidy_status(worker_id)

    @app.get("/api/admin/alerts")
    async def get_admin_alerts():
        with admin_alert_cache_lock:
            alerts = [dict(alert) for alert in admin_alert_cache]
        return {"status": "success", "alerts": alerts}

    class OrderItemSchema(BaseModel):
        id: Optional[int] = 1
        name: str
        price: int
        protein: int
        qty: int = 1
        product_id: Optional[int] = None

    class OrderCreateSchema(BaseModel):
        employee_id: Optional[str] = None
        worker_id: Optional[str] = None
        items: List[OrderItemSchema]
        total_price: int
        total_protein: int

        def get_employee_id(self) -> str:
            return (self.employee_id or self.worker_id or "11112345").strip()

    class AuthRequest(BaseModel):
        employee_id: str
        password: str
        name: Optional[str] = None

    def hash_password(password: str) -> str:
        return hashlib.sha256(password.encode('utf-8')).hexdigest()

    @app.post("/api/register")
    async def register(req: AuthRequest):
        db = SessionLocal()
        try:
            emp_id = req.employee_id.strip()
            existing = db.query(EmployeeDB).filter(EmployeeDB.id == emp_id).first()
            if existing:
                if existing.password_hash:
                    raise HTTPException(status_code=400, detail="工號已註冊過")
                else:
                    existing.password_hash = hash_password(req.password)
                    if req.name:
                        existing.name = req.name
                    db.commit()
                    return {"status": "success", "message": "註冊成功，已為現有員工設定密碼"}
            else:
                new_emp = EmployeeDB(
                    id=emp_id, 
                    name=req.name or f"員工 ({emp_id})",
                    password_hash=hash_password(req.password)
                )
                db.add(new_emp)
                db.commit()
                return {"status": "success", "message": "新工號註冊成功"}
        finally:
            db.close()

    @app.post("/api/login")
    async def login(req: AuthRequest):
        db = SessionLocal()
        try:
            emp_id = req.employee_id.strip()
            emp = db.query(EmployeeDB).filter(EmployeeDB.id == emp_id).first()
            if not emp:
                raise HTTPException(status_code=401, detail="工號或密碼錯誤")
            if emp.password_hash != hash_password(req.password):
                raise HTTPException(status_code=401, detail="工號或密碼錯誤")
            return {"status": "success", "user": {"id": emp.id, "name": emp.name}}
        finally:
            db.close()

    @app.get("/api/user/me")
    async def get_user_me(employee_id: str):
        db = SessionLocal()
        try:
            emp = db.query(EmployeeDB).filter(EmployeeDB.id == employee_id).first()
            if not emp:
                raise HTTPException(status_code=404, detail="User not found")
            
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            
            # 轉換星期幾 (0=Monday, 6=Sunday)
            weekdays = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]
            today_weekday = weekdays[now.weekday()]

            # 計算今日蛋白質 (排除已取消)
            today_orders = db.query(OrderDB).filter(
                OrderDB.worker_id == employee_id,
                OrderDB.order_date == today_str,
                OrderDB.status != "已取消 (CANCELED)"
            ).all()
            
            today_protein = sum(o.total_protein for o in today_orders)
            
            # 歷史紀錄 (排除已取消)
            all_orders = db.query(OrderDB).filter(
                OrderDB.worker_id == employee_id,
                OrderDB.status != "已取消 (CANCELED)"
            ).order_by(OrderDB.id.desc()).all()
            
            history = []
            for o in all_orders:
                try:
                    items = json.loads(o.meals_json)
                except:
                    items = []
                history.append({
                    "id": o.order_code,
                    "date": o.order_date,
                    "time": o.order_time,
                    "items": items,
                    "totalPrice": o.total_price,
                    "totalProtein": o.total_protein,
                    "status": "已下單"
                })
                
            return {
                "id": emp.id,
                "name": emp.name,
                "today": {
                    "date": today_str,
                    "weekday": today_weekday,
                    "protein": today_protein
                },
                "history": history
            }
        finally:
            db.close()

    @app.get("/api/orders")
    @app.get("/api/history")
    async def get_orders_endpoint(worker_id: Optional[str] = None):
        db = SessionLocal()
        try:
            if worker_id:
                orders = db.query(OrderDB).filter(OrderDB.worker_id == worker_id).order_by(OrderDB.id.desc()).all()
            else:
                orders = db.query(OrderDB).order_by(OrderDB.id.desc()).all()
            
            result = []
            for o in orders:
                result.append({
                    "id": o.id,
                    "order_code": o.order_code,
                    "employee_id": o.employee_id,
                    "worker_id": o.worker_id,
                    "order_date": o.order_date,
                    "order_time": o.order_time,
                    "meals": json.loads(o.meals_json),
                    "total_price": o.total_price,
                    "total_protein": o.total_protein,
                    "status": o.status,
                    "created_at": str(o.created_at)
                })
            return {"status": "success", "orders": result}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            db.close()

    @app.delete("/api/orders/{order_code}")
    async def delete_order_endpoint(order_code: str):
        db = SessionLocal()
        try:
            order = db.query(OrderDB).filter(OrderDB.order_code == order_code).first()
            if not order:
                try:
                    order = db.query(OrderDB).filter(OrderDB.id == int(order_code)).first()
                except ValueError:
                    pass
            if not order:
                raise HTTPException(status_code=404, detail="Order not found")

            # 解析 meals_json
            try:
                items_list = json.loads(order.meals_json)
            except Exception:
                items_list = []

            # 觸發 Playwright 自動前往尚琳官網『訂餐查詢』頁面進行刪除
            log_debug(f"[取消流程] 啟動 Playwright 前往尚琳『訂餐查詢』進行官網刪除: 工號={order.worker_id}, 日期={order.order_date}")
            website_deleted = await asyncio.to_thread(
                run_playwright_delete_order,
                order.worker_id,
                order.order_date
            )
            if not website_deleted:
                raise HTTPException(status_code=502, detail="尚琳官網未確認刪除訂單，未變更本地紀錄")

            # 官網確認刪除後才軟刪除本地紀錄，避免兩邊狀態不一致。
            order.status = "已取消 (CANCELED)"
            db.commit()

            # 發送 Teams 取消通知
            t_teams = threading.Thread(target=send_teams_notification, args=(order.worker_id, order.order_code, items_list, order.total_price, "ORDER_CANCEL"), daemon=False)
            t_teams.start()

            return {"status": "success", "message": "訂單已從尚琳官網刪除並同步取消"}
        except HTTPException:
            raise
        except Exception as e:
            log_debug(f"API Delete Order Exception: {e}\n{traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            db.close()

    @app.post("/api/orders")
    async def create_order_endpoint(order_req: OrderCreateSchema):
        emp_id = order_req.get_employee_id()
        try:
            items_dict = [item.dict() for item in order_req.items]
            
            # 1. 自動檢查/建立員工與訂單寫入 Commit & Refresh
            new_order = save_order_to_db(
                employee_id=emp_id,
                items=items_dict,
                total_price=order_req.total_price,
                total_protein=order_req.total_protein,
                status="已轉至尚琳結帳"
            )

            # 2. 建立尚琳結帳 URL
            import urllib.parse
            encoded_id = urllib.parse.quote(str(emp_id))
            encoded_company = urllib.parse.quote(f"工號:{emp_id}")
            encoded_comments = urllib.parse.quote(f"員工工號:{emp_id}")
            if len(items_dict) == 1:
                first_pid = get_product_id_for_item(items_dict[0])
                qty = items_dict[0].get("qty", 1)
                checkout_url = f"https://www.slk9898.com.tw/checkout/?add-to-cart={first_pid}&quantity={qty}&billing_first_name={encoded_id}&billing_company={encoded_company}&order_comments={encoded_comments}"
            elif len(items_dict) > 1:
                pids = ",".join(str(get_product_id_for_item(it)) for it in items_dict)
                qtys = ",".join(str(it.get("qty", 1)) for it in items_dict)
                checkout_url = f"https://www.slk9898.com.tw/checkout/?add-to-cart={pids}&quantity={qtys}&billing_first_name={encoded_id}&billing_company={encoded_company}&order_comments={encoded_comments}"
            else:
                checkout_url = f"https://www.slk9898.com.tw/checkout/?billing_first_name={encoded_id}&billing_company={encoded_company}&order_comments={encoded_comments}"

            # 3. 觸發 Playwright 自動化流程（模式 B：將購物車餐點加入尚琳購物車 ➔ 自動按下「立即前往結帳」 ➔ 跳轉至結帳頁 ➔ 自動填入工號 ➔ 自動完成下單）
            log_debug(f"[模式 B] 訂單成功落盤 ID={new_order.id}，啟動 Playwright 自動化流程: {emp_id}")
            t = threading.Thread(target=run_playwright, args=(checkout_url, emp_id, items_dict, None, 1, new_order.order_code, order_req.total_price), daemon=False)
            t.start()

            return {
                "status": "success",
                "message": f"訂單已成功寫入資料庫！Playwright 機器人已啟動，將為工號 {emp_id} 完成自動點餐與填單...",
                "order_id": new_order.id,
                "order_code": new_order.order_code,
                "employee_id": emp_id,
                "checkout_url": checkout_url
            }
        except Exception as e:
            log_debug(f"API Orders Exception: {e}\n{traceback.format_exc()}")
            raise HTTPException(status_code=500, detail=f"資料庫寫入失敗: {str(e)}")

    class PhotoAnalyzeRequest(BaseModel):
        image_base64: str
        mime_type: Optional[str] = "image/jpeg"

    @app.post("/api/analyze-photo")
    async def analyze_photo_endpoint(req: PhotoAnalyzeRequest):
        # 從環境變數中讀取金鑰（已在啟動時從 .env 加載）
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        mock_mode = os.environ.get("MOCK_MODE", "false").lower() == "true"
        
        log_debug(f"📸 開始分析照片... (Mock Mode: {mock_mode})")
        
        # 模擬食物分析結果數據庫
        mock_results = {
            "預設": {"name": "健康雞肉沙拉（模擬）", "protein": 28, "calories": 320, "reason": "根據照片中的雞肉和新鮮蔬菜判斷"},
            "雞胸肉": {"name": "烤雞胸肉套餐（模擬）", "protein": 35, "calories": 380, "reason": "高蛋白低脂肪的優質蛋白質來源"},
            "牛肉": {"name": "牛肉飯（模擬）", "protein": 30, "calories": 450, "reason": "含豐富鐵質和蛋白質"},
            "豬肉": {"name": "豬肉咖喱飯（模擬）", "protein": 25, "calories": 520, "reason": "含B群維生素和蛋白質"},
            "魚": {"name": "清蒸魚套餐（模擬）", "protein": 32, "calories": 280, "reason": "Omega-3脂肪酸豐富的健康選擇"},
        }

        try:
            # 如果啟用模擬模式或沒有 API 金鑰，使用模擬結果
            if mock_mode or not api_key:
                log_debug("🎭 使用模擬模式返回結果")
                mock_result = mock_results.get("預設")
                return {
                    "status": "success",
                    "result": mock_result,
                    "mode": "mock"
                }

            import requests
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={api_key}"
            payload = {
                "contents": [{
                    "parts": [
                        { "text": "這是一張食物照片。請仔細分析照片中的餐點，辨識所有食材，並以純粹的 JSON 物件格式回傳分析結果（不需要任何 markdown 或說明文字），JSON 必須包含以下四個欄位：\"name\"(繁體中文餐點名稱), \"protein\"(整數，蛋白質克數), \"calories\"(整數，估計熱量大卡), \"reason\"(繁體中文，簡述判斷根據，約30字)。" },
                        { "inline_data": { "mime_type": req.mime_type or "image/jpeg", "data": req.image_base64 } }
                    ]
                }]
            }

            log_debug(f"📡 向 Gemini API 發送請求...")
            res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
            
            if not res.ok:
                err_json = res.json() if res.content else {}
                err_msg = err_json.get("error", {}).get("message", f"HTTP {res.status_code}")
                log_debug(f"⚠️ Gemini API 返回錯誤 ({res.status_code}): {err_msg}，降級使用模擬數據")
                # 降級到模擬模式而不是拋出錯誤
                mock_result = mock_results.get("預設")
                return {
                    "status": "success",
                    "result": mock_result,
                    "mode": "mock",
                    "note": f"API 失敗，使用模擬數據 (HTTP {res.status_code})"
                }

            data = res.json()
            result_text = data["candidates"][0]["content"]["parts"][0]["text"]
            log_debug(f"✅ Gemini API 響應成功")
            
            import re
            json_match = re.search(r"\{[\s\S]*\}", result_text)
            if not json_match:
                log_debug(f"⚠️ 無法從回應中提取 JSON，降級使用模擬數據")
                # 降級到模擬模式而不是拋出錯誤
                mock_result = mock_results.get("預設")
                return {
                    "status": "success",
                    "result": mock_result,
                    "mode": "mock",
                    "note": "無法解析 AI 回應，使用模擬數據"
                }
            
            parsed = json.loads(json_match.group(0))
            log_debug(f"✅ 成功解析 AI 回應: {parsed}")
            
            return {
                "status": "success",
                "result": {
                    "name": parsed.get("name", "未知餐點"),
                    "protein": int(parsed.get("protein", 0)),
                    "calories": int(parsed.get("calories", 0)),
                    "reason": parsed.get("reason", "AI 分析完成")
                },
                "mode": "real"
            }
        except HTTPException:
            raise
        except Exception as e:
            error_detail = f"{str(e)}\n{traceback.format_exc()}"
            log_debug(f"❌ Gemini Proxy 錯誤: {error_detail}，降級使用模擬數據")
            # 最後的降級方案：總是返回模擬結果而不是錯誤
            mock_result = mock_results.get("預設")
            return {
                "status": "success",
                "result": mock_result,
                "mode": "mock",
                "note": f"分析過程出錯，使用模擬數據: {str(e)}"
            }

except ImportError:
    app = None

# ==========================================
# 4. Standard HTTP Server Fallback
# ==========================================
import http.server

class CustomHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIRECTORY), **kwargs)

    def _set_cors_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')

    def do_GET(self):
        clean_path = self.path.split('?')[0].rstrip('/')
        if clean_path == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
            return
        if clean_path in ['/api/orders', '/api/history']:
            query_components = parse_qs(urlparse(self.path).query)
            worker_id = query_components.get('worker_id', [None])[0] or query_components.get('employee_id', [None])[0]
            
            db = SessionLocal()
            try:
                if worker_id:
                    orders = db.query(OrderDB).filter(OrderDB.worker_id == worker_id).order_by(OrderDB.id.desc()).all()
                else:
                    orders = db.query(OrderDB).order_by(OrderDB.id.desc()).all()
                
                result = []
                for o in orders:
                    result.append({
                        "id": o.id,
                        "order_code": o.order_code,
                        "employee_id": o.employee_id,
                        "worker_id": o.worker_id,
                        "order_date": o.order_date,
                        "order_time": o.order_time,
                        "meals": json.loads(o.meals_json),
                        "total_price": o.total_price,
                        "total_protein": o.total_protein,
                        "status": o.status,
                        "created_at": str(o.created_at)
                    })
                
                payload = {"status": "success", "orders": result}
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self._set_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self._set_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))
                return
            finally:
                db.close()
        else:
            # 靜態網頁檔案服務 (index.html, CSS, JS 檔案)
            super().do_GET()

    def do_POST(self):
        clean_path = self.path.split('?')[0].rstrip('/')
        if clean_path in ['/api/orders', '/api/checkout']:
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            
            try:
                data = json.loads(post_data.decode('utf-8'))
                worker_id = data.get('employee_id') or data.get('worker_id') or data.get('workerId') or "11112345"
                items = data.get('items', [])
                total_price = data.get('total_price') or data.get('totalPrice') or 0
                total_protein = data.get('total_protein') or data.get('totalProtein') or 0

                # 1. 寫入 DB (Commit & Refresh & Auto-create Employee)
                saved_order = save_order_to_db(worker_id, items, total_price, total_protein)

                # 2. 建立 Checkout URL
                checkout_url = data.get('checkout_url')
                if not checkout_url:
                    first_pid = get_product_id_for_item(items[0]) if items else 14626
                    checkout_url = f"https://www.slk9898.com.tw/checkout/?add-to-cart={first_pid}&quantity=1&billing_first_name={worker_id}&billing_company=工號:{worker_id}&order_comments=員工工號:{worker_id}"

                # 3. 啟動 Playwright 腳本
                t = threading.Thread(target=run_playwright, args=(checkout_url, worker_id, items), daemon=True)
                t.start()

                response_payload = {
                    "status": "success",
                    "message": "訂單已成功寫入資料庫，並已觸發 Playwright 填寫尚琳頁面",
                    "order_id": saved_order.id,
                    "order_code": saved_order.order_code,
                    "employee_id": worker_id,
                    "checkout_url": checkout_url
                }

                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self._set_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps(response_payload, ensure_ascii=False).encode('utf-8'))
                return
            except Exception as e:
                log_debug(f"HTTP Handler Exception: {e}\n{traceback.format_exc()}")
                self.send_response(500)
                self.send_header('Content-Type', 'application/json')
                self._set_cors_headers()
                self.end_headers()
                err_resp = {"status": "error", "message": f"資料庫寫入失敗: {str(e)}"}
                self.wfile.write(json.dumps(err_resp, ensure_ascii=False).encode('utf-8'))
                return
        else:
            self.send_response(404)
            self._set_cors_headers()
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

# ==========================================
# 09:30 AM Daily Teams Recommendation & 1-Click Order
# ==========================================
def get_top_meals_last_7_days():
    """查詢過去 7 天內，各工號訂購次數最多的餐點"""
    db = SessionLocal()
    try:
        seven_days_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        orders = db.query(OrderDB).filter(
            OrderDB.order_date >= seven_days_ago,
            ~OrderDB.status.like("%CANCELED%"),
            ~OrderDB.status.like("%取消%")
        ).all()

        worker_meal_counts = {}
        for ord_item in orders:
            w_id = ord_item.worker_id
            if w_id not in worker_meal_counts:
                worker_meal_counts[w_id] = {}
            try:
                meals = json.loads(ord_item.meals_json) if isinstance(ord_item.meals_json, str) else ord_item.meals_json
                if isinstance(meals, list):
                    for m in meals:
                        name = m.get("name", "推薦餐點")
                        qty = m.get("qty", 1)
                        price = m.get("price", 0)
                        if price <= 0:
                            continue
                        protein = m.get("protein", 30)
                        pid = m.get("id") or m.get("product_id")
                        if name not in worker_meal_counts[w_id]:
                            worker_meal_counts[w_id][name] = {"count": 0, "price": price, "protein": protein, "pid": pid}
                        worker_meal_counts[w_id][name]["count"] += qty
            except Exception as e:
                log_debug(f"Parse meals_json error: {e}")

        top_meals = {}
        for w_id, meals in worker_meal_counts.items():
            if meals:
                sorted_m = sorted(meals.items(), key=lambda x: x[1]["count"], reverse=True)
                meal_name, info = sorted_m[0]
                top_meals[w_id] = {
                    "meal_name": meal_name,
                    "count": info["count"],
                    "price": info["price"],
                    "protein": info["protein"],
                    "pid": info["pid"]
                }
        return top_meals
    except Exception as e:
        log_debug(f"get_top_meals_last_7_days error: {e}")
        return {}
    finally:
        db.close()

def send_daily_0930_teams_recommendations():
    """發送 09:30 個人化一週熱門餐點推薦 + 1 鍵點餐按鈕卡片至 Teams"""
    teams_url = os.environ.get("TEAMS_WEBHOOK_URL", "").strip()
    if not teams_url:
        log_debug("⚠️ 未設定 TEAMS_WEBHOOK_URL，跳過 09:30 推薦推送")
        return

    top_meals = get_top_meals_last_7_days()
    if not top_meals:
        log_debug("ℹ️ 過去一週無點餐紀錄，跳過 09:30 推薦推送")
        return

    try:
        import requests, urllib.parse
        facts = []
        actions = []
        host_url = os.environ.get("HOST_URL", "http://localhost:8000")

        for w_id, info in top_meals.items():
            meal_name = info["meal_name"]
            price = info["price"]
            protein = info["protein"]
            facts.append({"title": f"工號 {w_id} 一週最愛", "value": f"{meal_name} (NT$ {price} | {protein}g 蛋白質)"})

            encoded_meal = urllib.parse.quote(meal_name)
            quick_url = f"{host_url}/api/quick-order-web?worker_id={w_id}&meal_name={encoded_meal}&price={price}&protein={protein}"
            actions.append({
                "type": "Action.OpenUrl",
                "title": f"🍱 工號 {w_id} 一鍵下訂【{meal_name}】",
                "url": quick_url
            })

        card = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptivecard.json",
                        "type": "AdaptiveCard",
                        "version": "1.2",
                        "body": [
                            {
                                "type": "TextBlock",
                                "text": "⏰ 早上 09:30 尚琳廚苑 - 每日個人化推薦預訂選單",
                                "weight": "Bolder",
                                "size": "Medium",
                                "color": "Accent"
                            },
                            {
                                "type": "TextBlock",
                                "text": "根據過去一週的點餐紀錄，系統已為您整理出最常點的招牌餐點！點擊下方按鈕即可直接一鍵完成下單：",
                                "wrap": True
                            },
                            {
                                "type": "FactSet",
                                "facts": facts
                            }
                        ],
                        "actions": actions[:5]
                    }
                }
            ]
        }
        res = requests.post(teams_url, json=card, headers={"Content-Type": "application/json"}, timeout=10)
        log_debug(f"📡 09:30 Teams 推薦推播完成 ({res.status_code})")
    except Exception as e:
        log_debug(f"send_daily_0930_teams_recommendations error: {e}")

@app.get("/api/quick-order-web")
async def quick_order_web(worker_id: str, meal_name: str, price: int = 80, protein: int = 34):
    """Teams 點擊一鍵下訂時開啟的網頁/API：自動下單並啟動 Playwright 到尚琳結帳"""
    try:
        items_dict = [{"id": 1, "name": meal_name, "price": price, "protein": protein, "qty": 1}]
        
        # 1. 寫入 DB
        new_order = save_order_to_db(
            employee_id=worker_id,
            items=items_dict,
            total_price=price,
            total_protein=protein,
            status="已轉至尚琳結帳"
        )

        # 2. 建立結帳 URL 與 Playwright 下單
        first_pid = get_product_id_for_item(items_dict[0])
        import urllib.parse
        encoded_id = urllib.parse.quote(str(worker_id))
        encoded_company = urllib.parse.quote(f"工號:{worker_id}")
        encoded_comments = urllib.parse.quote(f"員工工號:{worker_id}")
        checkout_url = f"https://www.slk9898.com.tw/checkout/?add-to-cart={first_pid}&quantity=1&billing_first_name={encoded_id}&billing_company={encoded_company}&order_comments={encoded_comments}"

        # 3. 同步執行 Playwright 尚琳官網下單流程，確認真實成功狀態
        success, reason = await asyncio.to_thread(run_playwright, checkout_url, worker_id, items_dict, None, 1, new_order.order_code, price)

        from fastapi.responses import HTMLResponse

        if success:
            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <title>尚琳廚苑 - 下單成功</title>
                <style>
                    body {{ font-family: sans-serif; background: #0f172a; color: white; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
                    .card {{ background: #1e293b; border-radius: 16px; padding: 40px; text-align: center; max-width: 480px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5); }}
                    .icon {{ font-size: 64px; margin-bottom: 20px; }}
                    h1 {{ color: #4ade80; margin-bottom: 10px; font-size: 24px; }}
                    p {{ color: #94a3b8; line-height: 1.6; font-size: 16px; }}
                </style>
            </head>
            <body>
                <div class="card">
                    <div class="icon">✅</div>
                    <h1>工號 {worker_id} 下單成功！</h1>
                    <p>成功預訂：<strong style="font-size:20px; color:#fff;">{meal_name}</strong> (NT$ {price})</p>
                </div>
            </body>
            </html>
            """
        else:
            # 尚琳官網下單失敗，同步將 DB 中的訂單標記為已取消
            try:
                db = SessionLocal()
                ord_db = db.query(OrderDB).filter(OrderDB.order_code == new_order.order_code).first()
                if ord_db:
                    ord_db.status = "已取消 (非開放點餐時段)"
                    db.commit()
                db.close()
            except Exception:
                pass

            html_content = f"""
            <!DOCTYPE html>
            <html>
            <head>
                <meta charset="utf-8">
                <title>尚琳廚苑 - 下單失敗</title>
                <style>
                    body {{ font-family: sans-serif; background: #0f172a; color: white; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
                    .card {{ background: #1e293b; border-radius: 16px; padding: 40px; text-align: center; max-width: 480px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5); border: 2px solid #ef4444; }}
                    .icon {{ font-size: 64px; margin-bottom: 20px; }}
                    h1 {{ color: #f87171; margin-bottom: 10px; font-size: 24px; }}
                    p {{ color: #cbd5e1; line-height: 1.6; font-size: 16px; }}
                    .reason {{ color: #fbbf24; font-weight: bold; background: #334155; padding: 12px; border-radius: 8px; margin: 15px 0; font-size: 15px; }}
                </style>
            </head>
            <body>
                <div class="card">
                    <div class="icon">❌</div>
                    <h1>工號 {worker_id} 下單失敗</h1>
                    <p>餐點：<strong>{meal_name}</strong></p>
                    <div class="reason">⚠️ {reason}</div>
                    <p style="font-size:14px; color:#94a3b8;">尚琳官網有固定開放點餐時段限制，請於開放時段內再試。</p>
                </div>
            </body>
            </html>
            """
        return HTMLResponse(content=html_content)
    except Exception as e:
        log_debug(f"quick_order_web error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

def get_recent_group_recommendation_history():
    cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    with group_recommendation_cache_lock:
        return [dict(item) for item in group_recommendation_cache if item.get("date", "") >= cutoff]

def save_group_recommendation_history(history_list):
    global group_recommendation_cache
    with group_recommendation_cache_lock:
        group_recommendation_cache = [dict(item) for item in history_list]

def send_daily_0930_group_channel_recommendation():
    """每天 09:30 隨機挑選一週內不重複的主廚推薦餐點，發送至『智饗』群組頻道"""
    channel_url = os.environ.get("TEAMS_CHANNEL_WEBHOOK_URL", "").strip()
    if not channel_url:
        log_debug("⚠️ 未設定 TEAMS_CHANNEL_WEBHOOK_URL，跳過智饗群組 09:30 推薦推送")
        return

    try:
        menu = get_menu_cache()
        if not menu:
            return

        valid_meals = [m for m in menu if m.get("price", 0) > 0 and m.get("is_available", True)]
        if not valid_meals:
            return

        recent_history = get_recent_group_recommendation_history()
        recent_names = {item["name"] for item in recent_history}

        # 篩選過去 7 天尚未推薦過的餐點
        candidates = [m for m in valid_meals if m["name"] not in recent_names]
        if not candidates:
            candidates = valid_meals

        import random, urllib.parse
        selected = random.choice(candidates)
        meal_name = selected["name"]
        price = selected.get("price", 80)
        protein = selected.get("protein", 30)
        calories = selected.get("calories", 650)
        reason = selected.get("protein_breakdown") or "優質蛋白質與均衡美味的主廚特選便當！"

        recent_history.append({"date": datetime.now().strftime("%Y-%m-%d"), "name": meal_name})
        save_group_recommendation_history(recent_history)

        host_url = os.environ.get("HOST_URL", "http://localhost:8000")
        encoded_meal = urllib.parse.quote(meal_name)
        quick_input_url = f"{host_url}/api/quick-order-form?meal_name={encoded_meal}&price={price}&protein={protein}"

        card = {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptivecard.json",
                        "type": "AdaptiveCard",
                        "version": "1.2",
                        "body": [
                            {
                                "type": "TextBlock",
                                "text": f"🍱 今日推薦主餐：{meal_name}",
                                "weight": "Bolder",
                                "size": "Medium"
                            },
                            {
                                "type": "TextBlock",
                                "text": f"原價：NT$ {price}",
                                "weight": "Bolder",
                                "size": "Normal",
                                "color": "Good"
                            }
                        ],
                        "actions": [
                            {
                                "type": "Action.OpenUrl",
                                "title": "🍱 點擊按鈕下訂",
                                "url": quick_input_url
                            }
                        ]
                    }
                }
            ]
        }
        res = requests.post(channel_url, json=card, headers={"Content-Type": "application/json"}, timeout=10)
        log_debug(f"📡 智饗群組頻道 09:30 隨機推薦推播完成 ({res.status_code}): {meal_name}")
    except Exception as e:
        log_debug(f"send_daily_0930_group_channel_recommendation error: {e}")

@app.get("/api/quick-order-form")
async def quick_order_form(meal_name: str, price: int = 80, protein: int = 34):
    """智饗群組點擊推薦餐點時開啟的網頁：供同仁輸入工號一鍵下單"""
    from fastapi.responses import HTMLResponse
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>智饗群組 - 快速點餐</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: sans-serif; background: #0f172a; color: white; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
            .card {{ background: #1e293b; border-radius: 16px; padding: 40px; text-align: center; width: 90%; max-width: 420px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5); }}
            .icon {{ font-size: 56px; margin-bottom: 15px; }}
            h1 {{ color: #38bdf8; margin-bottom: 10px; font-size: 22px; }}
            p {{ color: #94a3b8; font-size: 15px; line-height: 1.5; }}
            input {{ width: 100%; padding: 14px; margin: 20px 0; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: white; font-size: 16px; box-sizing: border-box; text-align: center; }}
            button {{ width: 100%; padding: 14px; border-radius: 8px; border: none; background: #3b82f6; color: white; font-size: 16px; font-weight: bold; cursor: pointer; transition: background 0.2s; }}
            button:hover {{ background: #2563eb; }}
            .meal-tag {{ background: #334155; padding: 8px 16px; border-radius: 20px; color: #fbbf24; font-weight: bold; display: inline-block; margin-top: 5px; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="icon">🍱🌟</div>
            <h1>尚琳廚苑 - 智饗今日推薦餐點</h1>
            <div class="meal-tag">{meal_name} (NT$ {price})</div>
            <form action="/api/quick-order-web" method="get">
                <input type="hidden" name="meal_name" value="{meal_name}">
                <input type="hidden" name="price" value="{price}">
                <input type="hidden" name="protein" value="{protein}">
                <input type="text" name="worker_id" placeholder="請輸入您的取餐工號" required autofocus>
                <button type="submit">🚀 一鍵確認下單</button>
            </form>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/api/trigger-daily-recommendations")
async def trigger_daily_recommendations():
    """手動觸發 09:30 Teams 個人私訊與智饗群組推薦推送測試"""
    t1 = threading.Thread(target=send_daily_0930_teams_recommendations, daemon=False)
    t1.start()
    t2 = threading.Thread(target=send_daily_0930_group_channel_recommendation, daemon=False)
    t2.start()
    return {"status": "success", "message": "已成功同時觸發 09:30 個人私訊與智饗群組頻道推薦推送！"}

def run_daily_0930_scheduler():
    """每天 09:30 自動檢測並發送 Teams 個人與智饗群組推薦選單"""
    last_pushed_date = ""
    while True:
        try:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M")
            if time_str >= "09:30" and last_pushed_date != today_str:
                log_debug(f"⏰ 檢測到時間為 {time_str} 且今日尚未推播！自動補發今日 Teams 個人與智饗群組點餐推薦選單...")
                send_daily_0930_teams_recommendations()
                send_daily_0930_group_channel_recommendation()
                last_pushed_date = today_str
        except Exception as e:
            log_debug(f"0930 Scheduler Exception: {e}")
        time.sleep(30)

# 啟動 09:30 推薦排程執行緒
scheduler_0930_thread = threading.Thread(target=run_daily_0930_scheduler, daemon=True)
scheduler_0930_thread.start()


def run_daily_crawler():
    """每天 09:00 自動爬取尚琳官網菜單並更新記憶體快取。"""
    global menu_cache
    last_crawled_date = ""
    # 啟動時先跑一次，讓前端盡快取得當天菜單。
    try:
        log_debug("🕷️ [Crawler] Server 啟動，立即補跑一次菜單爬蟲...")
        crawled_menu = crawler.crawl_slk9898()
        with menu_cache_lock:
            menu_cache = crawled_menu or []
        last_crawled_date = datetime.now().strftime("%Y-%m-%d")
        log_debug(f"✅ [Crawler] 啟動補跑完成，記憶體菜單已更新 ({len(menu_cache)} 項)")
    except Exception as e:
        log_debug(f"⚠️ [Crawler] 啟動補跑失敗: {e}")

    while True:
        try:
            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            time_str  = now.strftime("%H:%M")
            # 每天 09:00 ~ 09:05 之間觸發（避免重複）
            if time_str >= "09:00" and last_crawled_date != today_str:
                log_debug(f"🕷️ [Crawler] 每日定時 09:00 觸發菜單爬蟲 ({time_str})...")
                crawled_menu = crawler.crawl_slk9898()
                with menu_cache_lock:
                    menu_cache = crawled_menu or []
                last_crawled_date = today_str
                log_debug(f"✅ [Crawler] 每日爬蟲完成，記憶體菜單已更新 ({len(menu_cache)} 項)")
        except Exception as e:
            log_debug(f"⚠️ [Crawler] 排程爬蟲失敗: {e}")
        time.sleep(60)  # 每分鐘檢查一次

# 啟動每日菜單爬蟲背景執行緒
crawler_thread = threading.Thread(target=run_daily_crawler, daemon=True)
crawler_thread.start()

if __name__ == "__main__":
    if app:
        import uvicorn
        log_debug("--- Starting FastAPI Server with Uvicorn ---")
        print(f"🚀 啟動全端 FastAPI 伺服器: http://localhost:{PORT}")
        uvicorn.run(app, host="0.0.0.0", port=PORT)
    else:
        server_address = ('', PORT)
        httpd = http.server.HTTPServer(server_address, CustomHandler)
        log_debug("--- Starting Fallback HTTP Server ---")
        print(f"🚀 啟動 Python Web 伺服器: http://localhost:{PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n關閉伺服器...")
            httpd.server_close()

