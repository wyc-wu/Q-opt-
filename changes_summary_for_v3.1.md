# 尚琳智慧午餐系統 v3.0 部署與功能變動彙總報告 (供下版打包參考)

本報告彙整了在將 **v3.0 版本** 成功容器化並部署至 Linux 主機、以及切換至地端 GPUStack 大語言模型過程中，所做的所有程式碼修改與新增之容器編排設定檔。請宜瑾在打包下一版本時將以下變更整併納入。

---

## 📂 變動檔案清單一覽

| 檔案路徑 | 類型 | 變動核心摘要 |
| :--- | :---: | :--- |
| **`index.html`** | 🟡 修改 | 1. API 網址全面動態化 (`window.location.origin`)<br>2. 拍照分析標籤由 `(Gemini Vision)` 改為 `(AI)` |
| **`.env`** | 🟡 修改 | 增加地端大模型 (GPUStack / `qwen3.6-27b-awq`) 連線設定 |
| **`analyze_menu_protein.py`** | 🟡 修改 | 由 Gemini API 改為標準 OpenAI 相容 API 協定呼叫地端模型 |
| **`server.py`** | 🟡 修改 | 拍照分析端點改接地端模型，並將金鑰/IP 改由 `.env` 完全接管 |
| **`docker-compose.yml`** | 🟢 新增 | Docker Compose 服務編排設定（Port 8010、時區、Volume 掛載） |
| **`Dockerfile`** | 🟢 新增 | Linux 容器映像檔建置定義（含 Playwright Chromium 依賴庫） |
| **`requirements.txt`** | 🟢 新增 | Python 相依套件清單 |
| **`.dockerignore`** | 🟢 新增 | 容器打包忽略清單（排除 `.git`、`.tar`、`.venv` 等） |
| **`DEPLOY_LINUX.md`** | 🟢 新增 | Linux 主機部署步驟與 Crontab 自動排程維運手冊 |

---

## 📝 具體修改內容與程式碼說明

### 1. `index.html`（前端跨環境動態適應）
* **問題**：原程式碼中多處寫死 `http://localhost:8000` 或 `http://127.0.0.1:8000`，部署至 Linux 主機並使用其他 Port（如 8010）時，使用者瀏覽器會發送請求至本機而導致「無法連線伺服器」。
* **修改內容**：
  1. **動態基底網址**：
     ```javascript
     // 自動取得當前連線之主機 IP/Domain 與 Port
     const API_BASE = window.location.origin.startsWith('http') ? window.location.origin : 'http://127.0.0.1:8000';
     const API = {
       menu: `${API_BASE}/api/menu`,
       checkout: `${API_BASE}/api/checkout`,
       analyzePhoto: `${API_BASE}/api/analyze-photo`
     };
     ```
  2. **將所有 API 呼叫改為 `${API_BASE}/api/...`**：
     * `fetchUserData` (`/api/user/me`)
     * `login` (`/api/login`)
     * `register` (`/api/register`)
     * `fetchAdminOrders` / `syncBackendDb` / `checkout` (`/api/orders`)
     * `deleteOrder` / `editOrder` (`/api/orders/${id}`)
  3. **UI 標籤文字微調**：
     將拍照辨識結果標籤由 `(Gemini Vision)` 改為通用且簡潔的 `(AI)`。

---

### 2. `.env`（環境變數設定）
* **修改內容**：加入地端 GPUStack 大模型連線參數（敏感 IP 與 Token 統一由 `.env` 管理）：
  ```env
  # 地端大模型伺服器設定 (GPUStack / OpenAI-compatible API)
  AI_SERVER_BASE_URL=http://10.0.1.77:9090/v1
  AI_SERVER_API_KEY=gpustack_9f090a5a919442ca_3018e91b52011e48ffccb8aec3637451
  AI_SERVER_MODEL=qwen3.6-27b-awq

  # 舊版 Google Gemini API 金鑰 (備援用)
  GEMINI_API_KEY=AQ.Ab8RN6JOXuRnSKKqAqzwsieORQ6dNLcwO_wGz3qq15NdcQBqsA
  ```

---

### 3. `analyze_menu_protein.py`（菜單營養精算切換為地端模型）
* **修改內容**：
  * 改採標準 OpenAI-compatible Chat Completions 協定 (`POST /v1/chat/completions`)。
  * 移除程式碼中 Hardcode 的預設 IP 與 Token，完全由 `.env` 驅動。
  * 強化正則表達式 JSON 陣列提取（`re.search(r"\[[\s\S]*\]", text)`），避免模型回傳 ````json` 標記時解析報錯。

---

### 4. `server.py`（後端 AI 端點安全強化）
* **修改內容**：
  * `/api/analyze-photo` 端點優先調用地端大模型，並保留 Gemini API 與模擬數據雙層 Fallback 機制。
  * 移除 `os.environ.get(...)` 中的 Hardcoded IP 與 Key 預設值，落實 12-Factor 資安規範。

---

### 5. 新增 Docker 容器化與 Compose 設定檔
* **`Dockerfile`**：
  * 基底映像檔：`python:3.11-slim`
  * 設定時區為 `Asia/Taipei` 與 UTF-8 編碼環境。
  * 安裝 Python 相依套件與 Playwright 底層 Chromium 依賴：`playwright install --with-deps chromium`。
* **`docker-compose.yml`**：
  * 映射外部連接埠 **`8010:8000`**（避免與伺服器既有 8000 Port 衝突）。
  * 配置持久化掛載（Volumes）：
    * `./orders.db:/app/orders.db`
    * `./menu.json:/app/menu.json`
    * `./.env:/app/.env`
    * `./admin_alerts.json:/app/admin_alerts.json`
    * `./playwright_debug.log:/app/playwright_debug.log`
* **`requirements.txt`**：
  ```text
  fastapi>=0.100.0
  uvicorn[standard]>=0.22.0
  sqlalchemy>=2.0.0
  pymysql>=1.0.0
  pydantic>=2.0.0
  playwright>=1.40.0
  requests>=2.31.0
  ```

---

## 💡 給宜瑾後續架構優化的 2 點技術建議

1. **背景定時爬蟲啟動順序**：
   * 在 `server.py` 中，`crawler_thread.start()` 原本放在 `if __name__ == "__main__": uvicorn.run(...)` 之後。因為 `uvicorn.run()` 會阻塞主行程，建議在下一版將執行緒啟動移至 `uvicorn.run()` 之前，或使用 FastAPI 官方的 `lifespan` 事件處理。
2. **揚棄品名寫死字典 (`NAME_OVERRIDES`)**：
   * 目前 `crawler.py` 透過 `product_id` 寫死菜名（如「超夯酥炸大雞腿飯」），近期店家改版為「無骨/去骨大雞腿」時字典無法自動感知。建議直接結合地端 AI 模型，在精算蛋白質時順帶完成「去除行銷廣告詞、保留無骨/去骨核心料理特徵」的自動清洗，達到 100% 零維護。
