# Corner.X 城市反射鏡數據網 ── 反射鏡安裝與維護系統

DevJam TW 2026｜Google Cloud 賽道

---

## 系統做什麼

1. **維護需求**：對已有的反射鏡，用街景與民眾照片判讀鏡況，
   算出「維護優先序 = 鏡況劣化 × 該點位風險」
2. **設置需求**：對沒有鏡子的路口，用事故資料與路口幾何算出設置需求分級
3. **民眾回報**：把自由文字轉成結構化事實，並作為模型的校正訊號

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
corner-x-mirror/
├── schema.sql              BigQuery 資料表與評分視圖
├── pipeline/
│   ├── detect.py           Stage 1：街景廣角掃描找出鏡子座標 → 寫入 mirrors
│   ├── inspection.py       Stage 2：變焦特寫 + Gemini 判讀 → 寫入 inspections
│   ├── link.py             把 mirrors 逐一送進 Stage 2 判讀
│   └── validate.py         precision / recall 驗證
├── api/main.py             Cloud Run API
├── web/index.html          管理者地圖（維護優先序／待複查／設置需求／待審核）
├── web/report.html         民眾上傳鏡子照片＋位置的行動版頁面
├── tests/                  純函式單元測試（不需要 GCP 憑證）
└── README.md
```

---

## 建置步驟

以下先講「本機跑起來看網站」（開發、demo 用，最快），
最後才是正式部署到 Cloud Run／Firebase 的版本。兩條路徑共用步驟 0-3。

### 0. 前提

- Python 3.10+，`pip install -r requirements.txt`（跑測試另外要 `pip install -r requirements-dev.txt`）
- 一個已啟用帳單、Vertex AI／BigQuery／Maps Platform 的 GCP 專案
- `gcloud auth login` 且 `gcloud auth application-default login`
  （Vertex AI 用 ADC，不是 API 金鑰；沒設定的話 `genai.Client(vertexai=True,...)` 會連不上）
- 一把 Maps API 金鑰，需啟用：Street View Static API、Street View Metadata API、
  Maps JavaScript API（前端地圖用）

### 1. 環境變數

**PowerShell（Windows，本專案主要開發環境）：**

```powershell
$env:GOOGLE_CLOUD_PROJECT = "your-project-id"
$env:GOOGLE_CLOUD_LOCATION = "us-central1"   # 不要用 asia-east1，這個 region 沒有 gemini-2.5-flash（實測 404）
$env:MAPS_API_KEY = "your-maps-key"
gcloud config set project $env:GOOGLE_CLOUD_PROJECT
```

**bash / macOS / Linux：**

```bash
export GOOGLE_CLOUD_PROJECT=your-project-id
export GOOGLE_CLOUD_LOCATION=us-central1
export MAPS_API_KEY=your-maps-key
gcloud config set project $GOOGLE_CLOUD_PROJECT
```

之後每個新開的終端機視窗都要重設一次這三個變數
（PowerShell 關掉視窗不會保留 `$env:`）。

### 2. 啟用 API、建立資料表

```bash
gcloud services enable \
  aiplatform.googleapis.com \
  bigquery.googleapis.com \
  run.googleapis.com \
  maps-backend.googleapis.com \
  street-view-image-backend.googleapis.com

# 注意 --location，跟 GOOGLE_CLOUD_LOCATION 是兩件事：
# 這裡是 BigQuery 資料集位置（schema.sql 裡寫死 asia-east1），
# 不指定的話 bq 預設用 US，執行 schema.sql 會直接報錯
bq --location=asia-east1 query --use_legacy_sql=false < schema.sql
```

### 3. 灌資料：偵測範圍內的反射鏡

沒有現成的反射鏡開放資料時（多數縣市目前查不到），用 `detect.py` 掃街景自動建清冊——
這是這個專案的核心賣點，不是退而求其次的作法。

```bash
cd pipeline   # 一定要在 pipeline/ 目錄內執行，validate.py/link.py 用相對 import 抓 detect.py

