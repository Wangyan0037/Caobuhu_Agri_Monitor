from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import Tk, filedialog
from urllib.parse import quote

import numpy as np
import rasterio
from flask import Flask, jsonify, request, send_file
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import transform_bounds, transform_geom

from config import LOCAL_AOI_DIR, LOCAL_OUTPUT_DIR, LOCAL_SAFE_DIR, LOCAL_ZIP_DIR
from gee_modules.local_pipeline import available_acquisitions, run_local_processing


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "web" / "static"
INDEX_HTML = APP_DIR / "web" / "index.html"
AOI_DIR = Path(LOCAL_AOI_DIR)
SENTINEL_DOWNLOAD_SCRIPT = APP_DIR / "download" / "S2_CDSE_download.py"
S1_ASF_DOWNLOAD_SCRIPT = APP_DIR / "download" / "S1_ASF_auto_download.py"

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
DERIVED_RENDER_VERSION = "v8"
NATIVE_RGB_LAYERS = {"truecolor_rgb", "falsecolor_rgb", "truecolor_preview_rgb", "falsecolor_preview_rgb"}
RESULT_PNG_ALPHA = 255
IMPORTED_RESULTS_DIR = APP_DIR / "data" / "imported_results"
SEARCH_JSON_MARKER = "__SEARCH_RESULTS_JSON__"
DOWNLOAD_PROGRESS_MARKER = "__DOWNLOAD_PROGRESS_JSON__"

JOBS: dict[str, dict] = {
    "download": {"status": "idle", "message": "等待检索", "items": [], "percent": 0, "progress_text": "未开始"},
    "process": {
        "status": "idle",
        "message": "等待处理",
        "metadata": None,
        "stop_requested": False,
        "percent": 0,
        "item_progress": {},
    },
}
DOWNLOAD_RUNTIME: dict[str, object] = {"process": None, "stop_requested": False}

LAYER_META = {
    "vitality_grade": {
        "name": "长势等级",
        "legend": [
            {"kind": "note", "name": "长势诊断图例（NDVI）", "note": "NDVI越高，通常表示植被覆盖度和作物活力越高。"},
            {"value": 1, "name": "NDVI < 0.20 长势差", "color": "#d73027", "note": "可能存在缺苗、裸土、重度胁迫、病虫害或严重干旱。"},
            {"value": 2, "name": "0.20-0.35 长势偏弱", "color": "#fdae61", "note": "重点关注。可能为干旱、病害、早衰或管理不均。"},
            {"value": 3, "name": "0.35-0.55 长势正常", "color": "#ffffbf", "note": "生长基本平稳，建议结合物候期判断。"},
            {"value": 4, "name": "≥0.55 长势良好", "color": "#1a9850", "note": "植被活力较高，冠层覆盖较好。"},
        ],
        "type": "class",
        "colors": {1: (215, 48, 39, 190), 2: (253, 174, 97, 190), 3: (255, 255, 191, 185), 4: (26, 152, 80, 190)},
    },
    "moisture_grade": {
        "name": "墒情等级",
        "legend": [
            {"value": 1, "name": "NDWI < -0.15 干旱", "color": "#a6611a", "note": "土壤/冠层水分不足。"},
            {"value": 2, "name": "-0.15-0.10 适宜", "color": "#80cdc1", "note": "水分条件较适宜。"},
            {"value": 3, "name": "0.10-0.30 偏湿", "color": "#35978f", "note": "水分偏高，需要关注排水。"},
            {"value": 4, "name": ">0.30 疑似积水", "color": "#2166ac", "note": "NDWI/LSWI较高，可能有积水或明水。"},
        ],
        "type": "class",
        "colors": {1: (166, 97, 26, 190), 2: (128, 205, 193, 180), 3: (53, 151, 143, 185), 4: (33, 102, 172, 195)},
    },
    "anomaly_combined": {
        "name": "综合异常",
        "legend": [
            {"value": 1, "name": "弱长势", "color": "#ffd92f", "note": "长势明显偏弱，建议优先核查。"},
            {"value": 3, "name": "低水分 + 弱长势", "color": "#a6611a", "note": "疑似干旱。"},
            {"value": 4, "name": "NDWI/LSWI 高值", "color": "#2b83ba", "note": "疑似积水。"},
            {"value": 5, "name": "低 NDVI + 裸土特征", "color": "#bdbdbd", "note": "疑似撂荒。"},
            {"value": 6, "name": "NDVI下降 + NDBI增强", "color": "#d7191c", "note": "疑似非农化。"},
        ],
        "type": "class",
        "colors": {1: (255, 217, 47, 190), 3: (166, 97, 26, 200), 4: (43, 131, 186, 200), 5: (189, 189, 189, 180), 6: (215, 25, 28, 205)},
    },
    "patrol_priority": {
        "name": "巡田优先级",
        "legend": [
            {"kind": "note", "name": "巡田指导图例（LAI）", "note": "叶面积指数越高，长势越好。"},
            {"value": 1, "name": "≤1.0 严重异常", "color": "#ff0000", "note": "立即巡查。"},
            {"value": 2, "name": "1.0-1.8 长势差", "color": "#ff9900", "note": "重点关注。"},
            {"value": 3, "name": "1.8-2.5 长势一般", "color": "#ffff00", "note": "结合近期影像继续观察。"},
            {"value": 4, "name": "2.5-3.2 长势良好", "color": "#66dd66", "note": "常规巡查。"},
            {"value": 5, "name": "3.2-4.0 长势旺盛", "color": "#1a9850", "note": "长势较好。"},
            {"value": 6, "name": ">4.0 极佳", "color": "#006837", "note": "叶片茂密。"},
        ],
        "type": "class",
        "colors": {1: (255, 0, 0, 190), 2: (255, 153, 0, 190), 3: (255, 255, 0, 185), 4: (102, 221, 102, 190), 5: (26, 152, 80, 190), 6: (0, 104, 55, 190)},
    },
    "vitality_index_ndvi": {
        "name": "NDVI 连续值",
        "legend": [
            {"value": 1, "name": "NDVI 0.00-0.30 低", "color": "#d73027", "note": "植被覆盖或活力低。"},
            {"value": 2, "name": "NDVI 0.30-0.55 中", "color": "#fdae61", "note": "中等植被覆盖。"},
            {"value": 3, "name": "NDVI 0.55-0.80 高", "color": "#1a9850", "note": "植被覆盖和长势较好。"},
        ],
        "type": "threshold",
        "breaks": [0.30, 0.55],
        "colors": {1: (215, 48, 39, 185), 2: (253, 174, 97, 185), 3: (26, 152, 80, 185)},
    },
    "ndre_index": {
        "name": "NDRE",
        "legend": [
            {"value": 1, "name": "NDRE 低", "color": "#d73027", "note": "红边活力偏低。"},
            {"value": 2, "name": "NDRE 中", "color": "#fdae61", "note": "红边活力中等。"},
            {"value": 3, "name": "NDRE 高", "color": "#1a9850", "note": "红边活力较高。"},
        ],
        "type": "threshold",
        "breaks": [0.12, 0.22],
        "colors": {1: (215, 48, 39, 185), 2: (253, 174, 97, 185), 3: (26, 152, 80, 185)},
    },
    "ndvi_last_year_delta": {
        "name": "去年同期 NDVI 差值",
        "legend": [
            {"value": 1, "name": "ΔNDVI < -0.10 下降", "color": "#ffd92f", "note": "长势退化，可能与干旱、病害或早衰有关。"},
            {"value": 2, "name": "ΔNDVI -0.10-0.10 稳定", "color": "#ffffff", "note": "生长平稳或成熟期。"},
            {"value": 3, "name": "ΔNDVI > 0.10 上升", "color": "#1a9641", "note": "长势旺盛，可能处于分蘖、拔节或恢复期。"},
        ],
        "type": "gradient",
        "min": -0.3,
        "max": 0.3,
    },
    "crop_type_class": {
        "name": "作物类型分类",
        "legend": [
            {"kind": "note", "name": "无样本规则分类", "note": "基于 Sentinel-2 指数规则推理，适合演示和初步判读，不等同于实地样本监督分类。"},
            {"value": 1, "name": "水体 / 鱼塘 / 沟渠", "color": "#2b83ba", "note": "高水分、低植被覆盖区域。"},
            {"value": 2, "name": "建筑 / 道路 / 裸地 / 非植被", "color": "#bdbdbd", "note": "低 NDVI 且裸土或建设特征较强。"},
            {"value": 3, "name": "疑似水稻 / 早夏稻田", "color": "#1a9850", "note": "高 NDVI、较高 LSWI 与红边活力特征。"},
            {"value": 4, "name": "春季油菜 / 小麦等冬作物 / 轮作田", "color": "#fee08b", "note": "中等偏高植被覆盖，水分低于稻田。"},
            {"value": 5, "name": "其他作物 / 其他植被", "color": "#66bd63", "note": "其余植被覆盖区域。"},
        ],
        "type": "class",
        "colors": {1: (43, 131, 186, 220), 2: (189, 189, 189, 210), 3: (26, 152, 80, 220), 4: (254, 224, 139, 220), 5: (102, 189, 99, 215)},
    },
    "crop_yield_estimate": {
        "name": "作物估产",
        "legend": [
            {"kind": "note", "name": "演示型估产", "note": "这是基于光学指数融合的空间差异演示值，单位 t/ha，不是实测产量。"},
            {"value": 1, "name": "低产区 3-4.5 t/ha", "color": "#d73027", "note": "相对产量潜力较低。"},
            {"value": 2, "name": "中产区 4.5-5.8 t/ha", "color": "#fee08b", "note": "中等产量潜力。"},
            {"value": 3, "name": "高产区 5.8-7.0 t/ha", "color": "#1a9850", "note": "相对产量潜力较高。"},
        ],
        "type": "gradient",
        "min": 3.0,
        "max": 7.0,
        "gradient_low": (215, 48, 39),
        "gradient_high": (26, 152, 80),
    },
    "anomaly_drought": {"name": "疑似干旱", "legend": [{"value": 1, "name": "NDWI ≤ -0.15", "color": "#a6611a", "note": "低水分指数，可能导致长势下降。"}], "type": "class", "colors": {1: (166, 97, 26, 215)}},
    "anomaly_waterlogging": {"name": "疑似积水", "legend": [{"value": 1, "name": "NDWI ≥ 0.30 或 LSWI ≥ 0.40", "color": "#2166ac", "note": "水分指数异常偏高，需关注排涝。"}], "type": "class", "colors": {1: (33, 102, 172, 215)}},
    "anomaly_fallow": {"name": "疑似撂荒", "legend": [{"value": 1, "name": "NDVI ≤ 0.35 且 NDBI ≥ 0.05", "color": "#bdbdbd", "note": "长期低植被活力或裸土特征明显。"}], "type": "class", "colors": {1: (189, 189, 189, 210)}},
    "anomaly_nonagri": {"name": "疑似非农化", "legend": [{"value": 1, "name": "NDVI ≤ 0.30 且 NDBI增强", "color": "#d7191c", "note": "植被下降并出现建设/裸土增强信号。"}], "type": "class", "colors": {1: (215, 25, 28, 220)}},
}

