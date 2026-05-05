"""
TDX 路網資料抓取 Script

從 TDX Section API 抓取台北市的路段資料（含起終點座標、路段長度），
並從 SectionShape API 補充幾何線資訊，輸出為 JSON 快照供 multiagent-service seed 使用。

Usage:
    python import_tdx_road_network.py
    （自動讀取專案根目錄 .env 的 TDX-CLIENT-ID / TDX-CLIENT-SECRET）
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# 自動載入 .env（專案根目錄）
_env_path = Path(__file__).resolve().parents[1] / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

TDX_AUTH_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
TDX_SECTION_URL = "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Section/City/Taipei"
TDX_SECTION_SHAPE_URL = "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/SectionShape/City/Taipei"

# 台北車站 (25.0478, 121.5170) 為中心，半徑約 2.2km
BBOX_SW = (25.0278, 121.4970)  # 西南角 (lat, lng)
BBOX_NE = (25.0678, 121.5370)  # 東北角 (lat, lng)

OUTPUT_PATH = Path(__file__).resolve().parents[1] / "data" / "taipei_road_sections.json"

# TDX 免費會員 rate limit 較低，每次請求間隔
REQUEST_DELAY_SEC = 2


def get_access_token(client_id: str, client_secret: str) -> str:
    """透過 OAuth2 Client Credentials 取得 TDX access token。"""
    response = httpx.post(
        TDX_AUTH_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    if response.status_code != 200:
        print(f"[ERROR] TDX 認證失敗: HTTP {response.status_code}", file=sys.stderr)
        print(f"  Response: {response.text}", file=sys.stderr)
        sys.exit(1)
    return response.json()["access_token"]


def fetch_sections(access_token: str) -> list[dict]:
    """從 TDX Section API 抓取所有高雄路段，處理分頁。"""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    all_sections = []
    skip = 0
    page_size = 100

    while True:
        params = {
            "$top": str(page_size),
            "$skip": str(skip),
            "$format": "JSON",
        }
        time.sleep(REQUEST_DELAY_SEC)
        response = httpx.get(TDX_SECTION_URL, headers=headers, params=params, timeout=60)

        if response.status_code == 429:
            print(f"[WARN] Rate limited, waiting 10s...", file=sys.stderr)
            time.sleep(10)
            continue

        if response.status_code != 200:
            print(f"[ERROR] TDX API 錯誤: HTTP {response.status_code}", file=sys.stderr)
            print(f"  Response: {response.text}", file=sys.stderr)
            sys.exit(1)

        data = response.json()
        sections = data.get("Sections", [])
        if not sections:
            break

        all_sections.extend(sections)
        print(f"  已取得 {len(all_sections)} 筆...", flush=True)

        if len(sections) < page_size:
            break
        skip += page_size

    return all_sections


def fetch_section_shapes(access_token: str) -> dict[str, str]:
    """從 TDX SectionShape API 抓取路段幾何線，回傳 {SectionID: WKT} mapping。"""
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}

    shapes: dict[str, str] = {}
    skip = 0
    page_size = 100

    while True:
        params = {
            "$top": str(page_size),
            "$skip": str(skip),
            "$format": "JSON",
        }
        time.sleep(REQUEST_DELAY_SEC)
        response = httpx.get(TDX_SECTION_SHAPE_URL, headers=headers, params=params, timeout=60)

        if response.status_code == 429:
            print("[WARN] Rate limited (SectionShape), waiting 10s...", file=sys.stderr)
            time.sleep(10)
            continue

        if response.status_code != 200:
            print(f"[WARN] SectionShape API 錯誤: HTTP {response.status_code}，跳過幾何資料", file=sys.stderr)
            break

        data = response.json()
        items = data.get("SectionShapes", [])
        if not items:
            break

        for item in items:
            sid = item.get("SectionID", "")
            geom = item.get("Geometry", "")
            if sid and geom:
                shapes[sid] = geom

        print(f"  SectionShape: 已取得 {len(shapes)} 筆...", flush=True)

        if len(items) < page_size:
            break
        skip += page_size

    return shapes


def in_bbox(lat: float, lon: float) -> bool:
    """檢查座標是否在 bounding box 內。"""
    return BBOX_SW[0] <= lat <= BBOX_NE[0] and BBOX_SW[1] <= lon <= BBOX_NE[1]


def extract_section(raw: dict) -> dict | None:
    """從 TDX Section 資料擷取需要的欄位，過濾 bounding box。"""
    start = raw.get("SectionStart", {})
    end = raw.get("SectionEnd", {})

    start_lat = start.get("PositionLat", 0)
    start_lon = start.get("PositionLon", 0)
    end_lat = end.get("PositionLat", 0)
    end_lon = end.get("PositionLon", 0)

    # 至少一端在 bounding box 內
    if not (in_bbox(start_lat, start_lon) or in_bbox(end_lat, end_lon)):
        return None

    # 用起終點座標組成 geometry（兩點線段）
    geometry = [[start_lon, start_lat], [end_lon, end_lat]]

    # SectionLength 單位是公里
    length_m = raw.get("SectionLength", 0) * 1000

    return {
        "RoadSectionID": raw.get("SectionID", ""),
        "RoadName": raw.get("RoadName", ""),
        "geometry": geometry,
        "RoadLength": length_m,
        "SpeedLimit": _infer_speed_limit(raw.get("RoadClass", 0)),
    }


def _infer_speed_limit(road_class: int) -> int:
    """根據 RoadClass 推估速限 (TDX Section API 沒有直接提供速限)。"""
    # RoadClass: 0=國道, 1=省道, 2=快速道路, 3=市區快速, 4=縣道, 5=鄉道, 6=市區道路
    return {
        0: 110, 1: 70, 2: 80, 3: 70, 4: 60, 5: 50, 6: 50,
    }.get(road_class, 40)


def main():
    client_id = os.environ.get("TDX_CLIENT_ID") or os.environ.get("TDX-CLIENT-ID")
    client_secret = os.environ.get("TDX_CLIENT_SECRET") or os.environ.get("TDX-CLIENT-SECRET")

    if not client_id or not client_secret:
        print("[ERROR] 請設定環境變數 TDX_CLIENT_ID / TDX_CLIENT_SECRET 或 .env", file=sys.stderr)
        sys.exit(1)

    print("[INFO] 開始 TDX OAuth2 認證...")
    token = get_access_token(client_id, client_secret)
    print("[INFO] 認證成功")

    print("[INFO] 抓取台北路段資料 (Section API)...")
    raw_sections = fetch_sections(token)
    print(f"[INFO] 共取得 {len(raw_sections)} 筆原始路段")

    print("[INFO] 抓取路段幾何線 (SectionShape API)...")
    shapes = fetch_section_shapes(token)
    print(f"[INFO] 共取得 {len(shapes)} 筆幾何資料")

    # 過濾 bounding box + 擷取欄位 + 合併幾何
    sections = []
    for raw in raw_sections:
        extracted = extract_section(raw)
        if extracted:
            sid = extracted["RoadSectionID"]
            if sid in shapes:
                extracted["geometry_wkt"] = shapes[sid]
            sections.append(extracted)

    matched = sum(1 for s in sections if "geometry_wkt" in s)
    print(f"[INFO] bounding box 過濾後剩餘 {len(sections)} 筆路段（{matched} 筆有完整幾何線）")

    output = {
        "metadata": {
            "source": "TDX Section + SectionShape API",
            "city": "Taipei",
            "bounding_box": {
                "sw": {"latitude": BBOX_SW[0], "longitude": BBOX_SW[1]},
                "ne": {"latitude": BBOX_NE[0], "longitude": BBOX_NE[1]},
            },
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "count": len(sections),
        },
        "road_sections": sections,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"[INFO] 已輸出至 {OUTPUT_PATH}")
    print(f"[INFO] 完成！共 {len(sections)} 筆路段")


if __name__ == "__main__":
    main()
