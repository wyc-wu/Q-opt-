# 尚琳廚苑 ✦ 智慧午餐系統 v3.0 - Linux 主機 Docker Compose 部署手冊

本手冊說明如何將已打包的 Docker 映像檔 (`q-opt_v3.0_image.tar`) 部署至遠端 Linux 主機（如 Ubuntu, Debian, CentOS 等）。

---

## 📁 部署所需檔案

請將本機 `D:\Aitigravity\q-opt_v3.0\` 中的以下檔案上傳至 Linux 主機的同一個目錄下（例如 `~/q-opt/`）：

1. `q-opt_v3.0_image.tar`（Docker 映像檔封存檔）
2. `docker-compose.yml`（Docker Compose 設定檔）
3. `.env`（環境變數檔，內含地端 AI 伺服器與模型連線設定）
4. `orders.db`（可選，若需保留目前的歷史訂單與員工資料）

---

## 🚀 部署與啟動步驟（在 Linux 主機上執行）

### 步驟 1：進入部署目錄
```bash
cd ~/q-opt
```

### 步驟 2：匯入 Docker 映像檔
```bash
docker load -i q-opt_v3.0_image.tar
```

### 步驟 3：預先建立掛載檔案（重要！避免 Docker 誤建為資料夾）
若主機目錄下尚無以下檔案，請先執行 `touch` 建立空檔案，避免 Docker 將單一檔案誤判並自動建立為資料夾：
```bash
touch playwright_debug.log .env
```
*(若 `orders.db` 尚未上傳，亦可執行 `touch orders.db`)*

### 步驟 4：透過 Docker Compose 一鍵啟動服務
```bash
docker compose up -d --remove-orphans
```

---

## ⚠️ 常見問題排除 (Troubleshooting)

### 錯誤：`not a directory: Are you trying to mount a directory onto a file?`
- **原因**：若在執行 `docker compose up` 前宿主機上不存在掛載的檔案，Docker 會自動在 Linux 上建立同名的「資料夾」，導致與容器內的檔案衝突。
- **解法**：
  ```bash
  # 1. 停止容器
  docker compose down --remove-orphans

  # 2. 刪除誤建的資料夾
  rm -rf playwright_debug.log

  # 3. 重新建立為實體檔案
  touch playwright_debug.log .env

  # 4. 重新啟動
  docker compose up -d
  ```

---

## 🔍 常用運維與監控指令

### 1. 查看容器運行狀態
```bash
docker compose ps
```

### 2. 查看即時 Playwright 執行日誌
```bash
docker compose logs -f --tail 50 q-opt
# 或直接監控本機掛載的日誌檔案
tail -f playwright_debug.log
```

### 3. 重啟服務
```bash
docker compose restart
```

### 4. 停止服務
```bash
docker compose down
```

---

## 🌐 存取應用程式
啟動後，開啟瀏覽器存取 Linux 主機的 IP 與 Port：
👉 **`http://<您的-Linux-IP>:8010`**
