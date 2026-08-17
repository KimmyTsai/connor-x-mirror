# 鏡眼 Mirror Eye ── 反射鏡安裝與維護系統

DevJam TW 2026｜Google Cloud 賽道

---

## 系統做什麼

1. **維護需求**：對已有的反射鏡，用街景與民眾照片判讀鏡況，
   算出「維護優先序 = 鏡況劣化 × 該點位風險」
2. **設置需求**：對沒有鏡子的路口，用事故資料與路口幾何算出設置需求分級
3. **民眾回報**：把自由文字轉成結構化事實，並作為模型的校正訊號
4. **陳情書生成**：一鍵產出可直接寄給交通局的申請書

---

## 兩個分數，不是一個

| | 設置需求 | 維護需求 |
|---|---|---|
| 對象 | 沒有鏡子的路口 | 已有鏡子的點位 |
| 輸入 | 事故、幾何、遮蔽、敏感設施 | 街景影像判讀 × 點位風險 |
| 動作 | 新設工程 | 派工巡修 |
| 視圖 | `v_installation_need` | `v_maintenance_priority` |

**核心公式**（`schema.sql` 中）：

```
priority_score = condition_score × (0.5 + risk_score / 100)
```

同樣髒的兩面鏡子，在國小通學路口的那面要先修。
這一行就是這個系統與「純影像分類器」的差別。

---

## 目錄結構

```
mirror-eye/
├── schema.sql            BigQuery 資料表與評分視圖
├── pipeline/inspection.py 街景取樣 + Gemini 判讀
├── api/main.py           Cloud Run API
├── web/index.html        地圖前端
└── README.md
```

---

## 建置步驟

### 0. 環境變數

```bash
export PROJECT=your-project-id
export REGION=asia-east1            # Cloud Run 部署位置，離台灣使用者近
export GEMINI_LOCATION=us-central1  # Vertex AI 呼叫用；asia-east1 目前沒有 gemini-2.5-flash（實測 404）
export MAPS_API_KEY=your-maps-key
gcloud config set project $PROJECT
```

### 1. 啟用 API

```bash
gcloud services enable \
  aiplatform.googleapis.com \
  bigquery.googleapis.com \
  run.googleapis.com \
  storage.googleapis.com \
  firestore.googleapis.com \
  maps-backend.googleapis.com \
  street-view-image-backend.googleapis.com
```

Maps Platform 金鑰需啟用：
Street View Static API、Street View Metadata API、Maps JavaScript API、Geocoding API

### 2. 建立資料表

```bash
bq query --use_legacy_sql=false < schema.sql
```

### 3. 灌入基礎資料

- **路口節點**：政府路網或 OSM 匯出，載入 `intersections`
- **事故資料**：道安資訊查詢網開放資料，載入 `accidents`
- **反射鏡點位**：縣市開放資料平台搜「反射鏡 / 凸面鏡 / 照後鏡」，載入 `mirrors`

> 若查無反射鏡開放資料，改由 `inspection.py` 掃街景自動偵測，
> `mirrors.source` 填 `streetview_detected` ──
> 這條路的故事更好：等於替城市建立第一份不存在的清冊。

### 4. 跑判讀管線

```bash
pip install -r requirements.txt
export GOOGLE_CLOUD_PROJECT=$PROJECT GOOGLE_CLOUD_LOCATION=$GEMINI_LOCATION
python pipeline/inspection.py 22.9997 120.2270
```

### 5. 部署 API

```bash
cd api
gcloud run deploy mirror-eye-api \
  --source . --region $REGION --allow-unauthenticated \
  --set-env-vars GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=$GEMINI_LOCATION
```

### 6. 部署前端

把 `web/index.html` 裡的 `YOUR_MAPS_API_KEY` 換掉，並設定 API base：

```html
<script>window.API_BASE = "https://mirror-eye-api-xxxx.run.app";</script>
```

```bash
firebase init hosting   # public 目錄設為 web
firebase deploy --only hosting
```

---

## 成本控制

**Street View Static API 計費，Metadata API 免費。**

`inspection.py` 一律先呼叫 Metadata 確認該座標有無街景、拍攝年月，
通過才抓圖。否則會在半夜燒掉額度，抓回一堆空白圖。

每個路口取四方位（heading 0/90/180/270），`fov` 設 60~80 較接近人眼。

---

## 判讀 rubric

五個維度各 0~3 分，權重見 `pipeline/inspection.py` 的 `WEIGHTS`：

| 維度 | 權重 | 說明 |
|---|---|---|
| 角度偏移 | 0.30 | 最危險：駕駛會相信一面錯的鏡子 |
| 鏡面髒污 | 0.25 | 積塵、霧化、水漬 |
| 破損缺角 | 0.20 | 破裂、剝落、變形 |
| 遮蔽 | 0.15 | 植栽、招牌、違停 |
| 鏽蝕傾斜 | 0.10 | 支架與桿體 |

**信心低於 0.6 自動排入人工複查**（`needs_human_review`）。
簡報時講 human-in-the-loop，這是加分項。

---

## 民眾回報的處理原則

**回報數不直接加進分數。** 兩個理由：

1. 可以灌票
2. 更根本的問題：**陳情量高的往往是比較懂得使用制度的社區**。
   純靠陳情分配資源，會讓最需要的地方永遠排在後面 ──
   那正是現行「出事才裝」制度的缺陷。

正確用法：

- **獨立顯示**：客觀分數與民意訊號並列（`report_count` 欄位）
- **結構化抽取**：Gemini 從文字抽出時段、族群、危險型態，作為加權修正
- **歧異挖掘**：回報多但分數低的點位自動標記 `flag_review`，排進人工複查

第三點是簡報亮點：**把民眾當成模型的校正訊號，而不是投票機。**

---

## Google Cloud 服務對照

| 服務 | 用途 |
|---|---|
| Vertex AI（Gemini 2.5 Flash / Pro） | 鏡況判讀、事實抽取、陳情書生成 |
| BigQuery | 清冊、判讀歷史、事故整合 |
| BigQuery GIS | `ST_DWithin` 空間關聯、視距計算 |
| Cloud Run | 無伺服器 API |
| Cloud Storage | 民眾上傳照片 |
| Street View Static / Metadata API | 影像來源 |
| Maps JavaScript API | 前端地圖圖層 |
| Firebase Hosting | 網站託管 |

**選型取捨（簡報要講）**：
- 判讀用 Flash（量大、要便宜），陳情書生成用 Pro（品質優先）
- 空間關聯交給 BigQuery GIS，不自己寫幾何運算
- `temperature=0.1` ＋ `response_schema` 強制結構化輸出，確保評分可重現

---

## 開發順序（垂直切片優先）

**不要四人平行開發四個模組，最後才整合。**

先由兩人打通最小端到端管線：

```
一張街景照片 → Cloud Run → Gemini 判讀 → BigQuery → 地圖上一個點變紅
```

通了之後每加一個功能都是在能動的系統上疊加，而不是賭最後能不能接起來。
另兩人同時外出實拍與整理事故資料。
