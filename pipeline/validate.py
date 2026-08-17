"""
鏡眼 Mirror Eye ── 偵測準確率驗證

用途：對人工標註的路口清單跑偵測，算出 precision / recall。
簡報上「precision 0.9x / recall 0.8x，在 N 個路口的人工標註集上」這一行，
比任何架構圖都有說服力。

真值檔格式（groundtruth.csv）：
    id,lat,lng,has_mirror
    INT_001,22.99971,120.22703,1
    INT_002,22.99885,120.22610,0

用法：
    python pipeline/validate.py groundtruth.csv
    python pipeline/validate.py groundtruth.csv --dump errors.json
"""

import sys
import csv
import json

from detect import scan_intersection, cluster


def evaluate(path: str, min_support: int = 1) -> dict:
    tp = fp = tn = fn = 0
    errors = {"false_positive": [], "false_negative": []}

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for i, row in enumerate(rows, 1):
        truth = row["has_mirror"].strip() in ("1", "true", "True", "yes")
        obs = scan_intersection(row["id"], float(row["lat"]), float(row["lng"]))
        cands = [c for c in cluster(obs) if c.support >= min_support]
        pred = len(cands) > 0

        if pred and truth:
            tp += 1
        elif pred and not truth:
            fp += 1
            errors["false_positive"].append({
                "id": row["id"],
                "evidence": cands[0].evidence,
                "confidence": cands[0].confidence,
                "support": cands[0].support,
            })
        elif not pred and truth:
            fn += 1
            errors["false_negative"].append({"id": row["id"]})
        else:
            tn += 1

        print(f"[{i}/{len(rows)}] {row['id']}  真值={int(truth)} 預測={int(pred)}")

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return {
        "n": len(rows),
        "min_support": min_support,
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "errors": errors,
    }


if __name__ == "__main__":
    path = sys.argv[1]
    result = evaluate(path)

    print("\n" + "=" * 46)
    print(f"樣本數      {result['n']} 個路口")
    print(f"TP {result['tp']}   FP {result['fp']}   "
          f"TN {result['tn']}   FN {result['fn']}")
    print(f"Precision   {result['precision']}")
    print(f"Recall      {result['recall']}")
    print(f"F1          {result['f1']}")
    print("=" * 46)
    print("\n把失敗案例各挑一張放進簡報 —— 主動秀失敗會大幅提升評審信任度。")

    if "--dump" in sys.argv:
        out = sys.argv[sys.argv.index("--dump") + 1]
        with open(out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n錯誤案例已寫入 {out}")
