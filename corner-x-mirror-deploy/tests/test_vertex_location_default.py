"""
迴歸測試：GOOGLE_CLOUD_LOCATION 預設值不能是 asia-east1。

實測發現 gemini-2.5-flash 在 asia-east1 對這個專案回 404
（Publisher model ... was not found），us-central1 可用。

用原始碼文字比對而不是實際 import 執行，因為 Python 模組匯入後
會被 sys.modules 快取，import 順序會讓「重新匯入觀察行為」的測試
變得不可靠；直接檢查程式碼裡寫死的預設值字串反而穩定。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FILES_WITH_LOCATION_DEFAULT = [
    ROOT / "pipeline" / "detect.py",
    ROOT / "pipeline" / "inspection.py",
    ROOT / "api" / "main.py",
]


def test_no_file_defaults_to_asia_east1_for_gemini_location():
    needle = 'os.environ.get("GOOGLE_CLOUD_LOCATION", "asia-east1")'
    for f in FILES_WITH_LOCATION_DEFAULT:
        assert needle not in f.read_text(encoding="utf-8"), (
            f"{f} 又把 GOOGLE_CLOUD_LOCATION 預設值改回 asia-east1 了——"
            f"這個 region 對這個專案沒有 gemini-2.5-flash（實測 404）"
        )


def test_all_files_default_to_us_central1():
    needle = 'os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")'
    for f in FILES_WITH_LOCATION_DEFAULT:
        assert needle in f.read_text(encoding="utf-8"), f"{f} 沒有預期的預設值"
