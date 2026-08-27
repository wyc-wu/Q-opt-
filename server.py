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
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse, parse_qs
import hashlib
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
    alert_file = DIRECTORY / "admin_alerts.json"
    new_alert = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "type": alert_type,
        "worker_id": worker_id,
        "details": details,
        "status": "UNREAD"
    }
    try:
        alerts = []
        if alert_file.exists():
            with open(alert_file, "r", encoding="utf-8") as f:
                alerts = json.load(f)
        alerts.insert(0, new_alert)
        with open(alert_file, "w", encoding="utf-8") as f:
            json.dump(alerts[:50], f, ensure_ascii=False, indent=2)
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

# ==========================================
# 2. Playwright Automation Runner
# ==========================================
def run_playwright(checkout_url, worker_id, items_list=None, product_id=None, quantity=1):
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
            try:
                time.sleep(2)
                place_order_btn = page.locator("#place_order, button[name='woocommerce_checkout_place_order'], button:has-text('下單購買'), button:has-text('下訂單'), input[name='woocommerce_checkout_place_order']").first
                if place_order_btn.count() > 0:
                    place_order_btn.scroll_into_view_if_needed()
                    time.sleep(1)
                    place_order_btn.click(force=True, timeout=15000)
                    log_debug(f"✅ [步驟 4] 已自動按下『下訂單』按鈕！工號 {worker_id} 的訂單已送出！")
                    time.sleep(5)  # 等待訂單處理完成
                    log_debug(f"✅ 訂單流程全部完成！最終頁面網址: {page.url}")
                else:
                    log_debug("⚠️ [步驟 4] 找不到『下訂單』按鈕，請確認結帳頁面結構")
            except Exception as e:
                log_debug(f"⚠️ [步驟 4] 按下『下訂單』按鈕時發生錯誤: {e}")

            browser.close()
            log_debug("🏁 Playwright 瀏覽器已自動關閉，訂單流程結束。")
    except Exception as e:
        err_msg = f"Playwright Exception (DOM結構可能已變更或逾時): {e}"
        log_debug(f"{err_msg}\n{traceback.format_exc()}")
        # 發送告警通知至管理者日誌檔
        log_admin_alert(
            alert_type="DOM_STRUCTURE_CHANGE_WARNING",
            details=f"尚琳官網 Playwright 定位異常: {str(e)}。已自動引導員工直連官網手動填單。",
            worker_id=worker_id
        )

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

            # 填入日期
            target_date = order_date if order_date else datetime.now().strftime("%Y-%m-%d")
            page.evaluate(f"if(document.getElementById('start-date')) document.getElementById('start-date').value = '{target_date}'")
            log_debug(f"[取消流程] 已選擇日期: {target_date}")

            time.sleep(0.5)

            # 按下「查詢」按鈕
            search_btn = page.locator("#search-order-btn").first
            if search_btn.count() > 0:
                search_btn.click(force=True)
                log_debug("[取消流程] 已按下『查詢』按鈕，等待搜尋結果...")
                time.sleep(4)

            # 檢查是否有刪除按鈕 (.delete-item-btn 或 文字含有 刪 / 刪除)
            delete_btns = page.locator(".delete-item-btn, button:has-text('刪'), button:has-text('刪除')").all()
            if len(delete_btns) > 0:
                log_debug(f"[取消流程] 找到 {len(delete_btns)} 個刪除按鈕，準備依序自動刪除...")
                for btn in delete_btns:
                    try:
                        if btn.is_visible():
                            btn.click(force=True)
                            log_debug(f"✅ [取消流程] 已點擊尚琳官網『刪除』按鈕！")
                            time.sleep(2)
                    except Exception as btn_err:
                        log_debug(f"Click delete btn info: {btn_err}")
                log_debug(f"🎉 [取消流程] 已成功於尚琳官網『訂餐查詢』自動刪除工號 {worker_id} 的訂單！")
            else:
                log_debug(f"ℹ️ [取消流程] 未在尚琳官網搜尋結果中找到可刪除的按鈕（可能已過可取消時段或無訂單）")

            browser.close()
    except Exception as e:
        log_debug(f"run_playwright_delete_order error: {e}\n{traceback.format_exc()}")

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

    @app.get("/api/menu")
    async def get_menu():
        menu_file = DIRECTORY / "menu.json"
        if menu_file.exists():
            try:
                with open(menu_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                return {"error": str(e)}
        return []

    @app.get("/api/subsidy_status")
    async def get_subsidy_status(worker_id: str):
        return check_employee_subsidy_status(worker_id)

    @app.get("/api/admin/alerts")
    async def get_admin_alerts():
        alert_file = DIRECTORY / "admin_alerts.json"
        if alert_file.exists():
            try:
                with open(alert_file, "r", encoding="utf-8") as f:
                    return {"status": "success", "alerts": json.load(f)}
            except Exception as e:
                return {"status": "error", "message": str(e)}
        return {"status": "success", "alerts": []}

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
                raise HTTPException(status_code=404, detail="Order not found")
            # 軟刪除：更改狀態而非直接刪除
            order.status = "已取消 (CANCELED)"
            db.commit()

            # 觸發 Playwright 自動前往尚琳官網『訂餐查詢』頁面進行刪除
            log_debug(f"[取消流程] 已成功軟刪除 ID={order_code}，啟動 Playwright 前往尚琳『訂餐查詢』進行官網刪除: 工號={order.worker_id}")
            t = threading.Thread(target=run_playwright_delete_order, args=(order.worker_id, order.order_date), daemon=False)
            t.start()

            return {"status": "success", "message": "Order canceled and Playwright deletion triggered"}
        except Exception as e:
            log_debug(f"API Delete Order Exception: {e}")
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

            # 3. 觸發 Playwright 自動化流程（模式 B：將購物車餐點加入尚琳購物車 ➔ 自動按下「立即前往結帳」 ➔ 跳轉至結帳頁 ➔ 自動填入工號）
            log_debug(f"[模式 B] 訂單成功落盤 ID={new_order.id}，啟動 Playwright 自動化流程: {emp_id}")
            t = threading.Thread(target=run_playwright, args=(checkout_url, emp_id, items_dict), daemon=False)
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
        # 讀取地端 AI 伺服器設定與備援金鑰（由 .env 提供，不 Hardcode 預設值）
        ai_base_url = os.environ.get("AI_SERVER_BASE_URL", "").strip()
        ai_api_key = os.environ.get("AI_SERVER_API_KEY", "").strip()
        ai_model = os.environ.get("AI_SERVER_MODEL", "").strip()
        gemini_api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        mock_mode = os.environ.get("MOCK_MODE", "false").lower() == "true"
        
        log_debug(f"📸 開始分析照片... (Local Model: {ai_model}, Mock Mode: {mock_mode})")
        
        # 模擬食物分析結果數據庫（降級備援）
        mock_results = {
            "預設": {"name": "健康雞肉沙拉（模擬）", "protein": 28, "calories": 320, "reason": "根據照片中的雞肉和新鮮蔬菜判斷"},
            "雞胸肉": {"name": "烤雞胸肉套餐（模擬）", "protein": 35, "calories": 380, "reason": "高蛋白低脂肪的優質蛋白質來源"},
            "牛肉": {"name": "牛肉飯（模擬）", "protein": 30, "calories": 450, "reason": "含豐富鐵質和蛋白質"},
            "豬肉": {"name": "豬肉咖喱飯（模擬）", "protein": 25, "calories": 520, "reason": "含B群維生素和蛋白質"},
            "魚": {"name": "清蒸魚套餐（模擬）", "protein": 32, "calories": 280, "reason": "Omega-3脂肪酸豐富的健康選擇"},
        }

        try:
            if mock_mode:
                log_debug("🎭 使用模擬模式返回結果")
                return {"status": "success", "result": mock_results.get("預設"), "mode": "mock"}

            import requests
            import re
            
            # 優先嘗試地端 OpenAI 相容大模型
            if ai_base_url and ai_api_key:
                endpoint_url = f"{ai_base_url.rstrip('/')}/chat/completions"
                headers = {
                    "Authorization": f"Bearer {ai_api_key}",
                    "Content-Type": "application/json"
                }
                
                # 提示詞要求純 JSON 回應
                prompt_text = "這是一張食物照片。請仔細分析照片或餐點特徵，辨識食材，並以純粹的 JSON 物件格式回傳分析結果（不要包含任何 markdown 或說明文字），JSON 必須包含以下四個欄位：\"name\"(繁體中文餐點名稱), \"protein\"(整數，蛋白質克數), \"calories\"(整數，估計熱量大卡), \"reason\"(繁體中文，簡述判斷根據，約30字)。"
                
                payload = {
                    "model": ai_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt_text},
                                {"type": "image_url", "image_url": {"url": f"data:{req.mime_type or 'image/jpeg'};base64,{req.image_base64}"}}
                            ]
                        }
                    ],
                    "temperature": 0.2
                }
                
                log_debug(f"📡 向地端 AI 模型 ({ai_model}) 發送請求...")
                try:
                    res = requests.post(endpoint_url, json=payload, headers=headers, timeout=40)
                    if res.ok:
                        data = res.json()
                        result_text = data["choices"][0]["message"]["content"]
                        log_debug(f"✅ 地端 AI 響應成功")
                        json_match = re.search(r"\{[\s\S]*\}", result_text)
                        if json_match:
                            parsed = json.loads(json_match.group(0))
                            return {
                                "status": "success",
                                "result": {
                                    "name": parsed.get("name", "未知餐點"),
                                    "protein": int(parsed.get("protein", 0)),
                                    "calories": int(parsed.get("calories", 0)),
                                    "reason": parsed.get("reason", "地端 AI 分析完成")
                                },
                                "mode": "real"
                            }
                except Exception as local_ai_err:
                    log_debug(f"⚠️ 地端 AI 視覺調用提示/未支援圖片: {local_ai_err}")

            # 備援：若有配置 Gemini API
            if gemini_api_key:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={gemini_api_key}"
                payload = {
                    "contents": [{
                        "parts": [
                            { "text": "這是一張食物照片。請仔細分析照片中的餐點，辨識所有食材，並以純粹的 JSON 物件格式回傳分析結果（不需要任何 markdown 或說明文字），JSON 必須包含以下四個欄位：\"name\"(繁體中文餐點名稱), \"protein\"(整數，蛋白質克數), \"calories\"(整數，估計熱量大卡), \"reason\"(繁體中文，簡述判斷根據，約30字)。" },
                            { "inline_data": { "mime_type": req.mime_type or "image/jpeg", "data": req.image_base64 } }
                        ]
                    }]
                }
                log_debug(f"📡 嘗試備援 Gemini API 發送請求...")
                res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=20)
                if res.ok:
                    data = res.json()
                    result_text = data["candidates"][0]["content"]["parts"][0]["text"]
                    json_match = re.search(r"\{[\s\S]*\}", result_text)
                    if json_match:
                        parsed = json.loads(json_match.group(0))
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

            # 降級至模擬數據
            log_debug("⚠️ AI 呼叫未成功，優雅降級使用模擬數據")
            mock_result = mock_results.get("預設")
            return {
                "status": "success",
                "result": mock_result,
                "mode": "mock",
                "note": "AI 服務暫時無法解析，已啟動備援模式"
            }
        except Exception as e:
            error_detail = f"{str(e)}\n{traceback.format_exc()}"
            log_debug(f"❌ 分析照片發生未預期錯誤: {error_detail}，降級使用模擬數據")
            return {
                "status": "success",
                "result": mock_results.get("預設"),
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

def run_daily_crawler():
    while True:
        try:
            log_debug("--- Running Daily Scheduled Crawler ---")
            crawler.crawl_slk9898()
        except Exception as e:
            log_debug(f"Crawler Scheduler Exception: {e}")
        # Sleep for 24 hours (86400 seconds)
        time.sleep(86400)

# 啟動每日爬蟲背景執行緒
crawler_thread = threading.Thread(target=run_daily_crawler, daemon=True)
crawler_thread.start()
