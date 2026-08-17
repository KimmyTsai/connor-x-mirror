"""
鏡眼 Mirror Eye ── Cloud Run API

端點：
  GET  /api/mirrors          維護優先序（已有鏡子的點位）
  GET  /api/gaps             設置需求排序（沒有鏡子的路口）
  GET  /api/mirrors/{id}     單一鏡子詳情，含分項貢獻
  POST /api/reports          民眾回報，Gemini 抽取結構化事實
  POST /api/reports/{id}/letter  生成陳情書內容

部署：
  gcloud run deploy mirror-eye-api --source . --region asia-east1 \
    --allow-unauthenticated --set-env-vars GOOGLE_CLOUD_PROJECT=$PROJECT
"""

import os
import json
import uuid
import datetime as dt

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from google.genai import types
from google.cloud import bigquery

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

app = FastAPI(title="鏡眼 Mirror Eye API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

bq = bigquery.Client(project=PROJECT)
genai_client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)


def q(sql: str, params: list | None = None) -> list[dict]:
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return [dict(r) for r in bq.query(sql, job_config=cfg).result()]


# ---------------------------------------------------------------
# 地圖圖層
# ---------------------------------------------------------------
@app.get("/api/mirrors")
def list_mirrors(limit: int = 500):
    rows = q(f"""
        SELECT mirror_id, intersection_id,
               ST_Y(geom) AS lat, ST_X(geom) AS lng,
               mirror_present, condition_score, risk_score, priority_score,
               condition_level, confidence, reason, needs_human_review
        FROM `{PROJECT}.mirror_eye.v_maintenance_priority`
        ORDER BY priority_score DESC
        LIMIT @limit
    """, [bigquery.ScalarQueryParameter("limit", "INT64", limit)])
    return {"type": "mirrors", "count": len(rows), "items": rows}


@app.get("/api/gaps")
def list_gaps(limit: int = 200):
    rows = q(f"""
        SELECT intersection_id, city, district, road_names,
               ST_Y(geom) AS lat, ST_X(geom) AS lng,
               accident_count, base_score, need_level,
               report_count, flag_review,
               s_accident, s_sight, s_width, s_vulnerable
        FROM `{PROJECT}.mirror_eye.v_installation_need`
        WHERE base_score > 0
        ORDER BY base_score DESC
        LIMIT @limit
    """, [bigquery.ScalarQueryParameter("limit", "INT64", limit)])
    return {"type": "gaps", "count": len(rows), "items": rows}


@app.get("/api/mirrors/{mirror_id}")
def mirror_detail(mirror_id: str):
    rows = q(f"""
        SELECT * FROM `{PROJECT}.mirror_eye.v_maintenance_priority`
        WHERE mirror_id = @id
    """, [bigquery.ScalarQueryParameter("id", "STRING", mirror_id)])
    if not rows:
        raise HTTPException(404, "mirror not found")
    r = rows[0]
    # 分項貢獻：前端畫成貢獻條，這是可解釋性的來源
    r["breakdown"] = [
        {"label": "角度偏移", "score": r["angle_score"], "max": 3, "weight": 0.30},
        {"label": "鏡面髒污", "score": r["dirt_score"], "max": 3, "weight": 0.25},
        {"label": "破損缺角", "score": r["damage_score"], "max": 3, "weight": 0.20},
        {"label": "遮蔽",     "score": r["occlusion_score"], "max": 3, "weight": 0.15},
        {"label": "鏽蝕傾斜", "score": r["corrosion_score"], "max": 3, "weight": 0.10},
    ]
    return r


# ---------------------------------------------------------------
# 民眾回報
# ---------------------------------------------------------------
class ReportIn(BaseModel):
    lat: float
    lng: float
    report_type: str          # new_request | maintenance
    text: str
    photo_uri: str | None = None
    intersection_id: str | None = None
    mirror_id: str | None = None


EXTRACT_PROMPT = """
以下是民眾對某個路口危險狀況的自由描述。
抽取結構化事實，只根據文字中明確提到的內容，沒提到就填空字串。

hazard_time  危險發生的時段，例：上學時段、深夜、下班尖峰
hazard_group 受影響的族群，例：國小學童、長者、機車騎士
hazard_desc  危險的具體型態，一句話，繁體中文

不要推論、不要補充民眾沒說的內容。
"""

EXTRACT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "hazard_time": {"type": "STRING"},
        "hazard_group": {"type": "STRING"},
        "hazard_desc": {"type": "STRING"},
    },
    "required": ["hazard_time", "hazard_group", "hazard_desc"],
}


@app.post("/api/reports")
def create_report(body: ReportIn):
    resp = genai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[EXTRACT_PROMPT, body.text],
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=EXTRACT_SCHEMA,
        ),
    )
    facts = json.loads(resp.text)

    report_id = str(uuid.uuid4())
    row = {
        "report_id": report_id,
        "intersection_id": body.intersection_id,
        "mirror_id": body.mirror_id,
        "geom": f"POINT({body.lng} {body.lat})",
        "report_type": body.report_type,
        "raw_text": body.text,
        "photo_uri": body.photo_uri,
        "hazard_time": facts["hazard_time"],
        "hazard_group": facts["hazard_group"],
        "hazard_desc": facts["hazard_desc"],
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    errors = bq.insert_rows_json(f"{PROJECT}.mirror_eye.reports", [row])
    if errors:
        raise HTTPException(500, f"insert failed: {errors}")

    return {"report_id": report_id, "extracted": facts}


# ---------------------------------------------------------------
# 陳情書生成
# ---------------------------------------------------------------
LETTER_PROMPT = """
根據以下資料，撰寫一份向地方政府交通局申請設置道路反射鏡的陳情書。

要求：
- 繁體中文，公文語氣，但不要過度文言
- 結構：申請事由、現場狀況、事故事實、具體請求
- 事故件數與座標務必原樣引用，不要改動數字
- 不要杜撰任何資料中沒有的事實
- 全文 400 字以內
"""


@app.post("/api/reports/{report_id}/letter")
def generate_letter(report_id: str):
    rows = q(f"""
        SELECT r.raw_text, r.hazard_time, r.hazard_group, r.hazard_desc,
               ST_Y(r.geom) AS lat, ST_X(r.geom) AS lng,
               g.road_names, g.accident_count, g.base_score, g.need_level
        FROM `{PROJECT}.mirror_eye.reports` r
        LEFT JOIN `{PROJECT}.mirror_eye.v_installation_need` g
          ON r.intersection_id = g.intersection_id
        WHERE r.report_id = @id
    """, [bigquery.ScalarQueryParameter("id", "STRING", report_id)])
    if not rows:
        raise HTTPException(404, "report not found")

    resp = genai_client.models.generate_content(
        model="gemini-2.5-pro",
        contents=[LETTER_PROMPT, json.dumps(rows[0], ensure_ascii=False, default=str)],
        config=types.GenerateContentConfig(temperature=0.3),
    )
    return {"report_id": report_id, "letter": resp.text}


@app.get("/healthz")
def healthz():
    return {"ok": True}
