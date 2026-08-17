-- 鏡眼 Mirror Eye ── BigQuery schema
-- 執行：bq query --use_legacy_sql=false < schema.sql

CREATE SCHEMA IF NOT EXISTS mirror_eye
OPTIONS (location = "asia-east1");

-- ---------------------------------------------------------------
-- 路口節點：分析的基本單位
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mirror_eye.intersections (
  intersection_id   STRING  NOT NULL,
  geom              GEOGRAPHY,
  city              STRING,
  district          STRING,
  road_names        ARRAY<STRING>,
  has_signal        BOOL,            -- 是否有號誌
  road_width_m      FLOAT64,
  corner_angle_deg  FLOAT64,         -- 轉角夾角，越接近90度視線越差
  sight_distance_m  FLOAT64,         -- 轉角視距，由 GIS 幾何推算
  near_school       BOOL,
  near_market       BOOL,
  updated_at        TIMESTAMP
);

-- ---------------------------------------------------------------
-- 反射鏡清冊
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mirror_eye.mirrors (
  mirror_id         STRING NOT NULL,
  intersection_id   STRING,
  geom              GEOGRAPHY,
  source            STRING,          -- opendata | streetview_detected | citizen
  installed_year    INT64,
  status            STRING,          -- active | removed | pending（民眾回報新鏡子，待管理者審核才轉 active）
  updated_at        TIMESTAMP
);

-- ---------------------------------------------------------------
-- 街景／照片判讀結果（每次判讀一列，保留歷史）
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mirror_eye.inspections (
  inspection_id     STRING NOT NULL,
  mirror_id         STRING,
  intersection_id   STRING,
  image_uri         STRING,
  image_source      STRING,          -- streetview | citizen | fieldwork
  captured_at       TIMESTAMP,       -- 街景拍攝年月，非判讀時間

  mirror_present    BOOL,            -- 畫面中是否有反射鏡

  -- 五個劣化維度，各 0~3
  dirt_score        INT64,           -- 髒污／霧化
  angle_score       INT64,           -- 角度偏移
  damage_score      INT64,           -- 破損／裂痕
  occlusion_score   INT64,           -- 植栽招牌遮蔽
  corrosion_score   INT64,           -- 鏽蝕／支架傾斜

  condition_score   FLOAT64,         -- 0~100，劣化程度加權
  confidence        FLOAT64,         -- 0~1
  reason            STRING,          -- 人類可讀的判讀理由
  needs_human_review BOOL,

  model_version     STRING,
  created_at        TIMESTAMP
)
PARTITION BY DATE(created_at);

-- ---------------------------------------------------------------
-- 事故資料
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mirror_eye.accidents (
  accident_id       STRING NOT NULL,
  geom              GEOGRAPHY,
  occurred_at       TIMESTAMP,
  severity          STRING,          -- A1 | A2 | A3
  involved_types    ARRAY<STRING>    -- 機車、行人、自行車…
);

-- ---------------------------------------------------------------
-- 民眾回報
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mirror_eye.reports (
  report_id         STRING NOT NULL,
  intersection_id   STRING,
  mirror_id         STRING,
  geom              GEOGRAPHY,
  report_type       STRING,          -- new_request | maintenance
  raw_text          STRING,
  photo_uri         STRING,
  -- Gemini 從自由文字抽出的結構化事實
  hazard_time       STRING,          -- 例：上學時段
  hazard_group      STRING,          -- 例：國小學童
  hazard_desc       STRING,
  created_at        TIMESTAMP
)
PARTITION BY DATE(created_at);


-- ===============================================================
-- 評分視圖
-- ===============================================================

-- 路口風險係數：事故加權 ＋ 幾何 ＋ 敏感設施
CREATE OR REPLACE VIEW mirror_eye.v_intersection_risk AS
WITH acc AS (
  SELECT
    i.intersection_id,
    COUNTIF(a.severity = 'A1') * 10
      + COUNTIF(a.severity = 'A2') * 3
      + COUNTIF(a.severity = 'A3') * 1 AS accident_weight,
    COUNT(a.accident_id) AS accident_count
  FROM mirror_eye.intersections i
  LEFT JOIN mirror_eye.accidents a
    ON ST_DWithin(i.geom, a.geom, 30)
   AND a.occurred_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1095 DAY)
  GROUP BY 1
)
SELECT
  i.intersection_id,
  i.geom,
  i.city,
  i.district,
  i.road_names,
  acc.accident_count,
  -- 分項貢獻：前端要能拆開顯示，這是可解釋性的來源
  LEAST(acc.accident_weight * 2.0, 40)                              AS s_accident,
  CASE WHEN i.sight_distance_m IS NULL THEN 0
       WHEN i.sight_distance_m < 10 THEN 25
       WHEN i.sight_distance_m < 20 THEN 15
       WHEN i.sight_distance_m < 30 THEN 8
       ELSE 0 END                                                   AS s_sight,
  CASE WHEN i.road_width_m IS NULL THEN 0
       WHEN i.road_width_m < 6 THEN 10
       WHEN i.road_width_m < 8 THEN 5
       ELSE 0 END                                                   AS s_width,
  (IF(i.near_school, 15, 0) + IF(i.near_market, 10, 0))             AS s_vulnerable,
  IF(i.has_signal, -20, 0)                                          AS s_signal_offset
