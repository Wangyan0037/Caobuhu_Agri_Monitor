from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import webbrowser
import zipfile
from datetime import date, timedelta
from pathlib import Path
from tkinter import Tk, filedialog

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
            {"value": 2, "name": "NDVI 0.30-0.55 中", "color": "#fdae61", "note": "中等生长水平。"},
            {"value": 3, "name": "NDVI 0.55-0.80 高", "color": "#1a9850", "note": "植被活力高。"},
        ],
        "type": "gradient",
        "min": 0.0,
        "max": 0.8,
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
    "anomaly_drought": {"name": "疑似干旱", "legend": [{"value": 1, "name": "NDWI ≤ -0.15", "color": "#a6611a", "note": "低水分指数，可能导致长势下降。"}], "type": "class", "colors": {1: (166, 97, 26, 215)}},
    "anomaly_waterlogging": {"name": "疑似积水", "legend": [{"value": 1, "name": "NDWI ≥ 0.30 或 LSWI ≥ 0.40", "color": "#2166ac", "note": "水分指数异常偏高，需关注排涝。"}], "type": "class", "colors": {1: (33, 102, 172, 215)}},
    "anomaly_fallow": {"name": "疑似撂荒", "legend": [{"value": 1, "name": "NDVI ≤ 0.35 且 NDBI ≥ 0.05", "color": "#bdbdbd", "note": "长期低植被活力或裸土特征明显。"}], "type": "class", "colors": {1: (189, 189, 189, 210)}},
    "anomaly_nonagri": {"name": "疑似非农化", "legend": [{"value": 1, "name": "NDVI ≤ 0.30 且 NDBI增强", "color": "#d7191c", "note": "植被下降并出现建设/裸土增强信号。"}], "type": "class", "colors": {1: (215, 25, 28, 220)}},
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
    if "file" in request.files:
        uploaded = request.files["file"]
        target = AOI_DIR / uploaded.filename
        uploaded.save(target)
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
                output_dir=params.get("output_dir") or LOCAL_OUTPUT_DIR,
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


@app.get("/api/layers")
def layers():
    output_dir = Path(request.args.get("output_dir") or LOCAL_OUTPUT_DIR)
    mosaic_dir = output_dir / "clipped"
    if not mosaic_dir.exists():
        mosaic_dir = output_dir / "mosaic"
    items = []
    for tif in sorted(mosaic_dir.glob("*.tif")):
        key = tif.stem
        if key not in LAYER_META:
            continue
        bounds = _tif_bounds_wgs84(tif)
        meta = LAYER_META[key]
        items.append({"key": key, "name": meta["name"], "bounds": bounds, "png": f"/api/render/{key}.png?output_dir={output_dir}", "tif": f"/api/tif/{key}?output_dir={output_dir}", "legend": meta["legend"]})
    return jsonify({"ok": True, "items": items})


@app.get("/api/render/<layer>.png")
def render_layer(layer: str):
    output_dir = Path(request.args.get("output_dir") or LOCAL_OUTPUT_DIR)
    tif = _result_tif_path(output_dir, layer)
    if not tif.exists():
        return jsonify({"ok": False, "error": f"图层不存在：{layer}"}), 404
    png = output_dir / "render_cache" / f"{layer}.png"
    color_overrides = _parse_color_overrides(request.args.get("colors"))
    if color_overrides:
        png = output_dir / "render_cache" / f"{layer}_{abs(hash(json.dumps(color_overrides, sort_keys=True)))}.png"
    if not png.exists() or png.stat().st_mtime < tif.stat().st_mtime:
        png.parent.mkdir(parents=True, exist_ok=True)
        meta = _meta_with_color_overrides(LAYER_META.get(layer, LAYER_META["anomaly_combined"]), color_overrides)
        _render_tif_png(tif, png, meta)
    return send_file(png, mimetype="image/png")


@app.get("/api/tif/<layer>")
def tif_download(layer: str):
    output_dir = Path(request.args.get("output_dir") or LOCAL_OUTPUT_DIR)
    tif = _result_tif_path(output_dir, layer)
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

    tif_files = [path for path in sorted(source_dir.glob("*.tif")) if path.stem in LAYER_META]
    if not tif_files:
        return jsonify({"ok": False, "error": "没有可导出的 TIF 结果"}), 404

    export_dir = output_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    zip_path = export_dir / "processed_tif_results.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for tif in tif_files:
            zf.write(tif, arcname=_export_tif_name(output_dir, tif.stem))
    return send_file(zip_path, as_attachment=True, download_name=zip_path.name)


