"""
Corner.X 城市反射鏡數據網 ── Cloud Run API

端點：
  GET  /api/mirrors          維護優先序（已有鏡子的點位）
  GET  /api/gaps             設置需求排序（沒有鏡子的路口）
  GET  /api/mirrors/{id}     單一鏡子詳情，含分項貢獻
  POST /api/mirror-photos    民眾上傳鏡子照片＋座標，Gemini 判讀後更新／新增鏡子
  GET  /api/photo/{path}     代理讀取民眾上傳照片（bucket 不公開，一律經 API 讀）
  GET  /api/pending          待審核的民眾新增鏡子（status=pending）
  POST /api/pending/{id}/review  管理者審核（核准轉 active／退回轉 removed）
  POST /api/reports          民眾回報（文字陳情），Gemini 抽取結構化事實
  POST /api/reports/{id}/letter  生成陳情書內容

部署（從 repo 根目錄跑，不是 api/ 目錄——這樣才能同時抓到根目錄的
requirements.txt、Procfile，和 web/ 底下要一起服務的靜態網頁）：
  gcloud run deploy mirror-eye-api --source . --region asia-east1 \
    --allow-unauthenticated --set-env-vars GOOGLE_CLOUD_PROJECT=$PROJECT

靜態網頁（web/index.html、web/report.html）直接掛在這個 FastAPI app 上
一起出去，不是另外部署到 Firebase Hosting——同一個 Cloud Run 服務、
同一個網址，服務對照表上的 Firebase Hosting 是原本規劃但沒採用的方案。

注意：/api/pending 的審核端點目前沒有身分驗證（跟這個專案其他端點一樣，
Cloud Run 是 --allow-unauthenticated）。正式上線前要補管理者登入，
不然任何人都能核准／退回鏡子。
"""

import os
import json
import uuid
import datetime as dt
from pathlib import Path

from fastapi import FastAPI, HTTPException, Form, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from google import genai
from google.genai import types
from google.cloud import bigquery
from google.cloud import storage

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
PHOTO_BUCKET = os.environ.get("PHOTO_BUCKET", f"{PROJECT}-mirror-photos")

