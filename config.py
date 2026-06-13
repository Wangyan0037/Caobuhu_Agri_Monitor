"""Global configuration for the Caobuhu monitoring system."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ID = "caramel-sum-485005-g5"
ROI_ASSET = "projects/caramel-sum-485005-g5/assets/CBH3"
DRIVE_FOLDER = "Caobuhu_Monitor_TIF"
APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = str(APP_ROOT)
LOCAL_SAFE_DIR = str(APP_ROOT / "data" / "S2" / "DANGYANG" / "raw_safe")
LOCAL_ZIP_DIR = str(APP_ROOT / "data" / "S2" / "DANGYANG" / "raw_zip")
LOCAL_OUTPUT_DIR = str(APP_ROOT / "data" / "S2" / "DANGYANG" / "processed")
LOCAL_AOI_DIR = str(APP_ROOT / "data" / "ROI")

DEFAULT_WINDOW_DAYS = 15
DEFAULT_CLOUD_THRESHOLD = 20
DEFAULT_SCALE = 10
DEFAULT_TILE_SCALE = 4
NODATA_VALUE = 255
FLOAT_NODATA_VALUE = -9999

VITALITY_GRADE = {0: "No data", 1: "Poor", 2: "Weak", 3: "Normal", 4: "Good"}
MOISTURE_GRADE = {0: "No data", 1: "Dry", 2: "Suitable", 3: "Wet", 4: "Possible waterlogging"}
ANOMALY_TYPE = {
    0: "No anomaly",
    1: "Weak vitality",
    2: "Vitality decline",
    3: "Possible drought",
    4: "Possible waterlogging",
    5: "Possible fallow",
    6: "Possible non-agricultural conversion",
}
PATROL_PRIORITY = {0: "No patrol", 1: "Low", 2: "Medium", 3: "High"}


@dataclass(frozen=True)
class Thresholds:
    vitality_poor_ndvi: float = 0.20
    vitality_weak_ndvi: float = 0.35
    vitality_normal_ndvi: float = 0.55
    vitality_decline_delta: float = 0.10
    dry_vv_db: float = -16.0
    wet_vv_db: float = -12.0
    water_vv_db: float = -8.0
    dry_ndwi: float = -0.15
    wet_ndwi: float = 0.10
    water_ndwi: float = 0.30
    water_lswi: float = 0.40
    fallow_ndvi_max: float = 0.35
    fallow_ndvi_amp: float = 0.20
    nonagri_ndbi_rise: float = 0.05
    nonagri_ndvi_drop: float = -0.15


THRESHOLDS = Thresholds()