FROM mirror_eye.intersections i
LEFT JOIN acc USING (intersection_id);


-- 設置需求：對「沒有鏡子」的路口排序
CREATE OR REPLACE VIEW mirror_eye.v_installation_need AS
WITH r AS (
  SELECT
    *,
    GREATEST(0, LEAST(100,
      s_accident + s_sight + s_width + s_vulnerable + s_signal_offset
    )) AS base_score
  FROM mirror_eye.v_intersection_risk
),
rep AS (
  SELECT intersection_id, COUNT(*) AS report_count
  FROM mirror_eye.reports
  WHERE report_type = 'new_request'
  GROUP BY 1
)
SELECT
  r.intersection_id,
  r.geom,
  r.city,
  r.district,
  r.road_names,
  r.accident_count,
  r.s_accident, r.s_sight, r.s_width, r.s_vulnerable, r.s_signal_offset,
  r.base_score,
  IFNULL(rep.report_count, 0) AS report_count,   -- 併列顯示，不加進分數
  CASE
    WHEN r.base_score >= 70 THEN '急迫'
    WHEN r.base_score >= 50 THEN '高'
    WHEN r.base_score >= 30 THEN '中'
    ELSE '低'
  END AS need_level,
  -- 歧異挖掘：民意高但客觀分數低 → 排進人工複查
  (IFNULL(rep.report_count, 0) >= 3 AND r.base_score < 30) AS flag_review
FROM r
LEFT JOIN rep USING (intersection_id)
WHERE NOT EXISTS (
  SELECT 1 FROM mirror_eye.mirrors m
  WHERE m.intersection_id = r.intersection_id AND m.status = 'active'
);


-- 維護優先序 = 鏡況劣化 × 該點位風險
CREATE OR REPLACE VIEW mirror_eye.v_maintenance_priority AS
WITH latest AS (
  -- intersection_id 排除掉：mirrors.intersection_id 才是權威 FK，
  -- inspections 裡的只是寫入當下的快照，兩者併入同一張表會讓下面的
  -- USING (intersection_id) 對到哪一欄產生歧義
  SELECT * EXCEPT(rn, intersection_id) FROM (
    SELECT i.*, ROW_NUMBER() OVER (
      PARTITION BY mirror_id ORDER BY created_at DESC
    ) AS rn
    FROM mirror_eye.inspections i
    WHERE mirror_id IS NOT NULL
  ) WHERE rn = 1
),
risk AS (
  SELECT
    intersection_id,
    GREATEST(0, LEAST(100,
      s_accident + s_sight + s_width + s_vulnerable + s_signal_offset
    )) AS risk_score
  FROM mirror_eye.v_intersection_risk
)
SELECT
  m.mirror_id,
  m.intersection_id,
  m.geom,
  l.mirror_present,
  l.image_uri,
  l.condition_score,
  l.confidence,
  l.reason,
  l.dirt_score, l.angle_score, l.damage_score,
  l.occlusion_score, l.corrosion_score,
  IFNULL(risk.risk_score, 30) AS risk_score,
  -- 核心公式；mirror_present=false 時 condition_score 固定是 0，
  -- 但那不是「健康」，是 Stage 2 沒能在登記座標重新確認鏡子
  ROUND(l.condition_score * (0.5 + IFNULL(risk.risk_score, 30) / 100.0), 1)
    AS priority_score,
  CASE
    WHEN NOT l.mirror_present THEN '位置待確認'
    WHEN l.condition_score >= 60 THEN '失效'
    WHEN l.condition_score >= 30 THEN '需保養'
    ELSE '健康'
  END AS condition_level,
  l.needs_human_review
FROM mirror_eye.mirrors m
JOIN latest l USING (mirror_id)
LEFT JOIN risk USING (intersection_id)
WHERE m.status = 'active'
ORDER BY priority_score DESC;
