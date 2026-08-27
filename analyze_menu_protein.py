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

# 1. 讀取 .env 中的金鑰
env_file = Path(__file__).parent / ".env"
api_key = ""
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("GEMINI_API_KEY="):
                api_key = line.strip().split("GEMINI_API_KEY=", 1)[1].strip().strip('"').strip("'")
                break

if not api_key:
    print("❌ 找不到 GEMINI_API_KEY")
    exit(1)

# 2. 讀取現有菜單
menu_file = Path(__file__).parent / "menu.json"
with open(menu_file, "r", encoding="utf-8") as f:
    menu = json.load(f)

# 只針對有有效標價(price > 0)的正式便當品項進行營養精算
valid_items = [item for item in menu if item.get("price", 0) > 0]
dish_names = [item["name"] for item in valid_items]

print(f"🍱 準備請 Gemini AI 分析 {len(dish_names)} 道菜色的精確蛋白質：")
for d in dish_names:
    print(f" - {d}")

prompt = f"""你是一位國家級專業營養師與食品科學專家。以下是台灣便當店（尚琳廚苑）的菜單品項列表：
{json.dumps(dish_names, ensure_ascii=False, indent=2)}

請依據台灣常見一般外帶便當的標準規格（包含主菜肉品份量重約120~180g、白飯一碗約160g含6g蛋白質、以及3~4樣日常配菜約提供4~8g蛋白質），以極致精確的營養學標準，計算每一道便當的「總蛋白質含量（公克 g）」與「估計總熱量（大卡 kcal）」。

【重要格式規則】：
1. 一般便當：固定格式為「主菜簡稱約Xg + 白飯6g + 配菜約Yg」（例如："主菜雞腿排約28g + 白飯6g + 配菜約6g"），切勿在配菜後方加上括號說明。
2. 蔬食/素食便當（偏向菜飯便當無固定特定單一肉類主菜）：固定格式為「白飯6g + 多樣蔬食配菜約12g」（總蛋白質約18g，熱量約520 kcal），不需特別提及蒸蛋或特定豆製品。

請嚴格只回傳純 JSON 陣列，不要包含任何 markdown 代碼塊標記 (```json) 或說明文字，格式如下：
[
  {{
    "name": "餐點名稱",
    "protein": 34,
    "calories": 750,
    "breakdown": "主菜雞肉約22g + 白飯6g + 配菜約6g"
  }}
]
"""

url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={api_key}"
payload = {
    "contents": [{"parts": [{"text": prompt}]}]
}

print("\n🤖 正在連線 Google Gemini 進行深度營養學分析...")
res = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=30)
if not res.ok:
    print("❌ API 呼叫失敗:", res.status_code, res.text)
    exit(1)

data = res.json()
text = data["candidates"][0]["content"]["parts"][0]["text"]
json_match = re.search(r"\[[\s\S]*\]", text)
if not json_match:
    print("❌ 無法解析 JSON:", text)
    exit(1)

ai_analysis = json.loads(json_match.group(0))
ai_dict = {item["name"]: item for item in ai_analysis}

print("\n✅ Gemini 營養分析成果：")
print("=" * 60)
for item in ai_analysis:
    print(f"🍲 {item['name']:<18} | 蛋白質: {item['protein']:>2}g | 熱量: {item['calories']:>4} kcal | {item.get('breakdown', '')}")

# 3. 更新 menu.json
updated_count = 0
for item in menu:
    if item["name"] in ai_dict:
        analyzed = ai_dict[item["name"]]
        item["protein"] = analyzed["protein"]
        item["calories"] = analyzed["calories"]
        item["protein_breakdown"] = analyzed.get("breakdown", "")
        updated_count += 1

with open(menu_file, "w", encoding="utf-8") as f:
    json.dump(menu, f, ensure_ascii=False, indent=2)

print("=" * 60)
print(f"🎉 成功將 {updated_count} 筆真實 AI 精算蛋白質更新至 menu.json！")