LAYER_GROUPS = {
    "truecolor_preview_rgb": {
        "name": "影像预览",
        "children": [
            {"key": "truecolor_preview_rgb", "name": "真彩色影像（RGB 自然色）"},
            {"key": "falsecolor_preview_rgb", "name": "假彩色影像（植被增强 NIR-R-G）"},
        ],
    },
    "crop_type_class": {
        "name": "作物类型分类",
        "children": [
            {"key": "crop_type_class", "name": "作物类型分类"},
            {"key": "vitality_index_ndvi_rgb", "name": "NDVI"},
            {"key": "ndre_index_rgb", "name": "NDRE"},
        ],
    },
    "crop_yield_estimate": {
        "name": "作物估产",
        "children": [
            {"key": "crop_yield_estimate", "name": "产量演示数值_t_ha"},
            {"key": "vitality_index_ndvi_gray", "name": "NDVI灰度拉伸预览"},
            {"key": "crop_yield_estimate_gray", "name": "产量灰度拉伸预览"},
            {"key": "vitality_index_ndvi_rgb", "name": "NDVI"},
        ],
    },
}

SINGLE_LAYER_ORDER = [
    "vitality_grade",
    "moisture_grade",
    "anomaly_combined",
    "patrol_priority",
    "vitality_index_ndvi",
    "ndre_index",
    "ndvi_last_year_delta",
    "anomaly_weak_vitality",
    "anomaly_vitality_decline",
    "anomaly_drought",
    "anomaly_waterlogging",
    "anomaly_fallow",
    "anomaly_nonagri",
]

DERIVED_LAYER_META = {
    "truecolor_preview_rgb": {
        "legend": [{"kind": "note", "name": "真彩色影像（RGB 自然色）", "note": "Sentinel-2 红、绿、蓝波段组合；影像预览保留云、云影等真实观测像元。"}],
    },
    "falsecolor_preview_rgb": {
        "legend": [{"kind": "note", "name": "假彩色影像（植被增强 NIR-R-G）", "note": "Sentinel-2 近红外、红、绿波段组合；影像预览保留云、云影等真实观测像元。"}],
    },
    "truecolor_rgb": {
        "legend": [{"kind": "note", "name": "真彩色影像（RGB 自然色）", "note": "Sentinel-2 红、绿、蓝波段组合，接近人眼自然色观察效果。"}],
    },
    "falsecolor_rgb": {
        "legend": [{"kind": "note", "name": "假彩色影像（植被增强 NIR-R-G）", "note": "Sentinel-2 近红外、红、绿波段组合，植被通常更醒目，适合辅助判读作物覆盖。"}],
    },
    "vitality_index_ndvi_rgb": {
        "legend": [
            {"value": 1, "name": "低值", "color": "#d73027", "note": "植被覆盖低或长势弱。"},
            {"value": 2, "name": "中值", "color": "#fdae61", "note": "中等植被覆盖。"},
            {"value": 3, "name": "高值", "color": "#1a9850", "note": "植被覆盖和长势较好。"},
        ],
    },
    "ndre_index_rgb": {
        "legend": [
            {"value": 1, "name": "低红边活力", "color": "#d73027", "note": "作物活力或叶绿素水平偏低。"},
            {"value": 2, "name": "中等红边活力", "color": "#fdae61", "note": "作物活力中等。"},
            {"value": 3, "name": "高红边活力", "color": "#1a9850", "note": "冠层活力较强。"},
        ],
    },
    "crop_yield_estimate_rgb": {
        "legend": [
            {"kind": "note", "name": "产量空间差异", "note": "基于遥感指数的演示型产量空间差异，适合展示相对高低。"},
            {"value": 1, "name": "低值区", "color": "#d73027", "note": "相对产量潜力较低。"},
            {"value": 2, "name": "中值区", "color": "#fee08b", "note": "相对产量潜力中等。"},
            {"value": 3, "name": "高值区", "color": "#1a9850", "note": "相对产量潜力较高。"},
        ],
    },
    "vitality_index_ndvi_gray": {
        "type": "gray",
        "legend": [{"kind": "note", "name": "NDVI灰度拉伸预览", "note": "用于快速查看 NDVI 空间纹理，灰度值经过显示拉伸，不作为原始数值分析。"}],
    },
    "crop_yield_estimate_gray": {
        "type": "gray",
        "legend": [{"kind": "note", "name": "产量灰度拉伸预览", "note": "用于快速查看估产空间纹理，灰度值经过显示拉伸，不作为原始产量值分析。"}],
    },
}

EXPORT_LAYER_NAMES = {
    "truecolor_preview_rgb": "真彩色影像_RGB自然色_未云掩膜",
    "falsecolor_preview_rgb": "假彩色影像_植被增强_NIR-R-G_未云掩膜",
    "truecolor_rgb": "影像RGB",
    "falsecolor_rgb": "假彩色影像_植被增强_NIR-R-G",
    "vitality_index_ndvi_rgb": "NDVI",
    "ndre_index_rgb": "NDRE",
    "crop_yield_estimate_rgb": "产量空间差异彩色展示_RGB",
    "vitality_index_ndvi_gray": "NDVI灰度拉伸预览",
    "crop_yield_estimate_gray": "产量灰度拉伸预览",
}


