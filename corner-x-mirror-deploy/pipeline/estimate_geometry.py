"""
Corner.X 城市反射鏡數據網 ── 轉角視距／道路寬度 AI 估算（方法二）

背景：
  intersections 表裡 sight_distance_m／road_width_m 若無實測資料就是 NULL，
  v_intersection_risk 會把這視為「無資料」而非「安全」，前端顯示「無資料」，
  不會偷偷當成 0 分安全值算進風險分數。

  這支腳本不是要取代實測，是在完全沒資料時提供一個「有憑有據的估算值」，
  而且刻意跟實測分開標記（source = 'ai_estimated'），不偽裝成量測值：
    - v_intersection_risk 算分數時，AI 估算值跟實測值一視同仁地拿去用
      （分數本來就只看數字，不看來源）
    - 但前端顯示層會依 source 欄位加註「估算」標籤，讓使用者知道
      這個數字的可信度跟實測不同 —— 這是刻意保留的差異，不要在前端拿掉

做法：
  重用 detect.py 已經在打的 Street View 抓取邏輯（同一組 Metadata + Static
  API），對每個路口抓 4 個方位的照片，讓 Gemini 依 rubric 給出：
    - road_width_class：車道數估計 → 換算大略公尺數（不是精確測量）
    - sight_distance_class：轉角視線被遮擋的程度分級 → 換算大略公尺數
  只補「目前是 NULL」的路口，不覆蓋已有實測或已估算過的值。

限制（務必讓使用者知道，不要含糊帶過）：
  單張街景照片沒有深度資訊，這裡的「公尺數」是分級後的代表值，不是
  逐公分量出來的 —— 跟 detect.py 裡 ASSUMED_DIST_M 的假設距離問題是同一個
  根本限制。準確度明顯低於實測或 OSM 開放資料，僅在完全沒資料時作為
  「有總比沒有好、但要標明來源」的補值方案。

用法：
  python pipeline/estimate_geometry.py intersections.csv   # 欄位: id,lat,lng
"""

import os
import csv
import json
import sys
import datetime as dt

from google import genai
from google.genai import types
from google.cloud import bigquery

from detect import has_streetview, fetch, PITCH

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
MODEL = "gemini-2.5-flash"
MODEL_VERSION = f"{MODEL}/geometry-estimate-v1"

# 估算用廣角，四個方位涵蓋路口四向，跟 detect.py 的 8 方位掃描不同用途
EST_HEADINGS = (0, 90, 180, 270)
EST_FOV = 60

client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
bq = bigquery.Client(project=PROJECT)


# ---------------------------------------------------------------
# 估算 rubric ── 分級而非要求精確公尺數，避免模型編造假精度
# ---------------------------------------------------------------
RUBRIC = """
你是道路設施稽核員，正在依序檢視同一個路口四個方位的街景影像，
估計「道路寬度」與「轉角視距」的大略分級。不要臆測精確公尺數，
只依畫面可見證據判斷屬於哪一級。

road_width_class（依可見車道數與路面寬度估計，取四張中最寬的方向）：
  narrow    單線道或窄巷，路面寬度約小於 6 公尺
  medium    雙線道，約 6~8 公尺
  wide      三線道以上或有分向設施，約大於 8 公尺
  unknown   畫面不足以判斷（例如被遮擋、角度看不清路面）

sight_distance_class（依轉角處視線被建物、招牌、植栽、路邊停車等遮擋的程度）：
  poor      視線在很短距離內就被遮擋，駕駛需探頭才能看到來車，約小於 10 公尺
  moderate  視線受部分遮擋，約 10~20 公尺
  good      視野開闊，約大於 20 公尺
  unknown   畫面不足以判斷

針對每個方位的影像，用一句話寫出你觀察到的證據（evidence），
再給出整體路口的 road_width_class 與 sight_distance_class 各一個，
以及 0~1 的信心值 confidence（畫面越模糊、角度越差，信心應該越低）。

不要為了看起來精確而編造具體公尺數，只回傳分級。
"""

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "road_width_class": {"type": "STRING", "enum": ["narrow", "medium", "wide", "unknown"]},
        "sight_distance_class": {"type": "STRING", "enum": ["poor", "moderate", "good", "unknown"]},
        "evidence": {"type": "STRING"},
        "confidence": {"type": "NUMBER"},
    },
    "required": ["road_width_class", "sight_distance_class", "evidence", "confidence"],
}

# 分級 → 代表公尺數。取每個級距的中間值，不是精確量測，僅供排序用途。
ROAD_WIDTH_M = {"narrow": 5.0, "medium": 7.0, "wide": 9.0}
SIGHT_DISTANCE_M = {"poor": 7.0, "moderate": 15.0, "good": 25.0}