app = FastAPI(title="Corner.X 城市反射鏡數據網 API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

bq = bigquery.Client(project=PROJECT)
genai_client = genai.Client(vertexai=True, project=PROJECT, location=LOCATION)
storage_client = storage.Client(project=PROJECT)


def q(sql: str, params: list | None = None) -> list[dict]:
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return [dict(r) for r in bq.query(sql, job_config=cfg).result()]


# ---------------------------------------------------------------
# 地圖圖層
# ---------------------------------------------------------------
@app.get("/api/mirrors")
def list_mirrors(limit: int = 500, status: str = "confirmed"):
    """status=confirmed（預設，已確認的鏡子，維護優先序地圖用）
    ／unconfirmed（Stage 2 未能重新確認，待人工複查，不是鏡子清冊）／all"""
    where = {
        "confirmed": "WHERE mirror_present",
        "unconfirmed": "WHERE NOT mirror_present",
        "all": "",
    }.get(status, "WHERE mirror_present")
    rows = q(f"""
        SELECT mirror_id, intersection_id,
               ST_Y(geom) AS lat, ST_X(geom) AS lng,
               mirror_present, image_uri, condition_score, risk_score, priority_score,
               condition_level, confidence, reason, needs_human_review
        FROM `{PROJECT}.mirror_eye.v_maintenance_priority`
        {where}
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
               sight_distance_m, sight_distance_source,
               road_width_m, road_width_source,
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
# 民眾上傳鏡子照片
#
# 判讀 rubric 跟 pipeline/inspection.py 是同一套，複製一份而不是 import，
# 因為 Cloud Run 用 `--source .` 只會打包 api/ 目錄，抓不到 pipeline/。
# 改動 rubric 時兩邊要一起改。
# ---------------------------------------------------------------
CITIZEN_RUBRIC = """
你是道路設施稽核員，正在檢視一張民眾上傳的道路反射鏡（凸面鏡）照片，判斷鏡況。

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
- 民眾拍攝角度、光線可能不理想，若解析度不足以判斷某維度，該維度給 0，
  並降低 confidence，在 reason 中說明受限原因。
- confidence 為 0 到 1 的浮點數，代表你對整體判讀的把握程度。

reason 請用繁體中文，兩句以內，具體描述看到的證據。
"""

CITIZEN_RESPONSE_SCHEMA = {
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

CITIZEN_WEIGHTS = {
    "dirt_score": 0.25, "angle_score": 0.30, "damage_score": 0.20,
    "occlusion_score": 0.15, "corrosion_score": 0.10,
}


def judge_citizen_photo(image_bytes: bytes, mime: str) -> dict:
    resp = genai_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime), CITIZEN_RUBRIC],
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
            response_schema=CITIZEN_RESPONSE_SCHEMA,
        ),
    )
    return json.loads(resp.text)


def citizen_condition_score(v: dict) -> float:
    raw = sum(v[k] * w for k, w in CITIZEN_WEIGHTS.items())
    return round(raw / 3.0 * 100, 1)


ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_PHOTO_BYTES = 8 * 1024 * 1024   # 8MB，手機拍照常見大小，擋掉異常大檔
NEAR_MIRROR_RADIUS_M = 20            # 這個距離內視為同一支既有鏡子，不新增


@app.post("/api/mirror-photos")
async def submit_mirror_photo(
    lat: float = Form(...),
    lng: float = Form(...),
    note: str = Form(""),
    photo: UploadFile = File(...),
):
    content_type = photo.content_type or "image/jpeg"
    if content_type not in ALLOWED_PHOTO_TYPES:
        raise HTTPException(400, f"不支援的檔案類型：{content_type}")
    content = await photo.read()
    if len(content) > MAX_PHOTO_BYTES:
        raise HTTPException(400, "照片太大，請壓縮到 8MB 以內")
    if not content:
        raise HTTPException(400, "空檔案")

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    blob_name = f"citizen/{uuid.uuid4()}.jpg"
    bucket = storage_client.bucket(PHOTO_BUCKET)
    bucket.blob(blob_name).upload_from_string(content, content_type=content_type)

    # 找 20 公尺內既有的現役鏡子；找不到就當成新地點
    near = q(f"""
        SELECT mirror_id, intersection_id
        FROM `{PROJECT}.mirror_eye.mirrors`
        WHERE status = 'active'
          AND ST_DWITHIN(geom, ST_GEOGPOINT(@lng, @lat), @radius)
        LIMIT 1
    """, [
        bigquery.ScalarQueryParameter("lng", "FLOAT64", lng),
        bigquery.ScalarQueryParameter("lat", "FLOAT64", lat),
        bigquery.ScalarQueryParameter("radius", "FLOAT64", NEAR_MIRROR_RADIUS_M),
    ])

    is_new = not near
    if is_new:
        mirror_id = str(uuid.uuid4())
        intersection_id = None
        # 民眾回報的新鏡子先進 pending，管理者審核過才會出現在公開地圖上——
        # 這比「更新既有鏡子狀況」更強的主張，直接公開容易被灌票或誤判位置污染清冊
        #
        # 用 DML INSERT 而不是 insert_rows_json（streaming insert）：
        # streaming insert 寫入的資料最多 90 分鐘會卡在 buffer 裡不能 UPDATE，
        # 但審核動作（/api/pending/{id}/review）馬上就要能改 status，兩者衝突。
        q(f"""
            INSERT INTO `{PROJECT}.mirror_eye.mirrors`
              (mirror_id, intersection_id, geom, source, installed_year, status, updated_at)
            VALUES
              (@mirror_id, NULL, ST_GEOGPOINT(@lng, @lat), 'citizen', NULL, 'pending', CURRENT_TIMESTAMP())
        """, [
            bigquery.ScalarQueryParameter("mirror_id", "STRING", mirror_id),
            bigquery.ScalarQueryParameter("lng", "FLOAT64", lng),
            bigquery.ScalarQueryParameter("lat", "FLOAT64", lat),
        ])
    else:
        mirror_id = near[0]["mirror_id"]
        intersection_id = near[0]["intersection_id"]

    verdict = judge_citizen_photo(content, content_type)
    score = citizen_condition_score(verdict) if verdict["mirror_present"] else 0.0

    inspection_row = {
        "inspection_id": str(uuid.uuid4()),
        "mirror_id": mirror_id,
        "intersection_id": intersection_id,
        "image_uri": f"/api/photo/{blob_name}",
        "image_source": "citizen",
        "captured_at": now,
        "mirror_present": verdict["mirror_present"],
        "dirt_score": verdict["dirt_score"],
        "angle_score": verdict["angle_score"],
        "damage_score": verdict["damage_score"],
        "occlusion_score": verdict["occlusion_score"],
        "corrosion_score": verdict["corrosion_score"],
        "condition_score": score,
        "confidence": float(verdict["confidence"]),
        "reason": verdict["reason"],
        # 新鏡子一律排審核；既有鏡子沿用信心閾值
        "needs_human_review": is_new or float(verdict["confidence"]) < 0.6,
        "model_version": "gemini-2.5-flash/rubric-v1-citizen",
        "created_at": now,
    }
    errors = bq.insert_rows_json(f"{PROJECT}.mirror_eye.inspections", [inspection_row])
    if errors:
        raise HTTPException(500, f"insert failed: {errors}")

    return {
        "mirror_id": mirror_id,
        "is_new_mirror": is_new,
        "status": "pending" if is_new else "active",
        "mirror_present": verdict["mirror_present"],
        "condition_score": score,
        "reason": verdict["reason"],
    }


@app.get("/api/photo/{blob_path:path}")
def get_photo(blob_path: str):
    """代理讀取 bucket 裡的照片——bucket 本身不公開，一律經這個端點讀取。"""
    blob = storage_client.bucket(PHOTO_BUCKET).blob(blob_path)
    if not blob.exists():
        raise HTTPException(404, "photo not found")
    data = blob.download_as_bytes()
    return Response(content=data, media_type=blob.content_type or "image/jpeg")


@app.get("/api/pending")
def list_pending():
    """民眾回報但還沒審核的新鏡子——不在 v_maintenance_priority 裡
    （那個視圖只收 status='active'），管理者從這裡看照片決定核准或退回。"""
    rows = q(f"""
        WITH latest AS (
          SELECT * EXCEPT(rn) FROM (
            SELECT i.*, ROW_NUMBER() OVER (
              PARTITION BY mirror_id ORDER BY created_at DESC
            ) AS rn
            FROM `{PROJECT}.mirror_eye.inspections` i
            WHERE mirror_id IS NOT NULL
          ) WHERE rn = 1
        )
        SELECT m.mirror_id, ST_Y(m.geom) AS lat, ST_X(m.geom) AS lng, m.updated_at,
               l.image_uri, l.mirror_present, l.condition_score, l.confidence, l.reason
        FROM `{PROJECT}.mirror_eye.mirrors` m
        JOIN latest l USING (mirror_id)
        WHERE m.status = 'pending'
        ORDER BY m.updated_at DESC
    """)
    return {"type": "pending", "count": len(rows), "items": rows}


class ReviewIn(BaseModel):
    approve: bool


@app.post("/api/pending/{mirror_id}/review")
def review_pending(mirror_id: str, body: ReviewIn):
    new_status = "active" if body.approve else "removed"
    q(f"""
        UPDATE `{PROJECT}.mirror_eye.mirrors`
        SET status = @status, updated_at = CURRENT_TIMESTAMP()
        WHERE mirror_id = @id
    """, [
        bigquery.ScalarQueryParameter("status", "STRING", new_status),
        bigquery.ScalarQueryParameter("id", "STRING", mirror_id),
    ])
    return {"mirror_id": mirror_id, "status": new_status}


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


# ---------------------------------------------------------------
# 靜態網頁 —— 一定要放在所有 /api/* 路由後面，
# 用 "/" 掛載會接住所有沒被前面路由匹配到的路徑，順序反過來會蓋掉 API
# ---------------------------------------------------------------
WEB_DIR = Path(__file__).resolve().parent.parent / "web"
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
