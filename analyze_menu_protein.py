import os
import sys
import json
import requests
import re
from pathlib import Path

# Enforce UTF-8 output
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

# 1. 讀取 .env 中的地端 AI 伺服器設定
env_file = Path(__file__).parent / ".env"
ai_base_url = ""
ai_api_key = ""
ai_model = ""

if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if line_str.startswith("AI_SERVER_BASE_URL="):
                ai_base_url = line_str.split("AI_SERVER_BASE_URL=", 1)[1].strip().strip('"').strip("'")
            elif line_str.startswith("AI_SERVER_API_KEY="):
                ai_api_key = line_str.split("AI_SERVER_API_KEY=", 1)[1].strip().strip('"').strip("'")
            elif line_str.startswith("AI_SERVER_MODEL="):
                ai_model = line_str.split("AI_SERVER_MODEL=", 1)[1].strip().strip('"').strip("'")

if not ai_base_url or not ai_api_key:
    print("❌ 找不到 AI_SERVER_BASE_URL 或 AI_SERVER_API_KEY，請確認 .env 檔案中已設定！")
    exit(1)

# 2. 讀取現有菜單
menu_file = Path(__file__).parent / "menu.json"
if not menu_file.exists():
    print(f"❌ 找不到菜單檔案: {menu_file}")
    exit(1)

with open(menu_file, "r", encoding="utf-8") as f:
    try:
        menu = json.load(f)
    except Exception as e:
        print(f"❌ 讀取菜單失敗: {e}")
        exit(1)

# 只針對可供購買或有品名的正式便當品項進行營養精算
valid_items = [item for item in menu if item.get("is_available") or item.get("price", 0) > 0 or "飯" in item.get("name", "") or "便當" in item.get("name", "") or "排" in item.get("name", "") or "腿" in item.get("name", "")]
dish_names = [item["name"] for item in valid_items]

if not dish_names:
    print("ℹ️ 菜單中無有效餐點需要分析。")
    exit(0)

print(f"🍱 準備請地端 AI 模型 ({ai_model}) 分析 {len(dish_names)} 道菜色的精確蛋白質：")
for d in dish_names:
    print(f" - {d}")

prompt = f"""你是一位國家級專業營養師與食品科學專家。以下是台灣便當店（尚琳廚苑）的菜單品項列表：
{json.dumps(dish_names, ensure_ascii=False, indent=2)}

請依據台灣常見一般外帶便當的標準規格（包含主菜肉品份量重約120~180g、白飯一碗約160g含6g蛋白質、以及3~4樣日常配菜約提供4~8g蛋白質），以極致精確的營養學標準，計算每一道便當的「總蛋白質含量（公克 g）」與「估計總熱量（大卡 kcal）」。

【重要格式規則】：
1. 一般便當：固定格式為「主菜簡稱約Xg + 白飯6g + 配菜約Yg」（例如："主菜雞腿排約28g + 白飯6g + 配菜約6g"），切勿在配菜後方加上括號說明。
2. 蔬食/素食便當（偏向菜飯便當無固定特定單一肉類主菜）：固定格式為「白飯6g + 多樣蔬食配菜約12g」（總蛋白質約18g，熱量約520 kcal），不需特別提及蒸蛋或特定豆製品。

請嚴格只回傳純 JSON 陣列，不要包含任何 markdown 代碼塊標記 (```json) 或額外前言說明文字，格式如下：
[
  {{
    "name": "餐點名稱",
    "protein": 34,
    "calories": 750,
    "breakdown": "主菜雞肉約22g + 白飯6g + 配菜約6g"
  }}
]
"""

endpoint_url = f"{ai_base_url.rstrip('/')}/chat/completions"
headers = {
    "Authorization": f"Bearer {ai_api_key}",
    "Content-Type": "application/json"
}
payload = {
    "model": ai_model,
    "messages": [
        {
            "role": "system",
            "content": "你是一位國家級專業營養師與食品科學專家。請嚴格以 JSON 陣列格式回傳分析結果，不要輸出任何額外引言或文字。"
        },
        {
            "role": "user",
            "content": prompt
        }
    ],
    "temperature": 0.2
}

print(f"\n🤖 正在連線地端 AI 伺服器 ({endpoint_url}) 進行深度營養學分析...")
try:
    res = requests.post(endpoint_url, json=payload, headers=headers, timeout=60)
    if not res.ok:
        print(f"❌ API 呼叫失敗 (HTTP {res.status_code}): {res.text}")
        exit(1)

    data = res.json()
    text = data["choices"][0]["message"]["content"]
    
    # 提取 JSON 陣列
    json_match = re.search(r"\[[\s\S]*\]", text)
    if not json_match:
        print("❌ 無法從模型回應中解析 JSON 陣列:", text)
        exit(1)

    ai_analysis = json.loads(json_match.group(0))
    ai_dict = {item["name"]: item for item in ai_analysis if "name" in item}

    print("\n✅ 地端 AI 營養分析成果：")
    print("=" * 60)
    for item in ai_analysis:
        print(f"🍲 {item.get('name', ''):<18} | 蛋白質: {item.get('protein', 0):>2}g | 熱量: {item.get('calories', 0):>4} kcal | {item.get('breakdown', '')}")

    # 3. 更新 menu.json
    updated_count = 0
    for item in menu:
        if item["name"] in ai_dict:
            analyzed = ai_dict[item["name"]]
            item["protein"] = int(analyzed.get("protein", item.get("protein", 25)))
            item["calories"] = int(analyzed.get("calories", item.get("calories", 700)))
            item["protein_breakdown"] = analyzed.get("breakdown", "")
            updated_count += 1

    with open(menu_file, "w", encoding="utf-8") as f:
        json.dump(menu, f, ensure_ascii=False, indent=2)

    print("=" * 60)
    print(f"🎉 成功將 {updated_count} 筆地端 AI 精算蛋白質更新至 menu.json！")

except requests.exceptions.RequestException as req_err:
    print(f"❌ 地端 AI 伺服器連線異常: {req_err}")
    exit(1)
except Exception as e:
    print(f"❌ 處理過程發生錯誤: {e}")
    exit(1)