@app.get("/")
def index():
    return INDEX_HTML.read_text(encoding="utf-8")


@app.get("/api/defaults")
def defaults():
    return jsonify(
        {
            "safe_dir": LOCAL_SAFE_DIR,
            "zip_dir": LOCAL_ZIP_DIR,
            "output_dir": LOCAL_OUTPUT_DIR,
            "start_date": (date.today() - timedelta(days=30)).isoformat(),
            "end_date": date.today().isoformat(),
            "product_type": "SENTINEL-2-L2A",
        }
    )


@app.post("/api/choose-folder")
def choose_folder():
    current = (request.get_json(silent=True) or {}).get("current") or str(APP_DIR)
    selected = {"path": ""}

    def open_dialog():
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected["path"] = filedialog.askdirectory(initialdir=current if Path(current).exists() else str(APP_DIR))
        root.destroy()

    thread = threading.Thread(target=open_dialog)
    thread.start()
    thread.join()
    return jsonify({"ok": True, "path": selected["path"]})


@app.post("/api/choose-vector-file")
def choose_vector_file():
    current = (request.get_json(silent=True) or {}).get("current") or str(AOI_DIR)
    current_path = Path(current)
    initial_dir = current_path.parent if current_path.is_file() else current_path
    if not initial_dir.exists():
        initial_dir = AOI_DIR if AOI_DIR.exists() else APP_DIR
    selected = {"path": ""}

    def open_dialog():
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected["path"] = filedialog.askopenfilename(
            initialdir=str(initial_dir),
            title="选择处理范围矢量文件",
            filetypes=[
                ("矢量文件", "*.geojson *.json *.shp"),
                ("GeoJSON", "*.geojson *.json"),
                ("Shapefile", "*.shp"),
                ("所有文件", "*.*"),
            ],
        )
        root.destroy()

    thread = threading.Thread(target=open_dialog)
    thread.start()
    thread.join()
    return jsonify({"ok": True, "path": selected["path"]})


@app.post("/api/choose-result-path")
def choose_result_path():
    current = (request.get_json(silent=True) or {}).get("current") or str(LOCAL_OUTPUT_DIR)
    current_path = Path(current)
    initial_dir = current_path.parent if current_path.is_file() else current_path
    if not initial_dir.exists():
        initial_dir = APP_DIR
    selected = {"path": ""}

    def open_dialog():
        root = Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        filename = filedialog.askopenfilename(
            initialdir=str(initial_dir),
            title="选择已处理结果 ZIP/TIF，取消后可选择目录",
            filetypes=[
                ("处理结果", "*.zip *.tif *.tiff"),
                ("ZIP 压缩包", "*.zip"),
                ("GeoTIFF", "*.tif *.tiff"),
                ("所有文件", "*.*"),
            ],
        )
        selected["path"] = filename or filedialog.askdirectory(initialdir=str(initial_dir), title="选择已处理结果目录")
        root.destroy()

    thread = threading.Thread(target=open_dialog)
    thread.start()
    thread.join()
    return jsonify({"ok": True, "path": selected["path"]})


@app.get("/api/vector-bounds")
def vector_bounds():
    path = request.args.get("path")
    if not path:
        return jsonify({"ok": False, "error": "缺少矢量文件路径"}), 400
    try:
        return jsonify({"ok": True, "bounds": _vector_bounds_wgs84(Path(path)), "geojson": _vector_preview_geojson(Path(path))})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.get("/api/acquisitions")
def acquisitions():
    try:
        return jsonify({"ok": True, "items": available_acquisitions(request.args.get("safe_dir") or LOCAL_SAFE_DIR)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "items": []}), 400


@app.post("/api/aoi")
def save_aoi():
    AOI_DIR.mkdir(parents=True, exist_ok=True)
    uploads = request.files.getlist("files") or request.files.getlist("file")
    if uploads:
        saved_paths = []
        for uploaded in uploads:
            if not uploaded or not uploaded.filename:
                continue
            target = AOI_DIR / Path(uploaded.filename).name
            uploaded.save(target)
            saved_paths.append(target)
        shp_paths = [path for path in saved_paths if path.suffix.lower() == ".shp"]
        target = shp_paths[0] if shp_paths else saved_paths[0]
        _validate_vector_upload(target)
        return jsonify({"ok": True, "path": str(target), "bounds": _vector_bounds_wgs84(target), "geojson": _vector_preview_geojson(target)})

    payload = request.get_json(force=True)
    geometry = payload.get("geometry")
    if not geometry:
        return jsonify({"ok": False, "error": "缺少绘制区域"}), 400
    target = AOI_DIR / "gui_selected_aoi.geojson"
    feature = {"type": "Feature", "properties": {"source": "gui"}, "geometry": geometry}
    target.write_text(json.dumps({"type": "FeatureCollection", "features": [feature]}, ensure_ascii=False), encoding="utf-8")
    return jsonify({"ok": True, "path": str(target), "bounds": _vector_bounds_wgs84(target), "geojson": _vector_preview_geojson(target)})


@app.post("/api/search")
def search_products():
    params = request.get_json(force=True)
    _start_download_job(params, search_only=True)
    return jsonify({"ok": True, "message": "检索任务已启动"})


@app.post("/api/download")
def download_products():
    params = request.get_json(force=True)
    _start_download_job(params, search_only=False)
    return jsonify({"ok": True, "message": "下载任务已启动"})


@app.get("/api/download/status")
def download_status():
    return jsonify(JOBS["download"])


@app.post("/api/download/stop")
def stop_download():
    if JOBS["download"]["status"] != "running":
        return jsonify({"ok": False, "message": "当前没有正在运行的下载或检索任务"})
    DOWNLOAD_RUNTIME["stop_requested"] = True
    process = DOWNLOAD_RUNTIME.get("process")
    if process is not None and getattr(process, "poll")() is None:
        process.terminate()
    JOBS["download"].update({"status": "stopping", "message": "正在停止数据下载/检索任务", "progress_text": "正在停止"})
    return jsonify({"ok": True, "message": "已请求停止下载/检索"})


@app.post("/api/process")
def process_data():
    params = request.get_json(force=True)
    if JOBS["process"]["status"] == "running":
        return jsonify({"ok": False, "error": "已有处理任务正在运行"}), 409

    selected_outputs = params.get("selected_outputs") or []
    item_progress = {name: {"percent": 0, "status": "等待"} for name in selected_outputs}
    requested_output_dir = Path(params.get("output_dir") or LOCAL_OUTPUT_DIR)
    run_output_dir = _new_process_output_dir(requested_output_dir)

    def on_progress(progress: dict):
        percent = int(progress.get("percent", 0))
        outputs = progress.get("outputs") or selected_outputs
        for name in outputs:
            item_progress[name] = {"percent": percent, "status": "处理中"}
        JOBS["process"].update(
            {
                "percent": percent,
                "item_progress": item_progress,
                "message": f"正在处理 {progress.get('acquisition', '')} {progress.get('tile_id', '')}，{percent}%",
            }
        )

    def worker():
        JOBS["process"] = {
            "status": "running",
            "message": "正在读取本地光学 SAFE 并生成监测结果",
            "metadata": None,
            "stop_requested": False,
            "percent": 0,
            "item_progress": item_progress,
        }
        try:
            metadata = run_local_processing(
                raw_safe_dir=params.get("optical_safe_dir") or params.get("safe_dir") or LOCAL_SAFE_DIR,
                radar_safe_dir=params.get("radar_safe_dir") or None,
                output_dir=str(run_output_dir),
                selected_outputs=selected_outputs or None,
                processing_params=params.get("processing_params") or {},
                aoi_path=params.get("aoi_path") if params.get("process_scope") == "aoi" else None,
                process_scope_path=params.get("process_scope_path") or params.get("process_scope_dir") or None,
                process_start_date=params.get("process_start_date") or None,
                process_end_date=params.get("process_end_date") or None,
                should_stop=lambda: bool(JOBS["process"].get("stop_requested")),
                progress_callback=on_progress,
            )
            for name in selected_outputs:
                item_progress[name] = {"percent": 100, "status": "完成"}
            _clear_render_cache(Path(metadata["output_dir"]))
            JOBS["process"] = {"status": "done", "message": "处理完成", "metadata": metadata, "stop_requested": False, "percent": 100, "item_progress": item_progress}
        except InterruptedError:
            JOBS["process"] = {"status": "stopped", "message": "处理已停止", "metadata": None, "stop_requested": False, "percent": JOBS["process"].get("percent", 0), "item_progress": item_progress}
        except Exception as exc:
            JOBS["process"] = {"status": "error", "message": str(exc), "metadata": None, "stop_requested": False, "percent": JOBS["process"].get("percent", 0), "item_progress": item_progress}

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True, "message": "处理任务已启动"})