# 準備一份 csv：id,lat,lng（路口座標，可以自己列，或用 OSM Overpass API 抓某行政區界內的真實路口）
python detect.py intersections.csv     # Stage 1：找出鏡子概略座標，寫進 BigQuery mirrors 表
python link.py --only-new              # Stage 2：對每支新鏡子做變焦判讀，寫進 inspections 表
```

`detect.py` 一個路口要打 8 次 Gemini（8 方位掃描），路口一多很容易撞到 Vertex AI
的速率限制（429 RESOURCE_EXHAUSTED）；撞到就等個一兩分鐘再從卡住的路口繼續，
不用整批重跑。

只想測單一座標的判讀，不想跑完整偵測流程：

```bash
python inspection.py 22.9997 120.2270
```

### 4a. 本機跑起來看網站（推薦先做這步）

**啟動 API：**

```bash
cd api
python -m uvicorn main:app --host 127.0.0.1 --port 8080
```

看到 `Uvicorn running on http://127.0.0.1:8080` 就是啟動成功，這個終端機視窗要保持開著。
另開一個視窗確認：瀏覽器開 `http://127.0.0.1:8080/api/mirrors`，應該要看到 JSON。

**開網站：**

`web/index.html` 裡的 `YOUR_MAPS_API_KEY` 是刻意留著的佔位字串——
**金鑰不進版控**，這條規矩是這個專案吃過一次真實教訓換來的
（第一版曾經把帳號密碼跟金鑰明碼寫進 `pw.txt` 一起 commit 上 GitHub）。
所以本機測試時複製一份出去改，不要直接改 repo 裡的檔案：

```bash
cp web/index.html /tmp/index_test.html      # 或隨便一個 repo 外的路徑
# 把 /tmp/index_test.html 裡兩處 YOUR_MAPS_API_KEY 換成你自己的金鑰
```

換好金鑰後開網頁，兩種方式都可以：

- 直接用瀏覽器打開那個檔案（`file:///tmp/index_test.html`）
- 或在檔案所在目錄跑 `python -m http.server 8081`，開 `http://127.0.0.1:8081/index_test.html`

網頁預設會打 `http://localhost:8080` 抓資料（跟上面 API 啟動的 port 要一致）；
要指到別的位置，在 `<script>` 區塊前加一行
`<script>window.API_BASE = "http://your-host:port";</script>`。

打開後應該看到地圖跟三個分頁：**維護優先序**（已確認的鏡子）、
**待複查**（Stage 2 沒能重新確認、需要人工看照片判斷的點位）、**設置需求**
（沒有鏡子的路口缺口分析，需要先灌 `intersections`/`accidents` 資料才有內容）。

### 4b. 正式部署（Cloud Run + Firebase Hosting）

本機測試沒問題後才做這步。

```bash
cd api
gcloud run deploy mirror-eye-api \
  --source . --region asia-east1 --allow-unauthenticated \
  --set-env-vars GOOGLE_CLOUD_PROJECT=$GOOGLE_CLOUD_PROJECT,GOOGLE_CLOUD_LOCATION=$GOOGLE_CLOUD_LOCATION
```

`web/index.html` 一樣先複製一份換掉 `YOUR_MAPS_API_KEY`（部署上去的網站是公開網址，
Maps JavaScript API 金鑰本來就是前端可見的東西，但金鑰仍然不該進 git 歷史，
部署時用 CI 變數注入或部署腳本替換），並在 `<script>` 標籤前加：

```html
<script>window.API_BASE = "https://mirror-eye-api-xxxx.run.app";</script>
```

```bash
firebase init hosting   # public 目錄設為 web
firebase deploy --only hosting
```

### 疑難排解

