import os
import csv
from io import StringIO
from typing import Dict, List, Optional
from PIL import Image, ImageDraw, ImageFont
from fastapi import FastAPI, Query, Response, HTTPException
import uvicorn

# ==================== 參數設定 ====================
# 請確保字型檔案在同目錄下，或是修改這裡的路徑
FONT_PATH = 'NotoSansTC-Regular.ttf' 
DEFAULT_SIZE = 16
ALLOWED_SIZES = [16, 24, 32]

# ==================== 初始化 FastAPI ====================
app = FastAPI(
    title="ESP32 跑馬燈字型 API",
    description="動態生成中英文字元點陣圖 (CSV格式)",
    version="2.0"
)

# 全域快取：避免重複運算
# 格式: { "16": { "你": [bytes...], "好": [bytes...] } }
FONT_CACHE: Dict[str, Dict] = {}

# ==================== 核心功能：文字轉點陣 ====================
def text_to_dot_matrix(text: str, font_path: str, font_size: int) -> List[int]:
    """
    將單一字元轉為 1-bit 點陣數據列表
    """
    img_size = font_size

    # 1. 載入字型
    try:
        font = ImageFont.truetype(font_path, font_size)
    except Exception:
        # 如果載入失敗，使用預設字型（通常很醜，且不支援中文，但在伺服器端除錯有用）
        font = ImageFont.load_default()
    
    # 2. 建立畫布 (1-bit mode)
    img = Image.new('1', (img_size, img_size), 0)
    draw = ImageDraw.Draw(img)

    # 3. 計算文字大小與置中
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    # 自動縮放邏輯：如果字太大超出格子，就縮小字型
    if text_width > img_size or text_height > img_size:
        scale = min(img_size / max(text_width, 1), img_size / max(text_height, 1)) * 0.9
        new_size = max(8, int(font_size * scale))
        try:
            font = ImageFont.truetype(font_path, new_size)
        except:
            pass
        # 重新計算置中
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]

    x = (img_size - text_width) // 2 - bbox[0]
    y = (img_size - text_height) // 2 - bbox[1]

    # 4. 繪製文字 (fill=1 代表白色/亮點)
    draw.text((x, y), text, font=font, fill=1)

    # 5. 轉換為 Byte Array (Row-major)
    # 這是 ESP32 最容易處理的格式
    bytes_list = []
    for py in range(img_size):
        for px_start in range(0, img_size, 8):
            byte = 0
            for bit in range(8):
                px = px_start + bit
                if px < img_size:
                    # 取得像素值 (0 或 1)
                    pixel = img.getpixel((px, py))
                    if pixel:
                        byte |= (1 << (7 - bit))
            bytes_list.append(byte)
    
    return bytes_list

def get_cached_fonts(text: str, size: int) -> Dict:
    """從快取取得字型，若無則生成"""
    size_key = str(size)
    
    if size_key not in FONT_CACHE:
        FONT_CACHE[size_key] = {}
    
    cache = FONT_CACHE[size_key]
    result = {}
    
    # 找出不在快取中的字
    chars_to_gen = set(text) - set(cache.keys())
    
    # 批量生成
    if chars_to_gen:
        # 檢查字型檔是否存在，只檢查一次
        if not os.path.exists(FONT_PATH):
            print(f"⚠️ 警告：找不到字型檔 {FONT_PATH}，將使用系統預設字型")

        for char in chars_to_gen:
            try:
                dots = text_to_dot_matrix(char, FONT_PATH, size)
                cache[char] = dots
            except Exception as e:
                print(f"❌ 生成字元 '{char}' 失敗: {e}")
                # 失敗時給全黑
                bytes_per_char = (size * size) // 8
                cache[char] = [0] * bytes_per_char
    
    return cache

# ==================== API 路由 ====================

@app.get("/")
def index():
    return {
        "status": "online",
        "usage": "/font.csv?text=你好&size=16",
        "supported_sizes": ALLOWED_SIZES
    }

@app.get("/font.csv")
def download_font_csv(
    text: str = Query(..., description="要轉換的文字"),
    size: int = Query(DEFAULT_SIZE, description="字體大小 (16, 24, 32)")
):
    """
    ESP32 專用端點：回傳 CSV 格式的點陣資料
    """
    if not text:
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    
    if size not in ALLOWED_SIZES:
        # 如果請求了不支援的大小，強制轉回 16，避免報錯
        size = 16

    # 1. 確保字體已在快取中
    get_cached_fonts(text, size)
    cache = FONT_CACHE[str(size)]

    # 2. 建立 CSV 內容
    output = StringIO()
    writer = csv.writer(output)
    
    # Header: char, byte0, byte1, ...
    bytes_per_char = (size * size) // 8
    header = ['char'] + [f'byte{i}' for i in range(bytes_per_char)]
    writer.writerow(header)

    # Content
    unique_chars = sorted(list(set(text)), key=text.index) # 保持順序且去重
    for char in unique_chars:
        if char in cache:
            row = [char] + cache[char]
            writer.writerow(row)

    csv_data = output.getvalue()

    # 3. 回傳檔案
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=font_{size}.csv",
            "Cache-Control": "no-cache" # 禁止瀏覽器快取，確保開發時拿到最新的
        }
    )

@app.get("/clear")
def clear_cache():
    FONT_CACHE.clear()
    return {"message": "Cache cleared"}

# ==================== 啟動入口 ====================
if __name__ == "__main__":
    # 檢查環境
    if not os.path.exists(FONT_PATH):
        print("\n" + "="*50)
        print(f"❌ 嚴重錯誤：找不到 {FONT_PATH}")
        print("請下載 NotoSansTC-Regular.ttf 並放在此目錄下！")
        print("="*50 + "\n")
    else:
        print(f"✅ 字型檔檢查 OK: {FONT_PATH}")

    # 啟動伺服器
    print("🚀 API 伺服器啟動中...")
    print("   本地測試: http://localhost:5000/font.csv?text=測試&size=16")
    print("   Swagger文件: http://localhost:5000/docs")
    
    uvicorn.run(app, host="0.0.0.0", port=5000)