def _result_tif_path(output_dir: Path, layer: str) -> Path:
    clipped = output_dir / "clipped" / f"{layer}.tif"
    if clipped.exists():
        return clipped
    return output_dir / "mosaic" / f"{layer}.tif"


def _export_tif_name(output_dir: Path, layer: str) -> str:
    metadata = _load_processing_metadata(output_dir)
    layer_name = _safe_filename(LAYER_META.get(layer, {}).get("name") or layer)
    date_part = _export_date_part(metadata)
    tile_part = _export_tile_part(metadata)
    parts = [layer_name]
    if date_part:
        parts.append(date_part)
    if tile_part:
        parts.append(tile_part)
    return "_".join(parts) + ".tif"


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
        JOBS["download"] = {"status": "running", "message": f"正在连接 {provider}", "items": [], "percent": 8, "progress_text": "连接中"}
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
            try:
                stdout, stderr = process.communicate(timeout=7200)
            except subprocess.TimeoutExpired:
                DOWNLOAD_RUNTIME["stop_requested"] = True
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
            if DOWNLOAD_RUNTIME.get("stop_requested"):
                message = "数据下载/检索任务已停止"
                if is_sentinel1:
                    message = "Sentinel-1 ASF / NASA Earthdata 下载/检索任务已停止"
                JOBS["download"] = {"status": "stopped", "message": message, "items": [], "percent": JOBS["download"].get("percent", 0), "progress_text": "已停止"}
                return
            status = "done" if process.returncode == 0 else "error"
            message = stdout[-4000:] if process.returncode == 0 else (stderr or stdout)[-4000:]
            if is_sentinel1:
                message = _format_s1_asf_message(message)
            JOBS["download"] = {"status": status, "message": message, "items": [], "percent": 100 if status == "done" else 0, "progress_text": "完成" if status == "done" else "失败"}
        except Exception as exc:
            message = _format_s1_asf_message(str(exc)) if is_sentinel1 else str(exc)
            JOBS["download"] = {"status": "error", "message": message, "items": [], "percent": 0, "progress_text": "失败"}
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
        legend = copied.get("legend", [])
        if legend:
            sorted_values = sorted(overrides)
            if sorted_values:
                copied["gradient_low"] = _hex_to_rgb(overrides[sorted_values[0]])
                copied["gradient_mid"] = _hex_to_rgb(overrides[sorted_values[len(sorted_values) // 2]])
                copied["gradient_high"] = _hex_to_rgb(overrides[sorted_values[-1]])
    return copied


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


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
        data = ds.read(1, out_shape=(out_height, out_width), resampling=Resampling.nearest)
        nodata = ds.nodata

    rgba = np.zeros((data.shape[0], data.shape[1], 4), dtype=np.uint8)
    valid = np.ones(data.shape, dtype=bool)
    if nodata is not None:
        valid &= data != nodata
    valid &= np.isfinite(data)
    if meta.get("type") == "gradient":
        vmin = float(meta.get("min", np.nanmin(data[valid]) if np.any(valid) else 0))
        vmax = float(meta.get("max", np.nanmax(data[valid]) if np.any(valid) else 1))
        norm = np.clip((data.astype("float32") - vmin) / max(vmax - vmin, 1e-6), 0, 1)
        low = meta.get("gradient_low", (215, 48, 39))
        high = meta.get("gradient_high", (26, 152, 80))
        rgba[..., 0] = (low[0] * (1 - norm) + high[0] * norm).astype(np.uint8)
        rgba[..., 1] = (low[1] * (1 - norm) + high[1] * norm).astype(np.uint8)
        rgba[..., 2] = (low[2] * (1 - norm) + high[2] * norm).astype(np.uint8)
        rgba[..., 3] = np.where(valid, 185, 0).astype(np.uint8)
    else:
        for value, color in meta.get("colors", {}).items():
            rgba[data == value] = color
        rgba[~valid] = (0, 0, 0, 0)
    Image.fromarray(rgba, mode="RGBA").save(png)


def _clear_render_cache(output_dir: Path) -> None:
    cache = output_dir / "render_cache"
    if cache.exists():
        shutil.rmtree(cache)


def run_server(host: str = "127.0.0.1", port: int = 5078, open_browser: bool = True) -> None:
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}/")).start()
    app.run(host=host, port=port, debug=False, threaded=True)