@app.get("/api/process/status")
def process_status():
    return jsonify(JOBS["process"])


@app.post("/api/process/stop")
def stop_process():
    if JOBS["process"]["status"] == "running":
        JOBS["process"]["stop_requested"] = True
        JOBS["process"]["message"] = "正在停止处理，请等待当前分块结束"
        return jsonify({"ok": True, "message": "已请求停止处理"})
    return jsonify({"ok": False, "message": "当前没有正在运行的处理任务"})


def _new_process_output_dir(requested_output_dir: Path) -> Path:
    default_output = Path(LOCAL_OUTPUT_DIR).resolve()
    try:
        requested_resolved = requested_output_dir.resolve()
    except OSError:
        requested_resolved = requested_output_dir
    if requested_resolved != default_output:
        return requested_output_dir
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return default_output.parent / "processed_runs" / f"run_{stamp}"


@app.get("/api/layers")
def layers():
    output_dir = Path(request.args.get("output_dir") or LOCAL_OUTPUT_DIR)
    import_path = request.args.get("import_path")
    if import_path:
        try:
            return jsonify({"ok": True, "items": _imported_tif_layers(import_path)})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
    metadata = _load_processing_metadata(output_dir)
    selected_outputs = set(metadata.get("selected_outputs") or [])
    items = []
    for key, group in LAYER_GROUPS.items():
        if selected_outputs and key not in selected_outputs and key not in {"truecolor_preview_rgb"}:
            continue
        source_tif = _group_bounds_source_tif(output_dir, key, group)
        if source_tif is None:
            continue
        bounds = _tif_bounds_wgs84(source_tif)
        children = []
        for child in group["children"]:
            child_key = child["key"]
            if not _has_layer_asset(output_dir, child_key):
                continue
            children.append(
                {
                    "key": child_key,
                    "name": child["name"],
                    "png": f"/api/render/{child_key}.png?output_dir={output_dir}",
                    "tif": f"/api/tif/{child_key}?output_dir={output_dir}",
                    "legend": _layer_legend(child_key),
                }
            )
        items.append(
            {
                "key": key,
                "name": group["name"],
                "bounds": bounds,
                "png": f"/api/render/{key}.png?output_dir={output_dir}",
                "tif": f"/api/tif/{key}?output_dir={output_dir}",
                "legend": _layer_legend(key),
                "children": children,
            }
        )
    grouped_keys = set(LAYER_GROUPS)
    for group in LAYER_GROUPS.values():
        grouped_keys.update(child["key"] for child in group.get("children", []))
    for key in SINGLE_LAYER_ORDER:
        if key in grouped_keys or not _has_layer_asset(output_dir, key):
            continue
        if selected_outputs and key not in selected_outputs:
            continue
        bounds = _tif_bounds_wgs84(_result_tif_path(output_dir, key))
        items.append(
            {
                "key": key,
                "name": LAYER_META.get(key, {}).get("name", key),
                "bounds": bounds,
                "png": f"/api/render/{key}.png?output_dir={output_dir}",
                "tif": f"/api/tif/{key}?output_dir={output_dir}",
                "legend": _layer_legend(key),
                "children": [],
            }
        )
    return jsonify({"ok": True, "items": items})


@app.get("/api/render/<layer>.png")
def render_layer(layer: str):
    output_dir = Path(request.args.get("output_dir") or LOCAL_OUTPUT_DIR)
    import_path = request.args.get("import_path")
    if import_path:
        tif = _imported_tif_by_key(import_path, layer)
        if tif is None:
            return jsonify({"ok": False, "error": f"导入 TIF 不存在：{layer}"}), 404
        cache = _import_cache_dir(import_path) / "render_cache"
        png = cache / f"{layer}_{DERIVED_RENDER_VERSION}.png"
        color_overrides = _parse_color_overrides(request.args.get("colors"))
        if color_overrides:
            png = cache / f"{layer}_{DERIVED_RENDER_VERSION}_{abs(hash(json.dumps(color_overrides, sort_keys=True)))}.png"
        if not png.exists() or png.stat().st_mtime < tif.stat().st_mtime:
            png.parent.mkdir(parents=True, exist_ok=True)
            meta = _meta_with_color_overrides(_generic_tif_meta(tif), color_overrides)
            _render_tif_png(tif, png, meta)
        return send_file(png, mimetype="image/png")
    color_overrides = _parse_color_overrides(request.args.get("colors"))
    render_tif = _derived_source_tif_path(output_dir, layer) if color_overrides and layer.endswith("_rgb") and layer not in NATIVE_RGB_LAYERS else None
    tif = render_tif if render_tif is not None else _materialize_export_tif(output_dir, layer)
    if not tif.exists():
        return jsonify({"ok": False, "error": f"图层不存在：{layer}"}), 404
    png = output_dir / "render_cache" / f"{layer}_{DERIVED_RENDER_VERSION}.png"
    if color_overrides:
        png = output_dir / "render_cache" / f"{layer}_{DERIVED_RENDER_VERSION}_{abs(hash(json.dumps(color_overrides, sort_keys=True)))}.png"
    if not png.exists() or png.stat().st_mtime < tif.stat().st_mtime:
        png.parent.mkdir(parents=True, exist_ok=True)
        meta = _meta_with_color_overrides(_layer_render_meta(layer), color_overrides)
        _render_tif_png(tif, png, meta)
    return send_file(png, mimetype="image/png")


@app.get("/api/tif/<layer>")
def tif_download(layer: str):
    output_dir = Path(request.args.get("output_dir") or LOCAL_OUTPUT_DIR)
    import_path = request.args.get("import_path")
    if import_path:
        tif = _imported_tif_by_key(import_path, layer)
        if tif is None:
            return jsonify({"ok": False, "error": f"TIF 不存在：{layer}"}), 404
        return send_file(tif, as_attachment=True, download_name=tif.name)
    tif = _materialize_export_tif(output_dir, layer)
    if not tif.exists():
        return jsonify({"ok": False, "error": f"TIF 不存在：{layer}"}), 404
    return send_file(tif, as_attachment=True, download_name=_export_tif_name(output_dir, layer))


@app.get("/api/tif-batch")
def tif_batch_download():
    output_dir = Path(request.args.get("output_dir") or LOCAL_OUTPUT_DIR)
    source_dir = output_dir / "clipped"
    if not source_dir.exists():
        source_dir = output_dir / "mosaic"
    if not source_dir.exists():
        return jsonify({"ok": False, "error": "没有可导出的 TIF 结果"}), 404

    tif_keys = []
    for key, group in LAYER_GROUPS.items():
        if _has_layer_asset(output_dir, key):
            tif_keys.append(key)
        for child in group["children"]:
            if _has_layer_asset(output_dir, child["key"]):
                tif_keys.append(child["key"])
    tif_keys = list(dict.fromkeys(tif_keys))
    tif_files = [(key, _materialize_export_tif(output_dir, key)) for key in tif_keys]
    tif_files = [(key, path) for key, path in tif_files if path.exists()]
    if not tif_files:
        return jsonify({"ok": False, "error": "没有可导出的 TIF 结果"}), 404

    export_dir = output_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    zip_path = export_dir / "processed_tif_results.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for key, tif in tif_files:
            zf.write(tif, arcname=_export_tif_name(output_dir, key))
    return send_file(zip_path, as_attachment=True, download_name=zip_path.name)


