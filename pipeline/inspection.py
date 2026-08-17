"""
鏡眼 Mirror Eye ── 街景取樣與鏡況判讀管線

流程：
  1. Street View Metadata API 確認該點有無街景（免費，先擋掉無效請求）
  2. Street View Static API 取四方位影像
  3. Gemini on Vertex AI 做結構化判讀
  4. 寫入 BigQuery inspections

環境變數：
  GOOGLE_CLOUD_PROJECT   GCP 專案 ID
  GOOGLE_CLOUD_LOCATION  例 us-central1（asia-east1 目前沒有 gemini-2.5-flash）
  MAPS_API_KEY           Maps Platform 金鑰（需啟用 Street View Static + Metadata）
"""

import os
import json
import uuid
import base64
import datetime as dt
from dataclasses import dataclass, asdict

import requests
from google import genai
from google.genai import types
from google.cloud import bigquery

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
MAPS_KEY = os.environ["MAPS_API_KEY"]
MODEL = "gemini-2.5-flash"
MODEL_VERSION = f"{MODEL}/rubric-v1"

SV_META = "https://maps.googleapis.com/maps/api/streetview/metadata"
SV_IMG = "https://maps.googleapis.com/maps/api/streetview"

client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
bq = bigquery.Client(project=PROJECT)


# ---------------------------------------------------------------
# 判讀 rubric ── 直接寫進 prompt，避免模型自由發揮
# ---------------------------------------------------------------
RUBRIC = """
你是道路設施稽核員，正在檢視一張街景影像，判斷畫面中的道路反射鏡（凸面鏡）狀況。

先判斷畫面中是否存在道路反射鏡。若不存在，mirror_present 設為 false，
其餘評分欄位一律填 0，reason 說明畫面中看到什麼。

若存在，針對以下五個維度各給 0 到 3 分（0 = 完好，3 = 嚴重）：

dirt_score      鏡面髒污、積塵、霧化、水漬，導致成像模糊
angle_score     鏡體角度偏移、下垂、扭轉，反射方向偏離原設計盲區
damage_score    鏡面破裂、缺角、剝落、鏡體變形
occlusion_score 植栽、招牌、電線、違停車輛遮蔽鏡面或阻擋駕駛視線
corrosion_score 支架鏽蝕、螺絲鬆脫、桿體傾斜

評分原則：
- 只依畫面可見證據評分，不要臆測。
- 若影像本身模糊、逆光、解析度不足以判斷某維度，該維度給 0，
  並降低 confidence，在 reason 中說明受限原因。
- confidence 為 0 到 1 的浮點數，代表你對整體判讀的把握程度。

reason 請用繁體中文，兩句以內，具體描述看到的證據，
例如「鏡面下緣覆蓋灰塵約四成，鏡體向下偏移，推估無法涵蓋原設計盲區」。
不要寫「可能」「也許」這類無資訊的修飾。
"""

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "mirror_present": {"type": "BOOLEAN"},
        "dirt_score": {"type": "INTEGER"},
        "angle_score": {"type": "INTEGER"},
        "damage_score": {"type": "INTEGER"},
        "occlusion_score": {"type": "INTEGER"},
        "corrosion_score": {"type": "INTEGER"},
        "confidence": {"type": "NUMBER"},
        "reason": {"type": "STRING"},
    },
    "required": [
        "mirror_present", "dirt_score", "angle_score", "damage_score",
        "occlusion_score", "corrosion_score", "confidence", "reason",
    ],
}

# 五維度權重，加總後正規化到 0~100
WEIGHTS = {
    "dirt_score": 0.25,
    "angle_score": 0.30,   # 角度偏移最危險：駕駛會相信一面錯的鏡子
    "damage_score": 0.20,
    "occlusion_score": 0.15,
    "corrosion_score": 0.10,
}


@dataclass
class Inspection:
    inspection_id: str
    mirror_id: str | None
    intersection_id: str | None
    image_uri: str
    image_source: str
    captured_at: str | None
    mirror_present: bool
    dirt_score: int
    angle_score: int
    damage_score: int
    occlusion_score: int
    corrosion_score: int
    condition_score: float
    confidence: float
    reason: str
    needs_human_review: bool
    model_version: str
    created_at: str


