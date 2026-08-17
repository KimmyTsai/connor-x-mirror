"""
pytest 設定：把 pipeline/ 加進 sys.path，並在 import 期間補上假的
GOOGLE_CLOUD_PROJECT / MAPS_API_KEY，避免 detect.py / inspection.py
在模組層級建立 genai.Client() / bigquery.Client() 時因缺環境變數而炸掉。

這裡測的是純函式（destination / haversine / cluster / condition_score），
不會真的打 Vertex AI 或 Maps API，所以假值即可。
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("MAPS_API_KEY", "test-key")

PIPELINE_DIR = Path(__file__).resolve().parent.parent / "pipeline"
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))