| 現象 | 原因 |
|---|---|
| `bq query` 報 `Location ... is not consistent` | 沒加 `--location=asia-east1` |
| Gemini 呼叫回 404 `Publisher model ... was not found` | `GOOGLE_CLOUD_LOCATION` 設成 `asia-east1` 了，這個 region 沒有 `gemini-2.5-flash` |
| `import detect` / `import inspection` 相關錯誤 | 沒有在 `pipeline/` 目錄內執行 |
| `ImportError: cannot import name 'dataclass' ...`（或其他標準庫模組匯入錯誤） | 曾經發生過 `pipeline/inspect.py` 跟標準庫 `inspect` 模組同名的問題，現已改名 `inspection.py`；如果又看到類似錯誤，檢查是不是哪支腳本檔名撞到標準庫模組 |
| `429 RESOURCE_EXHAUSTED` | Vertex AI 速率限制，等 1-2 分鐘再繼續，不用整批重跑 |
| 網站地圖是空的、但 API 直接 curl 有資料 | 通常是 `YOUR_MAPS_API_KEY` 還沒換掉，或換的那份檔案跟你打開的不是同一份 |
| 審核（`/api/pending/{id}/review`）回 500，錯誤訊息提到 `streaming buffer` | 那筆鏡子是幾秒／幾分鐘前剛插入的，BigQuery streaming insert 的資料最多 90 分鐘內不能 UPDATE/DELETE，等一下再試 |

---

## 民眾回報鏡子照片

`web/report.html` 是給一般用路人用的行動版頁面：拍照＋標位置＋送出，
Gemini 判讀後自動更新既有鏡子的維護紀錄，或者（找不到 20 公尺內的既有鏡子時）
建一支新鏡子並標成 `pending`，不會馬上出現在公開地圖上，
要等管理者在 `web/index.html` 的「待審核」分頁核准。

### 額外要做一件事：建 Cloud Storage bucket 存照片

```bash
gcloud storage buckets create gs://${GOOGLE_CLOUD_PROJECT}-mirror-photos \
  --project=$GOOGLE_CLOUD_PROJECT --location=asia-east1 --uniform-bucket-level-access
```

**這個 bucket 刻意不公開**（`objectViewer` 沒有開放給 `allUsers`）。
照片一律經過 `GET /api/photo/{path}` 這個代理端點讀取，API 用自己的
service account 權限去 bucket 拿，前端拿到的是 API 的網址，不是 GCS 的網址。
bucket 名稱預設是 `{PROJECT}-mirror-photos`，要換名稱就設 `PHOTO_BUCKET` 環境變數。

### 端點

| 端點 | 用途 |
|---|---|
| `POST /api/mirror-photos` | 民眾上傳（multipart：`photo` 檔案、`lat`、`lng`、選填 `note`） |
| `GET  /api/photo/{path}`  | 照片代理讀取 |
| `GET  /api/pending`       | 待審核清單（管理者用） |
| `POST /api/pending/{id}/review` | 核准（`{"approve":true}` → `active`）或退回（`false` → `removed`） |

**這兩個審核端點目前沒有身分驗證**——跟這個專案其他 API 一樣掛在
`--allow-unauthenticated` 的 Cloud Run 服務上，任何人都能呼叫。
demo／黑客松範圍內先這樣，正式上線前一定要補管理者登入。

### 本機測試

```bash
cd api
python -m uvicorn main:app --host 127.0.0.1 --port 8080
```

`web/report.html` 跟 `web/index.html` 一樣要複製一份出去換掉 `YOUR_MAPS_API_KEY`
才能開（見上面「本機跑起來看網站」那段），用瀏覽器打開就能測完整流程。

---

## 成本控制

**Street View Static API 計費，Metadata API 免費。**

`inspection.py` 一律先呼叫 Metadata 確認該座標有無街景、拍攝年月，
通過才抓圖。否則會在半夜燒掉額度，抓回一堆空白圖。

Stage 2 只取一張特寫：由最近全景點算出精確指向目標座標的方位角，
`fov=20` 窄角拍攝——不是四方位各拍一張，是先算準方向再拍一張，
解決鏡面在廣角圖裡只有數十像素的解析度瓶頸。

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