def streetview_metadata(lat: float, lng: float) -> dict | None:
    """免費呼叫，先確認有無街景並取得拍攝年月。沒有就別浪費 Static API 額度。"""
    r = requests.get(
        SV_META,
        params={"location": f"{lat},{lng}", "key": MAPS_KEY},
        timeout=10,
    )
    data = r.json()
    return data if data.get("status") == "OK" else None


def streetview_image(lat: float, lng: float, heading: int,
                     fov: int = 70, pitch: int = 0) -> bytes:
    r = requests.get(
        SV_IMG,
        params={
            "location": f"{lat},{lng}",
            "size": "640x640",
            "heading": heading,
            "fov": fov,       # 60~80 較接近人眼視角
            "pitch": pitch,
            "key": MAPS_KEY,
            "return_error_code": "true",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.content


def judge(image_bytes: bytes, mime: str = "image/jpeg") -> dict:
    resp = client.models.generate_content(
        model=MODEL,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
            RUBRIC,
        ],
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
        ),
    )
    return json.loads(resp.text)


def condition_score(v: dict) -> float:
    """五維度加權 → 0~100 劣化分數。"""
    raw = sum(v[k] * w for k, w in WEIGHTS.items())   # 0~3
    return round(raw / 3.0 * 100, 1)


def build_inspection(v: dict, *, image_uri: str, image_source: str,
                     mirror_id: str | None, intersection_id: str | None,
                     captured_at: str | None) -> Inspection:
    score = condition_score(v) if v["mirror_present"] else 0.0
    return Inspection(
        inspection_id=str(uuid.uuid4()),
        mirror_id=mirror_id,
        intersection_id=intersection_id,
        image_uri=image_uri,
        image_source=image_source,
        captured_at=captured_at,
        mirror_present=v["mirror_present"],
        dirt_score=v["dirt_score"],
        angle_score=v["angle_score"],
        damage_score=v["damage_score"],
        occlusion_score=v["occlusion_score"],
        corrosion_score=v["corrosion_score"],
        condition_score=score,
        confidence=float(v["confidence"]),
        reason=v["reason"],
        # 低信心或高分低信心 → 人工複查，簡報上講 human-in-the-loop
        needs_human_review=(float(v["confidence"]) < 0.6),
        model_version=MODEL_VERSION,
        created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )


def write(rows: list[Inspection]) -> None:
    if not rows:
        return
    errors = bq.insert_rows_json(
        f"{PROJECT}.mirror_eye.inspections", [asdict(r) for r in rows]
    )
    if errors:
        raise RuntimeError(f"BigQuery insert failed: {errors}")


def inspect_point(lat: float, lng: float, *,
                  mirror_id: str | None = None,
                  intersection_id: str | None = None,
                  headings: tuple[int, ...] = (0, 90, 180, 270)) -> list[Inspection]:
    """對單一座標取四方位街景並判讀。回傳所有判讀結果。"""
    meta = streetview_metadata(lat, lng)
    if meta is None:
        print(f"[skip] no street view at {lat},{lng}")
        return []

    captured_at = meta.get("date")   # 形如 2023-07
    results: list[Inspection] = []

    for h in headings:
        img = streetview_image(lat, lng, h)
        verdict = judge(img)
        uri = f"{SV_IMG}?location={lat},{lng}&heading={h}"
        results.append(build_inspection(
            verdict,
            image_uri=uri,
            image_source="streetview",
            mirror_id=mirror_id,
            intersection_id=intersection_id,
            captured_at=f"{captured_at}-01T00:00:00Z" if captured_at else None,
        ))

    write(results)
    return results


if __name__ == "__main__":
    import sys
    lat, lng = float(sys.argv[1]), float(sys.argv[2])
    for r in inspect_point(lat, lng):
        if r.mirror_present:
            print(f"heading -> 劣化 {r.condition_score} "
                  f"(信心 {r.confidence:.2f})  {r.reason}")