def _has_layer_asset(output_dir: Path, layer: str) -> bool:
    if layer in NATIVE_RGB_LAYERS:
        return _result_tif_path(output_dir, layer).exists()
    if layer.endswith("_rgb") or layer.endswith("_gray"):
        return _derived_source_tif_path(output_dir, layer).exists()
    return _result_tif_path(output_dir, layer).exists()


def _group_bounds_source_tif(output_dir: Path, key: str, group: dict) -> Path | None:
    source_tif = _result_tif_path(output_dir, key)
    if source_tif.exists():
        return source_tif
    for child in group.get("children", []):
        child_tif = _derived_source_tif_path(output_dir, child["key"])
        if child_tif.exists():
            return child_tif
    return None


def _layer_legend(layer: str) -> list[dict]:
    meta = _layer_render_meta(layer)
    if meta.get("type") == "class" and meta.get("colors"):
        legend = DERIVED_LAYER_META.get(layer, {}).get("legend") if layer in DERIVED_LAYER_META else LAYER_META.get(layer, {}).get("legend")
        if legend:
            return _legend_with_render_colors(legend, meta)
    if layer in DERIVED_LAYER_META:
        return DERIVED_LAYER_META[layer]["legend"]
    if layer.endswith("_rgb") or layer.endswith("_gray"):
        base = layer.rsplit("_", 1)[0]
        return LAYER_META.get(base, {}).get("legend", [])
    return LAYER_META.get(layer, {}).get("legend", [])


def _legend_with_render_colors(legend: list[dict], meta: dict) -> list[dict]:
    colors = meta.get("colors", {})
    out = []
    for item in legend:
        copied = dict(item)
        value = copied.get("value")
        if value in colors:
            copied["color"] = _rgb_to_hex(colors[value][:3])
        out.append(copied)
    return out


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*[max(0, min(255, int(v))) for v in rgb])


def _layer_render_meta(layer: str) -> dict:
    if layer in DERIVED_LAYER_META:
        derived = DERIVED_LAYER_META[layer]
        if layer.endswith("_rgb") or layer.endswith("_gray"):
            base = layer.rsplit("_", 1)[0]
            meta = dict(LAYER_META.get(base, {}))
            meta.update({key: value for key, value in derived.items() if key != "legend"})
            return meta or {"type": derived.get("type", "gradient")}
        return derived
    return LAYER_META.get(layer, LAYER_META["anomaly_combined"])


def _result_tif_path(output_dir: Path, layer: str) -> Path:
    clipped = output_dir / "clipped" / f"{layer}.tif"
    if clipped.exists():
        return clipped
    return output_dir / "mosaic" / f"{layer}.tif"


def _derived_source_tif_path(output_dir: Path, layer: str) -> Path:
    if layer in NATIVE_RGB_LAYERS:
        return _result_tif_path(output_dir, layer)
    if layer.endswith("_rgb"):
        return _result_tif_path(output_dir, layer[:-4])
    if layer.endswith("_gray"):
        return _result_tif_path(output_dir, layer[:-5])
    return _result_tif_path(output_dir, layer)


def _materialize_export_tif(output_dir: Path, layer: str) -> Path:
    if not (layer.endswith("_rgb") or layer.endswith("_gray")):
        return _result_tif_path(output_dir, layer)

    source_tif = _derived_source_tif_path(output_dir, layer)
    if not source_tif.exists():
        return source_tif

    export_dir = output_dir / "exports" / "derived_tif"
    export_dir.mkdir(parents=True, exist_ok=True)
    target = export_dir / f"{layer}_{DERIVED_RENDER_VERSION}.tif"
    if target.exists() and target.stat().st_mtime >= source_tif.stat().st_mtime:
        return target

    if layer.endswith("_rgb"):
        _write_rgb_preview_tif(source_tif, target, layer)
    else:
        _write_gray_preview_tif(source_tif, target)
    return target


def _export_tif_name(output_dir: Path, layer: str) -> str:
    metadata = _load_processing_metadata(output_dir)
    layer_name = _safe_filename(EXPORT_LAYER_NAMES.get(layer) or LAYER_META.get(layer, {}).get("name") or layer)
    date_part = _export_date_part(metadata)
    tile_part = _export_tile_part(metadata)
    parts = [layer_name]
    if date_part:
        parts.append(date_part)
    if tile_part:
        parts.append(tile_part)
    return "_".join(parts) + ".tif"


def _import_cache_dir(import_path: str | Path) -> Path:
    source = Path(import_path)
    safe_name = _safe_filename(str(source.resolve() if source.exists() else source))
    return IMPORTED_RESULTS_DIR / safe_name


def _resolve_import_root(import_path: str | Path) -> Path:
    source = Path(import_path)
    if not source.exists():
        raise FileNotFoundError(f"路径不存在：{source}")
    if source.is_dir():
        return source
    suffix = source.suffix.lower()
    if suffix in {".tif", ".tiff"}:
        return source
    if suffix != ".zip":
        raise ValueError("仅支持已处理结果目录、.zip、.tif 或 .tiff")
    target = _import_cache_dir(source) / "unzipped"
    marker = target / ".extract_ok"
    if not marker.exists() or marker.stat().st_mtime < source.stat().st_mtime:
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as zf:
            for member in zf.infolist():
                member_path = target / member.filename
                if not str(member_path.resolve()).startswith(str(target.resolve())):
                    raise ValueError(f"ZIP 包含非法路径：{member.filename}")
            zf.extractall(target)
        marker.write_text("ok", encoding="utf-8")
    return target


def _imported_tif_files(import_path: str | Path) -> list[Path]:
    source = Path(import_path)
    if source.is_file() and source.suffix.lower() in {".tif", ".tiff"}:
        return [source]
    root = _resolve_import_root(source)
    if root.is_file() and root.suffix.lower() in {".tif", ".tiff"}:
        return [root]
    return sorted([p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".tif", ".tiff"}])


def _imported_tif_key(path: Path, index: int) -> str:
    return f"imported_{index}_{_safe_filename(path.stem)}"


def _imported_tif_layers(import_path: str | Path) -> list[dict]:
    items = []
    for index, tif in enumerate(_imported_tif_files(import_path), 1):
        try:
            bounds = _tif_bounds_wgs84(tif)
        except Exception:
            continue
        key = _imported_tif_key(tif, index)
        query = f"import_path={quote(str(Path(import_path)))}"
        items.append(
            {
                "key": key,
                "name": tif.stem,
                "bounds": bounds,
                "png": f"/api/render/{key}.png?{query}",
                "tif": f"/api/tif/{key}?{query}",
                "legend": _imported_tif_legend(tif),
                "children": [],
            }
        )
    return items


def _imported_tif_by_key(import_path: str | Path, key: str) -> Path | None:
    for index, tif in enumerate(_imported_tif_files(import_path), 1):
        if _imported_tif_key(tif, index) == key:
            return tif
    return None


def _generic_tif_meta(tif: Path) -> dict:
    with rasterio.open(tif) as ds:
        if ds.count >= 3:
            return {"type": "rgb"}
        data = ds.read(1, masked=True).astype("float32")
    values = data.compressed()
    if values.size == 0:
        return {"type": "gray"}
    return {
        "type": "gradient",
        "min": float(np.nanpercentile(values, 2)),
        "max": float(np.nanpercentile(values, 98)),
        "gradient_low": (215, 48, 39),
        "gradient_mid": (254, 224, 139),
        "gradient_high": (26, 152, 80),
    }


def _imported_tif_legend(tif: Path) -> list[dict]:
    name = tif.stem.lower()
    for key in sorted(LAYER_META, key=len, reverse=True):
        if key.lower() in name:
            return _layer_legend(key)
    if "ndvi" in name:
        return _layer_legend("vitality_index_ndvi")
    if "ndre" in name:
        return _layer_legend("ndre_index")
    if "yield" in name or "产量" in tif.stem or "估产" in tif.stem:
        return _layer_legend("crop_yield_estimate")
    if "crop_type" in name or "作物类型" in tif.stem:
        return _layer_legend("crop_type_class")
    with rasterio.open(tif) as ds:
        if ds.count >= 3:
            return [{"kind": "note", "name": tif.name, "note": "三波段 GeoTIFF，按 RGB 影像方式预览。"}]
    return [{"kind": "note", "name": tif.name, "note": "已导入单波段 GeoTIFF，使用 2%-98% 分位数绿色渐变预览。"}]


