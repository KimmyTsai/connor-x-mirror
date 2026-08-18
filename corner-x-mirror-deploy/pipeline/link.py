"""
Corner.X 城市反射鏡數據網 ── 把 mirrors 表的鏡子逐一送進鏡況判讀

流程：
  查詢 mirrors（status = active）→ 逐支呼叫 inspect_point() → 寫入 inspections

inspections 保留歷史（見 schema.sql 的 PARTITION BY DATE(created_at)），
所以預設會重跑全部 active 鏡子，藉此累積劣化趨勢；
只想幫剛偵測到、還沒判讀過的鏡子補資料時用 --only-new。

用法：
  python pipeline/link.py                  # 全部 active 鏡子
  python pipeline/link.py --limit 5        # 先小後大，先跑 5 支確認正確再放大
  python pipeline/link.py --only-new       # 只判讀還沒有 inspections 紀錄的鏡子
"""

import os
import argparse

from google.cloud import bigquery

from inspection import inspect_point

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]

bq = bigquery.Client(project=PROJECT)


def fetch_mirrors(limit: int | None, only_new: bool) -> list[dict]:
    only_new_clause = f"""
      AND NOT EXISTS (
        SELECT 1 FROM `{PROJECT}.mirror_eye.inspections` i
        WHERE i.mirror_id = m.mirror_id
      )
    """ if only_new else ""

    sql = f"""
        SELECT m.mirror_id, m.intersection_id,
               ST_Y(m.geom) AS lat, ST_X(m.geom) AS lng
        FROM `{PROJECT}.mirror_eye.mirrors` m
        WHERE m.status = 'active'
        {only_new_clause}
        ORDER BY m.updated_at DESC
        {"LIMIT @limit" if limit else ""}
    """
    params = ([bigquery.ScalarQueryParameter("limit", "INT64", limit)]
              if limit else [])
    cfg = bigquery.QueryJobConfig(query_parameters=params)
    return [dict(r) for r in bq.query(sql, job_config=cfg).result()]


def run(limit: int | None = None, only_new: bool = False) -> None:
    mirrors = fetch_mirrors(limit, only_new)
    print(f"待判讀鏡子：{len(mirrors)} 支")

    inspected = skipped = 0
    for i, m in enumerate(mirrors, 1):
        results = inspect_point(
            m["lat"], m["lng"],
            mirror_id=m["mirror_id"],
            intersection_id=m["intersection_id"],
        )
        if not results:
            skipped += 1
            print(f"[{i}/{len(mirrors)}] {m['mirror_id']}  無街景，略過")
            continue

        inspected += 1
        r = results[0]
        if r.mirror_present:
            print(f"[{i}/{len(mirrors)}] {m['mirror_id']}  "
                  f"劣化 {r.condition_score} (信心 {r.confidence:.2f})")
        else:
            # Stage 2 特寫都判不出鏡子：可能是定位偏移、街景過舊，或鏡子已被拆除
            print(f"[{i}/{len(mirrors)}] {m['mirror_id']}  "
                  f"未見鏡子 ⚠ 建議人工複查：{r.reason}")

    print(f"\n完成：{inspected} 支已判讀，{skipped} 支無街景略過")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="只處理前 N 支（先小後大時使用）")
    parser.add_argument("--only-new", action="store_true",
                        help="只判讀還沒有 inspections 紀錄的鏡子")
    args = parser.parse_args()
    run(limit=args.limit, only_new=args.only_new)