MIN_CONF = 0.5   # 低於這個信心值的估算不寫入，寧可繼續顯示「無資料」


def estimate(intersection_id: str, lat: float, lng: float) -> dict | None:
    meta = has_streetview(lat, lng)
    if meta is None:
        print(f"[skip] {intersection_id} 無街景")
        return None

    plat, plng = meta["location"]["lat"], meta["location"]["lng"]
    parts = []
    for h in EST_HEADINGS:
        try:
            img = fetch(plat, plng, h, EST_FOV)
        except Exception:
            continue
        parts.append(types.Part.from_bytes(data=img, mime_type="image/jpeg"))

    if not parts:
        print(f"[skip] {intersection_id} 街景抓取失敗")
        return None

    resp = client.models.generate_content(
        model=MODEL,
        contents=[*parts, RUBRIC],
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=SCHEMA,
        ),
    )
    result = json.loads(resp.text)

    if result["confidence"] < MIN_CONF:
        print(f"[low-conf] {intersection_id} 信心 {result['confidence']:.2f}，跳過不寫入")
        return None

    return result


def apply_estimate(intersection_id: str, result: dict) -> None:
    """只補 NULL 欄位，不覆蓋已有實測或已估算過的值。"""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    road_m = ROAD_WIDTH_M.get(result["road_width_class"])
    sight_m = SIGHT_DISTANCE_M.get(result["sight_distance_class"])

    sets, params = [], []
    if road_m is not None:
        sets.append("road_width_m = @road_m, road_width_source = 'ai_estimated'")
        params.append(bigquery.ScalarQueryParameter("road_m", "FLOAT64", road_m))
    if sight_m is not None:
        sets.append("sight_distance_m = @sight_m, sight_distance_source = 'ai_estimated'")
        params.append(bigquery.ScalarQueryParameter("sight_m", "FLOAT64", sight_m))

    if not sets:
        print(f"[skip] {intersection_id} 兩個維度都判斷為 unknown，不寫入")
        return

    params += [
        bigquery.ScalarQueryParameter("iid", "STRING", intersection_id),
    ]
    query = f"""
        UPDATE `{PROJECT}.mirror_eye.intersections`
        SET {", ".join(sets)}, updated_at = TIMESTAMP('{now}')
        WHERE intersection_id = @iid
          AND road_width_m IS NULL AND sight_distance_m IS NULL
    """
    # 只在兩個原本都是 NULL 時才更新，避免覆蓋掉某一項已有的實測/估算值。
    # 若只有其中一項是 NULL，改用下面較嚴謹的分欄位更新。
    job = bq.query(query, job_config=bigquery.QueryJobConfig(query_parameters=params))
    job.result()

    if job.num_dml_affected_rows == 0:
        _apply_partial(intersection_id, road_m, sight_m, now)


def _apply_partial(intersection_id: str, road_m: float | None,
                    sight_m: float | None, now: str) -> None:
    """兩個維度中只有一項原本是 NULL 的情況，分開判斷、分開補值。"""
    if road_m is not None:
        bq.query(f"""
            UPDATE `{PROJECT}.mirror_eye.intersections`
            SET road_width_m = @v, road_width_source = 'ai_estimated',
                updated_at = TIMESTAMP('{now}')
            WHERE intersection_id = @iid AND road_width_m IS NULL
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("v", "FLOAT64", road_m),
            bigquery.ScalarQueryParameter("iid", "STRING", intersection_id),
        ])).result()
    if sight_m is not None:
        bq.query(f"""
            UPDATE `{PROJECT}.mirror_eye.intersections`
            SET sight_distance_m = @v, sight_distance_source = 'ai_estimated',
                updated_at = TIMESTAMP('{now}')
            WHERE intersection_id = @iid AND sight_distance_m IS NULL
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("v", "FLOAT64", sight_m),
            bigquery.ScalarQueryParameter("iid", "STRING", intersection_id),
        ])).result()


def run(csv_path: str) -> None:
    filled, skipped = 0, 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            iid = row["id"]
            result = estimate(iid, float(row["lat"]), float(row["lng"]))
            if result is None:
                skipped += 1
                continue
            apply_estimate(iid, result)
            filled += 1
            print(f"{iid}  路寬={result['road_width_class']}  "
                  f"視距={result['sight_distance_class']}  "
                  f"信心={result['confidence']:.2f}")
            print(f"    {result['evidence']}")
    print(f"\n補值 {filled} 個路口，跳過 {skipped} 個（無街景或信心不足）")


if __name__ == "__main__":
    run(sys.argv[1])