def _load_processing_metadata(output_dir: Path) -> dict:
    metadata_path = output_dir / "processing_metadata.json"
    if not metadata_path.exists():
        return {}
    try:
        return json.loads(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _export_date_part(metadata: dict) -> str:
    products = metadata.get("products") or []
    acquisitions = []
    for product in products:
        acquisitions.extend(product.get("acquisitions") or [])
        if product.get("acquisition") and product.get("acquisition") != "median_composite":
            acquisitions.append(product["acquisition"])
    dates = sorted({_format_month_day(value) for value in acquisitions if _format_month_day(value)})
    if not dates:
        start = _format_iso_month_day(metadata.get("process_start_date"))
        end = _format_iso_month_day(metadata.get("process_end_date"))
        dates = [value for value in [start, end] if value]
    if not dates:
        return ""
    if len(dates) == 1:
        return dates[0]
    return f"{dates[0]}_{dates[-1]}"


def _format_month_day(acquisition: str | None) -> str:
    if not acquisition:
        return ""
    match = re.match(r"(\d{4})(\d{2})(\d{2})", acquisition)
    if not match:
        return ""
    return f"{int(match.group(1))}年{int(match.group(2))}月{int(match.group(3))}日"


def _format_iso_month_day(value: str | None) -> str:
    if not value:
        return ""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    if not match:
        return ""
    return f"{int(match.group(1))}年{int(match.group(2))}月{int(match.group(3))}日"


def _export_tile_part(metadata: dict) -> str:
    tiles = sorted({product.get("tile_id", "") for product in metadata.get("products", []) if product.get("tile_id")})
    if not tiles:
        return ""
    if len(tiles) == 1:
        return tiles[0]
    return f"{tiles[0]}等{len(tiles)}瓦片"


def _safe_filename(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("_")


def _start_download_job(params: dict, search_only: bool) -> None:
    if JOBS["download"]["status"] == "running":
        return
    DOWNLOAD_RUNTIME["stop_requested"] = False
    DOWNLOAD_RUNTIME["process"] = None

    def worker():
        product_type = (params.get("product_type", "SENTINEL-2-L2A") or "").upper()
        is_sentinel1 = "SENTINEL-1" in product_type
        provider = "ASF / Earthdata" if is_sentinel1 else "Copernicus Data Space"
        selected_ids = _selected_product_ids(params)
        item_progress = {product_id: {"percent": 0, "status": "等待下载"} for product_id in selected_ids}
        JOBS["download"] = {"status": "running", "message": f"正在连接 {provider}", "items": [], "percent": 8, "progress_text": "连接中", "item_progress": item_progress}
        try:
            if is_sentinel1:
                script = S1_ASF_DOWNLOAD_SCRIPT
                cmd = _build_s1_asf_download_cmd(params, search_only)
            else:
                script = SENTINEL_DOWNLOAD_SCRIPT
                cmd = _build_cdse_download_cmd(params, search_only)
            if search_only:
                message = "正在通过 ASF / NASA Earthdata 检索 Sentinel-1 数据" if is_sentinel1 else "正在通过 Copernicus Data Space 检索 Sentinel-2 数据"
                progress_text = "ASF 检索中" if is_sentinel1 else "检索中"
                JOBS["download"].update({"percent": 30, "message": message, "progress_text": progress_text})
            else:
                message = "正在通过 ASF / NASA Earthdata 下载 Sentinel-1 数据" if is_sentinel1 else "正在通过 Copernicus Data Space 下载 Sentinel-2 数据"
                progress_text = "ASF 下载中" if is_sentinel1 else "下载中"
                JOBS["download"].update({"percent": 18, "message": message, "progress_text": progress_text})
            process = subprocess.Popen(cmd, cwd=str(script.parent), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            DOWNLOAD_RUNTIME["process"] = process
            stdout_lines = []
            if process.stdout is not None:
                for line in process.stdout:
                    stdout_lines.append(line)
                    _update_download_item_progress(line, item_progress)
            try:
                stderr = process.stderr.read() if process.stderr is not None else ""
                process.wait(timeout=7200)
            except subprocess.TimeoutExpired:
                DOWNLOAD_RUNTIME["stop_requested"] = True
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                stderr = process.stderr.read() if process.stderr is not None else ""
            stdout = "".join(stdout_lines)
            if DOWNLOAD_RUNTIME.get("stop_requested"):
                message = "数据下载/检索任务已停止"
                if is_sentinel1:
                    message = "Sentinel-1 ASF / NASA Earthdata 下载/检索任务已停止"
                JOBS["download"] = {"status": "stopped", "message": message, "items": [], "bounds": None, "geojson": None, "percent": JOBS["download"].get("percent", 0), "progress_text": "已停止", "item_progress": item_progress}
                return
            status = "done" if process.returncode == 0 else "error"
            search_results = _parse_search_results(stdout) if search_only and process.returncode == 0 else {"items": []}
            message = stdout[-4000:] if process.returncode == 0 else (stderr or stdout)[-4000:]
            if is_sentinel1:
                message = _format_s1_asf_message(message)
            JOBS["download"] = {
                "status": status,
                "message": message,
                "items": search_results.get("items", []),
                "bounds": search_results.get("bounds"),
                "geojson": search_results.get("geojson"),
                "percent": 100 if status == "done" else 0,
                "progress_text": "完成" if status == "done" else "失败",
                "item_progress": item_progress,
            }
        except Exception as exc:
            message = _format_s1_asf_message(str(exc)) if is_sentinel1 else str(exc)
            JOBS["download"] = {"status": "error", "message": message, "items": [], "bounds": None, "geojson": None, "percent": 0, "progress_text": "失败", "item_progress": item_progress}
        finally:
            DOWNLOAD_RUNTIME["process"] = None
            DOWNLOAD_RUNTIME["stop_requested"] = False

    threading.Thread(target=worker, daemon=True).start()


def _format_s1_asf_message(message: str) -> str:
    lines = [
        line
        for line in (message or "").splitlines()
        if "Copernicus" not in line
    ]
    cleaned = "\n".join(lines).strip()
    prefix = "Sentinel-1 正在使用 ASF / NASA Earthdata 检索下载。\n"
    return prefix + cleaned if cleaned else prefix.strip()


def _update_download_item_progress(line: str, item_progress: dict) -> None:
    if DOWNLOAD_PROGRESS_MARKER not in line:
        return
    raw = line.split(DOWNLOAD_PROGRESS_MARKER, 1)[1].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return
    product_id = str(data.get("id") or "").strip()
    if not product_id:
        return
    percent = max(0, min(100, int(data.get("percent") or 0)))
    item_progress[product_id] = {"percent": percent, "status": data.get("status") or "下载中"}
    JOBS["download"]["item_progress"] = item_progress
    if item_progress:
        values = [int(item.get("percent") or 0) for item in item_progress.values()]
        JOBS["download"]["percent"] = max(18, min(99, int(sum(values) / max(len(values), 1))))
    JOBS["download"]["progress_text"] = f"{data.get('status') or '下载中'} {percent}%"


def _parse_search_results(stdout: str) -> dict:
    payload = {"items": [], "bounds": None, "geojson": None}
    for line in (stdout or "").splitlines():
        if SEARCH_JSON_MARKER not in line:
            continue
        raw = line.split(SEARCH_JSON_MARKER, 1)[1].strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        items = data.get("items") or []
        payload["items"] = items
        payload["bounds"] = _merge_item_bounds(items)
        payload["geojson"] = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {k: v for k, v in item.items() if k not in {"geometry", "bounds"}},
                    "geometry": item["geometry"],
                }
                for item in items
                if item.get("geometry")
            ],
        }
        break
    return payload


