# 尚琳廚苑 ✦ 智慧午餐系統 (Q-OPT v3.0) 架構審查與優化建議回饋

---

## 🌟 一、版本成果與肯定 (Key Achievements)

1. **Playwright 自動化無頭模式整合成功**：
   * v3.0 成功將 Playwright 瀏覽器改為無頭模式 (`headless=True`)，於背景順暢完成自動加車、結帳與取消訂單，大幅提升使用者體驗。
2. **專案標準化與輕量化**：
   * 移除本機相依的 `.venv` 目錄，使專案檔案結構更輕巧乾淨，符合正規軟體工程標準。
3. **介面現代化與功能完整**：
   * 蛋白質每週目標追蹤、AI 推薦、拍照影像辨識等企業級功能完整，UI 視覺質感高。

---

## 💡 二、遠端部署與跨環境相容性建議 (Deployment & Portability)

### 1. 前端 API 網址應改為「動態路徑」或「相對路徑」
* **現況問題**：
  `index.html` 中將 API 請求網址寫死為 `http://localhost:8000`（如 `/api/login`、`/api/orders` 等）。
* **影響層面**：
  當系統部署至遠端 Linux 伺服器或改用其他 Port（如 8010）時，使用者從外部電腦開啟網頁登入，瀏覽器會嘗試連線使用者自己的個人電腦 8000 Port，導致「無法連線伺服器」。
* **優化建議**：
  將 API 基礎網址改為 `window.location.origin`（或純相對路徑 `/api/...`）：
  ```javascript
  // ✅ 自動動態取得當前主機與 Port，通吃本機與遠端 Linux
  const API_BASE = window.location.origin;
  const res = await fetch(`${API_BASE}/api/login`, ...);
  ```

---

### 2. Windows 腳本編碼相容性
* **現況問題**：
  Windows `.bat` 批次檔若含有中文或 UTF-8 特殊字元，在繁體中文 Windows 預設 Big5 (CP950) 環境下執行會解析失敗或產生亂碼。
* **優化建議**：
  啟動與日誌監控腳本建議使用純 ASCII 指令引導，或透過 PowerShell 指定 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` 執行。

---

## ⚙️ 三、後端架構與背景排程優化 (Backend & Concurrency)

### 1. 修正每日定時爬蟲執行緒的啟動順序
* **現況問題**：
  `server.py` 中 `crawler_thread.start()` 放置在 `if __name__ == "__main__": uvicorn.run(...)` 之後。
* **影響層面**：
  `uvicorn.run()` 屬於同步阻塞式執行（Blocking Call），後方的執行緒啟動程式碼永遠不會被執行到，導致系統無法自動執行每日菜單定時更新。
* **優化建議**：
  將執行緒啟動移至 `uvicorn.run()` 前，或使用 FastAPI 官方推薦的生命週期管理器（Lifespan Handler）：
  ```python
  # ✅ 於 Web 伺服器啟動前先啟動背景排程執行緒
  crawler_thread = threading.Thread(target=run_daily_crawler, daemon=True)
  crawler_thread.start()

  if __name__ == "__main__":
      uvicorn.run(app, host="0.0.0.0", port=PORT)
  ```

---

### 2. 菜單資料空檔自我防禦（Auto-healing）
* **優化建議**：
  在 `GET /api/menu` 或伺服器啟動時，若偵測到 `menu.json` 為空檔或不存在，增加自動在背景補爬一次的防禦邏輯，避免因檔案損毀導致前端畫面空白。

---

## 🧠 四、爬蟲與資料清洗演算法建議 (Data Processing & AI)

### 1. 建議揚棄 `product_id` 硬編碼轉置，改用「Gemini AI / 地端 AI 語意清洗」
* **現況問題**：
  目前 `crawler.py` 採用寫死字典 `NAME_OVERRIDES = {69395: "泰式檸檬雞腿排", ...}` 強制轉置品名。
* **盲點與限制**：
  * **無法感知店家改版**：近期尚琳將大雞腿升級為「無骨/去骨大雞腿」，寫死的字典會強制將其變回舊名稱，失去改版的重要資訊。
  * **無法全自動化**：只要官網推出新餐點或更換 ID，就必須人工修改程式碼維護字典。
* **優化建議（雙贏做法）**：
  爬蟲抓完資料後原本就會調用 AI 精算蛋白質，建議**順帶讓 AI 在同一批 API 請求中完成品名清洗**：
  * Prompt 要求：*「去除廣告促銷廢話（如：肉多多、重磅回歸、可微波等），精準保留料理核心特徵（如：無骨、去骨、炭烤、泰式等），輸出 8~14 字標準品名」*。
  * **效益**：0 人工維護成本、自動適應菜色改版、不增加額外 API 調用次數。

---

## 📋 總結建議清單

| 項目 | 優先級 | 優化效益 |
| :--- | :---: | :--- |
| **前端 API 網址動態化** | 🔴 高 | 徹底解決遠端 Linux / Docker 部署後的連線問題 |
| **背景定時爬蟲執行序修復** | 🔴 高 | 確保每日自動從尚琳官網抓回最新菜單與價格 |
| **AI 自動語意清洗品名** | 🟡 中 | 解決品項改版（如無骨雞腿）無法自動反映之盲點，達到 100% 零人工維護 |
| **增加首次登入密碼引導** | 🟢 低 | 優化員工首次使用系統時的操作體驗 |