def _merge_item_bounds(items: list[dict]) -> list[list[float]] | None:
    bounds = [item.get("bounds") for item in items if item.get("bounds")]
    if not bounds:
        return None
    south = min(bound[0][0] for bound in bounds)
    west = min(bound[0][1] for bound in bounds)
    north = max(bound[1][0] for bound in bounds)
    east = max(bound[1][1] for bound in bounds)
    return [[south, west], [north, east]]


def _build_cdse_download_cmd(params: dict, search_only: bool) -> list[str]:
    collection = _download_sentinel2_collection(params.get("product_type", "SENTINEL-2-L2A"))
    limit = _optional_positive_int(params.get("limit"))
    cmd = [
        sys.executable,
        str(SENTINEL_DOWNLOAD_SCRIPT),
        "--start-date",
        params.get("start_date"),
        "--end-date",
        params.get("end_date"),
        "--aoi",
        params.get("aoi_path"),
        "--max-workers",
        str(max(1, min(10, int(params.get("max_workers") or 2)))),
        "--output-dir",
        params.get("zip_dir") or LOCAL_ZIP_DIR,
        "--extract-dir",
        params.get("safe_dir") or LOCAL_SAFE_DIR,
    ]
    if limit:
        cmd.extend(["--limit", str(limit)])
    if collection:
        cmd.extend(["--collection", collection])
    if params.get("contains"):
        cmd.extend(["--contains", params["contains"]])
    if params.get("cloud_max") not in ("", None):
        cmd.extend(["--cloud-max", str(params["cloud_max"])])
    selected_product_ids = _selected_product_ids(params)
    if selected_product_ids:
        cmd.extend(["--product-ids", ",".join(selected_product_ids)])
    if search_only:
        cmd.append("--search-only")
    return cmd


def _build_s1_asf_download_cmd(params: dict, search_only: bool) -> list[str]:
    default_s1_zip_dir = str(APP_DIR / "data" / "S1" / "DANGYANG" / "raw_zip")
    default_s1_safe_dir = str(APP_DIR / "data" / "S1" / "DANGYANG" / "raw_safe")
    zip_dir = params.get("zip_dir") or default_s1_zip_dir
    safe_dir = params.get("safe_dir") or default_s1_safe_dir
    if Path(zip_dir) == Path(LOCAL_ZIP_DIR):
        zip_dir = default_s1_zip_dir
    if Path(safe_dir) == Path(LOCAL_SAFE_DIR):
        safe_dir = default_s1_safe_dir
    limit = _optional_positive_int(params.get("limit"))
    cmd = [
        sys.executable,
        str(S1_ASF_DOWNLOAD_SCRIPT),
        "--start-date",
        params.get("start_date"),
        "--end-date",
        params.get("end_date"),
        "--beam-mode",
        "IW",
        "--processing-level",
        "GRD_HD",
        "--polarization",
        "VV+VH",
        "--output-dir",
        zip_dir,
        "--extract-dir",
        safe_dir,
        "--no-open-login-on-401",
    ]
    if limit:
        cmd.extend(["--max-results", str(limit)])
    if params.get("aoi_path"):
        cmd.extend(["--aoi-geojson", params["aoi_path"]])
    else:
        cmd.append("--no-aoi")
    if search_only:
        cmd.append("--search-only")
    return cmd


def _optional_positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _selected_product_ids(params: dict) -> list[str]:
    raw = params.get("selected_product_ids") or []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        return []
    seen = set()
    out = []
    for value in raw:
        product_id = str(value or "").strip()
        if product_id and product_id not in seen:
            seen.add(product_id)
            out.append(product_id)
    return out


def _download_sentinel2_collection(product_type: str) -> str:
    product_type = (product_type or "").upper()
    if product_type == "SENTINEL-2-L1C":
        return "sentinel-2-l1c"
    return "sentinel-2-l2a"


def _parse_color_overrides(raw: str | None) -> dict[int, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {int(key): value for key, value in data.items() if isinstance(value, str) and value.startswith("#")}


def _meta_with_color_overrides(meta: dict, overrides: dict[int, str]) -> dict:
    if not overrides:
        return meta
    copied = dict(meta)
    copied["colors"] = dict(meta.get("colors", {}))
    for value, hex_color in overrides.items():
        alpha = copied["colors"].get(value, (0, 0, 0, 190))[3]
        copied["colors"][value] = (*_hex_to_rgb(hex_color), alpha)
    if copied.get("type") == "gradient":
        legend_items = [item for item in copied.get("legend", []) if item.get("value") is not None]
        if legend_items:
            vmin = float(copied.get("min", 0))
            vmax = float(copied.get("max", 1))
            positions = np.linspace(vmin, vmax, len(legend_items))
            copied["gradient_stops"] = [
                (float(position), _hex_to_rgb(overrides.get(item["value"], item.get("color", "#000000"))))
                for position, item in zip(positions, legend_items)
            ]
    return copied


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _write_rgb_preview_tif(source_tif: Path, target_tif: Path, layer: str) -> None:
    with rasterio.open(source_tif) as src:
        if src.count >= 3:
            data = src.read([1, 2, 3])
        else:
            base_layer = layer[:-4]
            data = _render_tif_rgb_data(source_tif, LAYER_META.get(base_layer, LAYER_META["crop_yield_estimate"]))
        profile = src.profile.copy()
        profile.update(driver="GTiff", dtype="uint8", count=3, nodata=0, compress="deflate", tiled=True)
        with rasterio.open(target_tif, "w", **profile) as dst:
            dst.write(data.astype("uint8"))


def _write_gray_preview_tif(source_tif: Path, target_tif: Path) -> None:
    with rasterio.open(source_tif) as src:
        band = src.read(1).astype("float32")
        valid = np.isfinite(band)
        if src.nodata is not None:
            valid &= band != src.nodata
        out = np.zeros(band.shape, dtype="uint8")
        if np.any(valid):
            values = band[valid]
            vmin = float(np.nanpercentile(values, 2))
            vmax = float(np.nanpercentile(values, 98))
            if vmax <= vmin:
                vmin = float(np.nanmin(values))
                vmax = float(np.nanmax(values))
            scaled = np.clip((band - vmin) / max(vmax - vmin, 1e-6), 0, 1)
            out[valid] = (1 + scaled[valid] * 254).astype("uint8")
        profile = src.profile.copy()
        profile.update(driver="GTiff", dtype="uint8", count=1, nodata=0, compress="deflate", tiled=True)
        with rasterio.open(target_tif, "w", **profile) as dst:
            dst.write(out, 1)


def _tif_bounds_wgs84(tif: Path) -> list[list[float]]:
    with rasterio.open(tif) as ds:
        left, bottom, right, top = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
    return [[bottom, left], [top, right]]


def _vector_bounds_wgs84(path: Path) -> list[list[float]]:
    if path.suffix.lower() == ".shp":
        geometries, crs = _read_shp_as_geojson_features(path)
        coords = []
        for feature in geometries:
            coords.extend(_flatten_coords(feature["geometry"].get("coordinates", [])))
        if not coords:
            raise ValueError("SHP 文件没有可用坐标")
        xs = [xy[0] for xy in coords]
        ys = [xy[1] for xy in coords]
        left, bottom, right, top = min(xs), min(ys), max(xs), max(ys)
        if crs:
            left, bottom, right, top = transform_bounds(crs, "EPSG:4326", left, bottom, right, top, densify_pts=21)
        return [[bottom, left], [top, right]]
    data = json.loads(path.read_text(encoding="utf-8"))
    coords = []
    for geometry in _geojson_geometries(data):
        coords.extend(_flatten_coords(geometry.get("coordinates", [])))
    if not coords:
        raise ValueError("矢量文件没有可用坐标")
    xs = [xy[0] for xy in coords]
    ys = [xy[1] for xy in coords]
    return [[min(ys), min(xs)], [max(ys), max(xs)]]


def _validate_vector_upload(path: Path) -> None:
    if path.suffix.lower() != ".shp":
        return
    required = [".shp", ".shx", ".dbf"]
    missing = [suffix for suffix in required if not path.with_suffix(suffix).exists()]
    if missing:
        raise ValueError(f"SHP 上传不完整：缺少同名配套文件 {'、'.join(missing)}。请同时选择 .shp、.shx、.dbf、.prj 后上传。")
    if not path.with_suffix(".prj").exists():
        raise ValueError("SHP 缺少 .prj 投影文件，无法可靠转换为经纬度检索区域。请同时上传同名 .prj，或先转换为 EPSG:4326 GeoJSON。")


def _vector_preview_geojson(path: Path) -> dict:
    if path.suffix.lower() == ".shp":
        raw_features, crs = _read_shp_as_geojson_features(path)
        features = []
        for feature in raw_features:
            geometry = feature.get("geometry")
            if geometry:
                geometry = transform_geom(crs, "EPSG:4326", geometry) if crs else geometry
                features.append({"type": "Feature", "properties": {}, "geometry": geometry})
        return {"type": "FeatureCollection", "features": features}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        return data
    if data.get("type") == "Feature":
        return {"type": "FeatureCollection", "features": [data]}
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": data}]}


def _geojson_geometries(data: dict) -> list[dict]:
    if data.get("type") == "FeatureCollection":
        return [f["geometry"] for f in data.get("features", []) if f.get("geometry")]
    if data.get("type") == "Feature":
        return [data["geometry"]]
    if data.get("type") in {"Polygon", "MultiPolygon"}:
        return [data]
    return []


def _read_shp_as_geojson_features(path: Path) -> tuple[list[dict], str]:
    try:
        import fiona

        features = []
        with fiona.open(path) as src:
            crs = src.crs_wkt or src.crs or "EPSG:4326"
            for feature in src:
                geometry = feature.get("geometry")
                if geometry:
                    features.append({"type": "Feature", "properties": {}, "geometry": dict(geometry)})
        return features, crs
    except ImportError:
        import shapefile

        prj = path.with_suffix(".prj")
        crs = prj.read_text(encoding="utf-8", errors="ignore") if prj.exists() else "EPSG:4326"
        reader = shapefile.Reader(str(path))
        features = [
            {"type": "Feature", "properties": {}, "geometry": shape.__geo_interface__}
            for shape in reader.shapes()
            if shape.__geo_interface__.get("type") in {"Polygon", "MultiPolygon"}
        ]
        return features, crs


def _flatten_coords(coords):
    if not coords:
        return []
    if isinstance(coords[0], (float, int)):
        return [coords]
    out = []
    for item in coords:
        out.extend(_flatten_coords(item))
    return out


def _render_tif_png(tif: Path, png: Path, meta: dict) -> None:
    max_size = 1800
    with rasterio.open(tif) as ds:
        scale = max(ds.width / max_size, ds.height / max_size, 1)
        out_width = max(1, int(ds.width / scale))
        out_height = max(1, int(ds.height / scale))
        if ds.count >= 3:
            raw = ds.read([1, 2, 3], out_shape=(3, out_height, out_width), resampling=Resampling.nearest)
            if _is_display_rgb(raw):
                rgb_bands = np.clip(raw, 0, 255).astype("uint8")
            else:
                rgb_bands = _stretch_render_rgb(raw.astype("float32"))
            rgb = np.transpose(rgb_bands, (1, 2, 0)).astype("uint8")
            alpha = np.where(np.any(raw > 0, axis=0), RESULT_PNG_ALPHA, 0).astype("uint8")
            rgba = np.dstack([rgb, alpha])
            Image.fromarray(rgba, mode="RGBA").save(png)
            return
        data = ds.read(1, out_shape=(out_height, out_width), resampling=Resampling.nearest)
        nodata = ds.nodata

    rgba = _render_single_band_rgba(data, nodata, meta)
    Image.fromarray(rgba, mode="RGBA").save(png)


def _render_tif_rgb_data(tif: Path, meta: dict) -> np.ndarray:
    with rasterio.open(tif) as ds:
        data = ds.read(1)
        nodata = ds.nodata
    rgba = _render_single_band_rgba(data, nodata, meta)
    return np.transpose(rgba[..., :3], (2, 0, 1))


def _is_display_rgb(raw: np.ndarray) -> bool:
    if np.issubdtype(raw.dtype, np.integer) and raw.dtype.itemsize == 1:
        return True
    finite = raw[np.isfinite(raw)]
    if finite.size == 0:
        return False
    return float(np.nanmin(finite)) >= 0 and float(np.nanmax(finite)) <= 255


def _stretch_render_rgb(raw: np.ndarray) -> np.ndarray:
    rgb = np.zeros(raw.shape, dtype=np.uint8)
    valid = np.all(np.isfinite(raw), axis=0) & np.any(raw > 0, axis=0)
    if not np.any(valid):
        return rgb
    for band_index in range(raw.shape[0]):
        band = raw[band_index]
        values = band[valid]
        low = float(np.nanpercentile(values, 2))
        high = float(np.nanpercentile(values, 98))
        scaled = np.clip((band - low) / max(high - low, 1e-6), 0, 1)
        scaled = np.power(scaled, 1.05)
        rgb[band_index] = (scaled * 255).astype(np.uint8)
    rgb[:, ~valid] = 0
    return rgb


def _render_single_band_rgba(data: np.ndarray, nodata, meta: dict) -> np.ndarray:
    rgba = np.zeros((data.shape[0], data.shape[1], 4), dtype=np.uint8)
    valid = np.ones(data.shape, dtype=bool)
    if nodata is not None:
        valid &= data != nodata
    valid &= np.isfinite(data)
    nodata_mask = ~valid
    if meta.get("type") == "gray":
        gray = np.clip(data.astype("float32"), 0, 255).astype(np.uint8)
        rgba[..., 0] = gray
        rgba[..., 1] = gray
        rgba[..., 2] = gray
        rgba[..., 3] = np.where(valid & (gray > 0), RESULT_PNG_ALPHA, 0).astype(np.uint8)
    elif meta.get("type") == "threshold":
        breaks = [float(value) for value in meta.get("breaks", [])]
        colors = meta.get("colors", {})
        classes = np.ones(data.shape, dtype=np.uint8)
        if len(breaks) >= 1:
            classes[data > breaks[0]] = 2
        if len(breaks) >= 2:
            classes[data > breaks[1]] = 3
        for value, color in colors.items():
            rgba[valid & (classes == int(value))] = color
    elif meta.get("type") == "gradient":
        vmin = float(meta.get("min", np.nanmin(data[valid]) if np.any(valid) else 0))
        vmax = float(meta.get("max", np.nanmax(data[valid]) if np.any(valid) else 1))
        stops = meta.get("gradient_stops")
        if stops:
            values = [float(item[0]) for item in stops]
            colors = [item[1] for item in stops]
            scaled = np.clip(data.astype("float32"), min(values), max(values))
            for channel in range(3):
                rgba[..., channel] = np.interp(scaled, values, [color[channel] for color in colors]).astype(np.uint8)
        else:
            norm = np.clip((data.astype("float32") - vmin) / max(vmax - vmin, 1e-6), 0, 1)
            low = meta.get("gradient_low", (215, 48, 39))
            mid = meta.get("gradient_mid", (253, 174, 97))
            high = meta.get("gradient_high", (26, 152, 80))
            lower = norm <= 0.5
            t_low = np.clip(norm / 0.5, 0, 1)
            t_high = np.clip((norm - 0.5) / 0.5, 0, 1)
            for channel in range(3):
                rgba[..., channel] = np.where(
                    lower,
                    low[channel] * (1 - t_low) + mid[channel] * t_low,
                    mid[channel] * (1 - t_high) + high[channel] * t_high,
                ).astype(np.uint8)
        rgba[..., 3] = np.where(valid, RESULT_PNG_ALPHA, 0).astype(np.uint8)
    else:
        for value, color in meta.get("colors", {}).items():
            rgba[data == value] = color
    rgba[nodata_mask] = (0, 0, 0, 0)
    return rgba


def _clear_render_cache(output_dir: Path) -> None:
    cache = output_dir / "render_cache"
    if cache.exists():
        shutil.rmtree(cache)


def run_server(host: str = "127.0.0.1", port: int = 5078, open_browser: bool = True) -> None:
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    app.run(host=host, port=port, debug=False, threaded=True)
