from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.mask import mask
from rasterio.merge import merge
from rasterio.crs import CRS
from rasterio.warp import transform_geom
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window, transform as window_transform

from config import FLOAT_NODATA_VALUE, LOCAL_OUTPUT_DIR, LOCAL_SAFE_DIR, NODATA_VALUE, THRESHOLDS


OUTPUT_NAMES = [
    "vitality_grade",
    "moisture_grade",
    "anomaly_combined",
    "patrol_priority",
    "vitality_index_ndvi",
    "ndvi_last_year_delta",
    "crop_type_class",
    "crop_yield_estimate",
    "anomaly_weak_vitality",
    "anomaly_vitality_decline",
    "anomaly_drought",
    "anomaly_waterlogging",
    "anomaly_fallow",
    "anomaly_nonagri",
]

RGB_OUTPUT_NAMES = {"truecolor_rgb", "falsecolor_rgb", "truecolor_preview_rgb", "falsecolor_preview_rgb"}
ALWAYS_OUTPUT_NAMES = {"truecolor_rgb", "falsecolor_rgb", "truecolor_preview_rgb", "falsecolor_preview_rgb"}

SUPPORT_OUTPUTS = {
    "crop_type_class": {"vitality_index_ndvi", "ndre_index", "truecolor_rgb", "falsecolor_rgb", "truecolor_preview_rgb", "falsecolor_preview_rgb"},
    "crop_yield_estimate": {"vitality_index_ndvi", "ndre_index", "truecolor_rgb", "falsecolor_rgb", "truecolor_preview_rgb", "falsecolor_preview_rgb"},
}

WATER_SCL_CLASSES = {6}


@dataclass(frozen=True)
class SafeProduct:
    safe_dir: Path
    tile_id: str
    acquisition: str
    bands: dict[str, Path]


def discover_safe_products(raw_safe_dir: str | Path) -> list[SafeProduct]:
    root = Path(raw_safe_dir)
    products: list[SafeProduct] = []
    for safe_dir in sorted(root.glob("*.SAFE")):
        img_dirs = list(safe_dir.glob("GRANULE/*/IMG_DATA"))
        if not img_dirs:
            continue
        bands: dict[str, Path] = {}
        for band in ("B02", "B03", "B04", "B08"):
            matches = sorted(img_dirs[0].glob(f"R10m/*_{band}_10m.jp2"))
            if matches:
                bands[band] = matches[0]
        for band in ("B05", "B11", "B12", "SCL"):
            matches = sorted(img_dirs[0].glob(f"R20m/*_{band}_20m.jp2"))
            if matches:
                bands[band] = matches[0]
        missing = {"B02", "B03", "B04", "B05", "B08", "B11", "B12", "SCL"} - set(bands)
        if missing:
            continue
        tile_match = re.search(r"_(T\d{2}[A-Z]{3})_", safe_dir.name)
        date_match = re.search(r"MSIL2A_(\d{8}T\d{6})", safe_dir.name)
        products.append(
            SafeProduct(
                safe_dir=safe_dir,
                tile_id=tile_match.group(1) if tile_match else safe_dir.stem,
                acquisition=date_match.group(1) if date_match else "unknown_date",
                bands=bands,
            )
        )
    if not products:
        raise FileNotFoundError(f"No usable Sentinel-2 L2A .SAFE folders found under {root}")
    return products


def latest_complete_products(products: list[SafeProduct]) -> list[SafeProduct]:
    counts = {
        acquisition: sum(1 for product in products if product.acquisition == acquisition)
        for acquisition in {product.acquisition for product in products}
    }
    max_count = max(counts.values())
    selected_acquisition = max(acquisition for acquisition, count in counts.items() if count == max_count)
    return [product for product in products if product.acquisition == selected_acquisition]


def group_products_by_tile(products: list[SafeProduct]) -> list[list[SafeProduct]]:
    grouped: dict[str, list[SafeProduct]] = {}
    for product in products:
        grouped.setdefault(product.tile_id, []).append(product)
    return [sorted(group, key=lambda p: p.acquisition) for _, group in sorted(grouped.items())]


def available_acquisitions(raw_safe_dir: str | Path = LOCAL_SAFE_DIR) -> list[dict]:
    products = discover_safe_products(raw_safe_dir)
    rows = []
    for acquisition in sorted({product.acquisition for product in products}, reverse=True):
        selected = [product for product in products if product.acquisition == acquisition]
        rows.append(
            {
                "acquisition": acquisition,
                "date": acquisition[:8],
                "tiles": sorted(product.tile_id for product in selected),
                "count": len(selected),
            }
        )
    return rows


def run_local_processing(
    raw_safe_dir: str | Path = LOCAL_SAFE_DIR,
    output_dir: str | Path = LOCAL_OUTPUT_DIR,
    acquisition: str | None = None,
    all_products: bool = False,
    selected_outputs: list[str] | None = None,
    processing_params: dict | None = None,
    aoi_path: str | Path | None = None,
    radar_safe_dir: str | Path | None = None,
    process_scope_dir: str | Path | None = None,
    process_scope_path: str | Path | None = None,
    process_start_date: str | None = None,
    process_end_date: str | None = None,
    should_stop: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    output_root = Path(output_dir)
    tile_root = output_root / "tiles"
    mosaic_root = output_root / "mosaic"
    tile_root.mkdir(parents=True, exist_ok=True)
    mosaic_root.mkdir(parents=True, exist_ok=True)

    discovered = discover_safe_products(raw_safe_dir)
    discovered = _filter_products_by_date(discovered, process_start_date, process_end_date)
    if not discovered:
        raise ValueError(f"No SAFE products found in processing date range: {process_start_date or '-'} to {process_end_date or '-'}")
    if all_products:
        products = discovered
        selection = "all products"
    elif acquisition:
        products = [p for p in discovered if p.acquisition == acquisition or p.acquisition.startswith(acquisition)]
        if not products:
            raise ValueError(f"No SAFE products match acquisition/date: {acquisition}")
        selection = f"acquisition/date {acquisition}"
    else:
        products = discovered
        selection = "single scene where available; per-tile median composite when multiple scenes exist in date range"

    outputs_to_write = [name for name in (selected_outputs or OUTPUT_NAMES) if name in OUTPUT_NAMES]
    if not outputs_to_write:
        raise ValueError("No valid output products selected.")
    extra_outputs = set()
    for output_name in outputs_to_write:
        extra_outputs.update(SUPPORT_OUTPUTS.get(output_name, set()))
    extra_outputs.update(ALWAYS_OUTPUT_NAMES)
    outputs_to_compute = outputs_to_write + [name for name in sorted(extra_outputs) if name not in outputs_to_write]
    params = _merged_processing_params(processing_params or {})
    aoi_geometries = None
    aoi_crs = None
    if aoi_path:
        aoi_geometries, aoi_crs = _load_vector_geometries(aoi_path)
    tile_outputs: dict[str, list[Path]] = {name: [] for name in outputs_to_compute}
    metadata = {
        "input_dir": str(Path(raw_safe_dir)),
        "optical_safe_dir": str(Path(raw_safe_dir)),
        "radar_safe_dir": str(Path(radar_safe_dir)) if radar_safe_dir else "",
        "output_dir": str(output_root),
        "discovered_product_count": len(discovered),
        "processed_product_count": len(products),
        "selection": selection,
        "selected_outputs": outputs_to_write,
        "computed_outputs": outputs_to_compute,
        "processing_params": params,
        "aoi_path": str(aoi_path) if aoi_path else "",
        "process_scope_path": str(process_scope_path or process_scope_dir) if (process_scope_path or process_scope_dir) else "",
        "process_start_date": process_start_date or "",
        "process_end_date": process_end_date or "",
        "products": [],
        "notes": [
            "Processed locally from Sentinel-2 L2A SAFE folders; Google Earth Engine is not used.",
            "Moisture is estimated from Sentinel-2 NDWI/LSWI. Sentinel-1 input is recorded for future fusion, but local radar fusion is not enabled in this workflow.",
        ],
    }

    product_groups = group_products_by_tile(products)
    total_group_scenes = sum(len(group) for group in product_groups)
    metadata["composite_mode"] = "single_scene" if total_group_scenes == len(product_groups) else "median_by_tile"

    for product_index, group in enumerate(product_groups, start=1):
        if should_stop and should_stop():
            raise InterruptedError("Processing stopped by user.")
        product = group[-1]
        group_label = product.acquisition if len(group) == 1 else f"{group[0].acquisition}_{group[-1].acquisition}"
        product_dir = tile_root / f"{group_label}_{product.tile_id}"
        product_dir.mkdir(parents=True, exist_ok=True)
        outputs = _process_product_group(
            group,
            product_dir,
            outputs_to_compute,
            params,
            should_stop=should_stop,
            progress_callback=progress_callback,
            product_index=product_index,
            product_count=len(product_groups),
            aoi_geometries=aoi_geometries,
            aoi_crs=aoi_crs,
        )
        for name, path in outputs.items():
            tile_outputs[name].append(path)
        metadata["products"].append(
            {
                "safe": str(product.safe_dir),
                "safe_products": [str(item.safe_dir) for item in group],
                "tile_id": product.tile_id,
                "acquisition": product.acquisition if len(group) == 1 else "median_composite",
                "acquisitions": [item.acquisition for item in group],
                "composite": "single_scene" if len(group) == 1 else "median",
                "outputs": {name: str(path) for name, path in outputs.items()},
            }
        )

    metadata["mosaics"] = {
        name: str(_mosaic_outputs(paths, mosaic_root / f"{name}.tif"))
        for name, paths in tile_outputs.items()
    }
    if aoi_path:
        clipped_root = output_root / "clipped"
        clipped_root.mkdir(parents=True, exist_ok=True)
        metadata["clipped"] = {
            name: str(_clip_tif_to_geojson(Path(path), clipped_root / f"{name}.tif", aoi_path))
            for name, path in metadata["mosaics"].items()
        }
    metadata_path = output_root / "processing_metadata.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    metadata["metadata_path"] = str(metadata_path)
    return metadata


def _filter_products_by_date(products: list[SafeProduct], start_date: str | None, end_date: str | None) -> list[SafeProduct]:
    start = _parse_optional_date(start_date)
    end = _parse_optional_date(end_date)
    if not start and not end:
        return products
    filtered = []
    for product in products:
        acquisition_date = _product_date(product.acquisition)
        if not acquisition_date:
            continue
        if start and acquisition_date < start:
            continue
        if end and acquisition_date > end:
            continue
        filtered.append(product)
    return filtered


def _parse_optional_date(value: str | None):
    if not value:
        return None
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def _product_date(acquisition: str):
    match = re.match(r"(\d{8})", acquisition or "")
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%d").date()


def _process_product_group(
    products: list[SafeProduct],
    output_dir: Path,
    output_names: list[str],
    params: dict,
    should_stop: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict], None] | None = None,
    product_index: int = 1,
    product_count: int = 1,
    aoi_geometries: list[dict] | None = None,
    aoi_crs: str | CRS | None = None,
) -> dict[str, Path]:
    product = products[-1]
    output_paths = {name: output_dir / f"{name}.tif" for name in output_names}
    with rasterio.open(product.bands["B02"]) as ref:
        src_aoi_geometries = None
        if aoi_geometries and aoi_crs:
            src_aoi_geometries = [transform_geom(aoi_crs, ref.crs, geometry) for geometry in aoi_geometries]
        byte_profile = ref.profile.copy()
        byte_profile.update(driver="GTiff", dtype="uint8", count=1, nodata=NODATA_VALUE, compress="deflate", tiled=True)
        float_profile = ref.profile.copy()
        float_profile.update(driver="GTiff", dtype="float32", count=1, nodata=FLOAT_NODATA_VALUE, compress="deflate", tiled=True)
        rgb_profile = ref.profile.copy()
        rgb_profile.update(driver="GTiff", dtype="uint8", count=3, nodata=None, compress="deflate", tiled=True)

        datasets_by_product = [{band: rasterio.open(path) for band, path in item.bands.items()} for item in products]
        try:
            vrts_by_product = []
            for datasets in datasets_by_product:
                vrts_by_product.append(
                    {
                        band: WarpedVRT(
                            ds,
                            crs=ref.crs,
                            transform=ref.transform,
                            width=ref.width,
                            height=ref.height,
                            resampling=Resampling.nearest if band == "SCL" else Resampling.bilinear,
                        )
                        for band, ds in datasets.items()
                    }
                )
            writers = {}
            try:
                for name, path in output_paths.items():
                    profile = rgb_profile if name in RGB_OUTPUT_NAMES else (
                        float_profile if name in {"vitality_index_ndvi", "ndvi_last_year_delta", "crop_yield_estimate", "ndre_index"} else byte_profile
                    )
                    writers[name] = rasterio.open(
                        path,
                        "w",
                        **profile,
                    )
                windows = list(ref.block_windows(1))
                total_windows = max(1, len(windows))
                for window_index, (_, window) in enumerate(windows, start=1):
                    if should_stop and should_stop():
                        raise InterruptedError("Processing stopped by user.")
                    bands, scl = _read_group_composite(vrts_by_product, window)
                    outputs = _compute_outputs(bands, scl, params)
                    if src_aoi_geometries:
                        inside = geometry_mask(
                            src_aoi_geometries,
                            out_shape=(int(window.height), int(window.width)),
                            transform=window_transform(window, ref.transform),
                            invert=True,
                        )
                        for name, data in outputs.items():
                            if data.ndim == 3:
                                data[:, ~inside] = 0
                            else:
                                nodata = FLOAT_NODATA_VALUE if data.dtype.kind == "f" else NODATA_VALUE
                                data[~inside] = nodata
                    for name in output_names:
                        data = outputs[name]
                        if data.ndim == 3:
                            writers[name].write(data, window=window)
                        else:
                            writers[name].write(data, 1, window=window)
                    if progress_callback:
                        done_units = (product_index - 1) * total_windows + window_index
                        total_units = max(1, product_count * total_windows)
                        progress_callback(
                            {
                                "percent": int(done_units * 100 / total_units),
                                "product_index": product_index,
                                "product_count": product_count,
                                "window_index": window_index,
                                "window_count": total_windows,
                                "outputs": output_names,
                                "acquisition": product.acquisition if len(products) == 1 else "median_composite",
                                "tile_id": product.tile_id,
                            }
                        )
            finally:
                for writer in writers.values():
                    writer.close()
        finally:
            for vrts in locals().get("vrts_by_product", []):
                for vrt in vrts.values():
                    vrt.close()
            for datasets in datasets_by_product:
                for dataset in datasets.values():
                    dataset.close()
    return output_paths


def _read_group_composite(vrts_by_product: list[dict], window: Window) -> tuple[dict[str, np.ndarray], np.ndarray]:
    if len(vrts_by_product) == 1:
        vrts = vrts_by_product[0]
        bands = {band: _read_reflectance(vrt, window) for band, vrt in vrts.items() if band != "SCL"}
        scl = vrts["SCL"].read(1, window=window)
        return bands, scl

    band_names = [band for band in vrts_by_product[0] if band != "SCL"]
    bands = {}
    for band in band_names:
        stack = np.stack([_read_reflectance(vrts[band], window) for vrts in vrts_by_product], axis=0)
        with np.errstate(all="ignore"):
            bands[band] = np.nanmedian(stack, axis=0).astype("float32")

    scl_stack = np.stack([vrts["SCL"].read(1, window=window) for vrts in vrts_by_product], axis=0)
    scl = _mode_uint8(scl_stack)
    return bands, scl


def _mode_uint8(stack: np.ndarray) -> np.ndarray:
    values = stack.astype("uint8")
    flat = values.reshape(values.shape[0], -1)
    result = np.empty(flat.shape[1], dtype="uint8")
    for idx in range(flat.shape[1]):
        counts = np.bincount(flat[:, idx], minlength=256)
        result[idx] = int(np.argmax(counts))
    return result.reshape(values.shape[1], values.shape[2])


def _read_reflectance(dataset, window: Window) -> np.ndarray:
    data = dataset.read(1, window=window).astype("float32")
    data[data <= 0] = np.nan
    return data * 0.0001


def _normalized_difference(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        result = (a - b) / (a + b)
    result[~np.isfinite(result)] = np.nan
    return result.astype("float32")


def _enhanced_vegetation_index(nir: np.ndarray, red: np.ndarray, blue: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        result = 2.5 * (nir - red) / (nir + 6.0 * red - 7.5 * blue + 1.0)
    result[~np.isfinite(result)] = np.nan
    return result.astype("float32")


def _stretched_rgb(
    channels: list[np.ndarray],
    valid: np.ndarray,
    reflectance_max: float,
    reflectance_min: float = 0.02,
    gamma: float = 1.1,
) -> np.ndarray:
    rgb = np.stack(channels, axis=0).astype("float32")
    rgb = np.clip((rgb - reflectance_min) / max(reflectance_max - reflectance_min, 1e-6), 0, 1)
    rgb = np.power(rgb, gamma)
    rgb = (rgb * 255).astype("uint8")
    rgb[:, valid] = np.maximum(rgb[:, valid], 1)
    rgb[:, ~valid] = 0
    return rgb


def _merged_processing_params(overrides: dict) -> dict:
    defaults = {
        "vitality_poor_ndvi": THRESHOLDS.vitality_poor_ndvi,
        "vitality_weak_ndvi": THRESHOLDS.vitality_weak_ndvi,
        "vitality_normal_ndvi": THRESHOLDS.vitality_normal_ndvi,
        "vitality_decline_delta": THRESHOLDS.vitality_decline_delta,
        "dry_ndwi": THRESHOLDS.dry_ndwi,
        "wet_ndwi": THRESHOLDS.wet_ndwi,
        "water_ndwi": THRESHOLDS.water_ndwi,
        "water_lswi": THRESHOLDS.water_lswi,
        "fallow_ndvi_max": THRESHOLDS.fallow_ndvi_max,
        "fallow_ndvi_amp": THRESHOLDS.fallow_ndvi_amp,
        "fallow_ndbi_min": 0.05,
        "nonagri_ndbi_rise": THRESHOLDS.nonagri_ndbi_rise,
        "nonagri_ndvi_drop": THRESHOLDS.nonagri_ndvi_drop,
        "nonagri_ndvi_max": 0.30,
        "reflectance_min": None,
        "reflectance_max": None,
        "scl_excluded_classes": "0,1,3,9,11",
        "drought_ndwi_max": THRESHOLDS.dry_ndwi,
        "drought_ndvi_max": 0.45,
        "waterlogging_ndwi_min": THRESHOLDS.water_ndwi,
        "waterlogging_lswi_min": THRESHOLDS.water_lswi,
        "dry_vv_db": THRESHOLDS.dry_vv_db,
        "water_vv_db": THRESHOLDS.water_vv_db,
        "min_valid_ratio": None,
        "output_resolution_m": 10,
        "resampling_method": "bilinear",
        "drought_requires_weak": True,
        "export_masks": True,
        "crop_water_ndvi_max": 0.28,
        "crop_water_ndwi_min": 0.10,
        "crop_water_lswi_min": 0.06,
        "crop_built_ndvi_max": 0.18,
        "crop_built_ndbi_min": 0.06,
        "crop_rice_ndvi_min": 0.45,
        "crop_rice_lswi_min": 0.10,
        "crop_rice_ndre_min": 0.16,
        "crop_winter_ndvi_min": 0.28,
        "crop_winter_ndvi_max": 0.75,
        "crop_winter_ndre_min": 0.08,
        "crop_winter_ndre_max": 0.22,
        "crop_other_ndvi_min": 0.22,
        "yield_min_t_ha": 3.0,
        "yield_max_t_ha": 7.0,
        "yield_rice_bonus": 0.35,
        "yield_winter_bonus": 0.15,
        "yield_other_bonus": 0.05,
        "patrol_lai_scale": 6.0,
        "truecolor_reflectance_min": None,
        "truecolor_reflectance_max": None,
        "truecolor_gamma": 0.10,
    }
    for key, value in overrides.items():
        if key in defaults and value not in ("", None):
            defaults[key] = value
    return defaults


def _compute_outputs(bands: dict[str, np.ndarray], scl: np.ndarray, params: dict) -> dict[str, np.ndarray]:
    blue = bands["B02"]
    green = bands["B03"]
    red = bands["B04"]
    rededge1 = bands["B05"]
    nir = bands["B08"]
    swir1 = bands["B11"]
    swir2 = bands["B12"]
    band_stack = [blue, green, red, rededge1, nir, swir1, swir2]
    preview_valid = np.logical_and.reduce([np.isfinite(band) for band in [blue, green, red, nir]])
    valid_reflectance = np.logical_and.reduce([np.isfinite(band) for band in band_stack])
    if params.get("reflectance_min") not in ("", None):
        valid_reflectance &= np.logical_and.reduce([(band >= float(params["reflectance_min"])) for band in band_stack])
    if params.get("reflectance_max") not in ("", None):
        valid_reflectance &= np.logical_and.reduce([(band <= float(params["reflectance_max"])) for band in band_stack])
    valid = valid_reflectance.copy()
    valid &= ~np.isin(scl, _scl_excluded_classes(params.get("scl_excluded_classes")))

    ndvi = _normalized_difference(nir, red)
    ndwi = _normalized_difference(green, nir)
    lswi = _normalized_difference(nir, swir1)
    ndbi = _normalized_difference(swir1, nir)
    ndre = _normalized_difference(nir, rededge1)
    evi = _enhanced_vegetation_index(nir, red, blue)

    vitality = np.full(ndvi.shape, NODATA_VALUE, dtype="uint8")
    vitality[valid & (ndvi < params["vitality_poor_ndvi"])] = 1
    vitality[valid & (ndvi >= params["vitality_poor_ndvi"]) & (ndvi < params["vitality_weak_ndvi"])] = 2
    vitality[valid & (ndvi >= params["vitality_weak_ndvi"]) & (ndvi < params["vitality_normal_ndvi"])] = 3
    vitality[valid & (ndvi >= params["vitality_normal_ndvi"])] = 4

    moisture = np.full(ndwi.shape, NODATA_VALUE, dtype="uint8")
    moisture[valid & (ndwi < params["dry_ndwi"])] = 1
    moisture[valid & (ndwi >= params["dry_ndwi"]) & (ndwi < params["wet_ndwi"])] = 2
    moisture[valid & (ndwi >= params["wet_ndwi"])] = 3
    moisture[valid & ((ndwi > params["water_ndwi"]) | (lswi > params["water_lswi"]))] = 4

    weak = valid & (vitality > 0) & (vitality <= 2)
    drought_base = valid & (ndwi <= params["drought_ndwi_max"]) & (ndvi <= params["drought_ndvi_max"])
    drought = (weak & drought_base) if params["drought_requires_weak"] else drought_base
    waterlogging = valid & ((ndwi >= params["waterlogging_ndwi_min"]) | (lswi >= params["waterlogging_lswi_min"]))
    fallow = valid & (ndvi <= params["fallow_ndvi_max"]) & (ndbi >= params["fallow_ndbi_min"])
    nonagri = valid & (ndvi <= params["nonagri_ndvi_max"]) & (ndbi >= params["nonagri_ndbi_rise"])
    anomaly = np.zeros(ndvi.shape, dtype="uint8")
    anomaly[~valid] = NODATA_VALUE
    anomaly[weak] = 1
    anomaly[drought] = 3
    anomaly[waterlogging] = 4
    anomaly[fallow] = 5
    anomaly[nonagri] = 6

    lai_proxy = np.clip(ndvi * float(params["patrol_lai_scale"]), 0, 6)
    priority = np.full(ndvi.shape, NODATA_VALUE, dtype="uint8")
    priority[~valid] = NODATA_VALUE
    priority[valid & (lai_proxy <= 1.0)] = 1
    priority[valid & (lai_proxy > 1.0) & (lai_proxy <= 1.8)] = 2
    priority[valid & (lai_proxy > 1.8) & (lai_proxy <= 2.5)] = 3
    priority[valid & (lai_proxy > 2.5) & (lai_proxy <= 3.2)] = 4
    priority[valid & (lai_proxy > 3.2) & (lai_proxy <= 4.0)] = 5
    priority[valid & (lai_proxy > 4.0)] = 6

    ndvi_out = ndvi.astype("float32")
    ndvi_out[~valid] = FLOAT_NODATA_VALUE
    ndre_out = ndre.astype("float32")
    ndre_out[~valid] = FLOAT_NODATA_VALUE
    delta = np.full(ndvi.shape, FLOAT_NODATA_VALUE, dtype="float32")
    crop_type = _classify_crop_type(valid, ndvi, ndwi, lswi, ndbi, ndre, scl, params)
    crop_yield = _estimate_crop_yield(valid, crop_type, ndvi, ndre, lswi, evi, params)
    rgb_min = 0.02 if params.get("truecolor_reflectance_min") in ("", None) else float(params["truecolor_reflectance_min"])
    rgb_max = 0.40 if params.get("truecolor_reflectance_max") in ("", None) else float(params["truecolor_reflectance_max"])
    rgb_gamma = float(params["truecolor_gamma"])
    truecolor = _stretched_rgb([red, green, blue], preview_valid, rgb_max, rgb_min, rgb_gamma)
    falsecolor = _stretched_rgb([nir, red, green], preview_valid, rgb_max, rgb_min, rgb_gamma)
    truecolor_preview = _stretched_rgb([red, green, blue], preview_valid, rgb_max, rgb_min, rgb_gamma)
    falsecolor_preview = _stretched_rgb([nir, red, green], preview_valid, rgb_max, rgb_min, rgb_gamma)

    return {
        "vitality_grade": vitality,
        "moisture_grade": moisture,
        "anomaly_combined": anomaly,
        "patrol_priority": priority,
        "vitality_index_ndvi": ndvi_out,
        "ndre_index": ndre_out,
        "ndvi_last_year_delta": delta,
        "crop_type_class": crop_type,
        "crop_yield_estimate": crop_yield,
        "truecolor_rgb": truecolor,
        "falsecolor_rgb": falsecolor,
        "truecolor_preview_rgb": truecolor_preview,
        "falsecolor_preview_rgb": falsecolor_preview,
        "anomaly_weak_vitality": _mask(anomaly, 1, valid),
        "anomaly_vitality_decline": _placeholder_mask(valid),
        "anomaly_drought": _mask(anomaly, 3, valid),
        "anomaly_waterlogging": _mask(anomaly, 4, valid),
        "anomaly_fallow": _mask(anomaly, 5, valid),
        "anomaly_nonagri": _mask(anomaly, 6, valid),
    }


def _scl_excluded_classes(value) -> list[int]:
    if isinstance(value, str):
        return [int(part.strip()) for part in value.split(",") if part.strip().isdigit()]
    if isinstance(value, (list, tuple, set)):
        return [int(part) for part in value]
    return [0, 1, 3, 8, 9, 10, 11]


def _classify_crop_type(
    valid: np.ndarray,
    ndvi: np.ndarray,
    ndwi: np.ndarray,
    lswi: np.ndarray,
    ndbi: np.ndarray,
    ndre: np.ndarray,
    scl: np.ndarray,
    params: dict,
) -> np.ndarray:
    result = np.full(ndvi.shape, NODATA_VALUE, dtype="uint8")
    result[valid] = 2

    water = valid & (
        np.isin(scl, list(WATER_SCL_CLASSES))
        | (
            (
                (ndwi >= params["crop_water_ndwi_min"])
                | (lswi >= params["crop_water_lswi_min"])
                | ((ndwi + lswi) >= (params["crop_water_ndwi_min"] + params["crop_water_lswi_min"]))
            )
            & (ndvi <= params["crop_water_ndvi_max"])
        )
    )
    thin_cloud_like = np.isin(scl, [8, 10])
    built = valid & (ndvi <= params["crop_built_ndvi_max"]) & (ndbi >= params["crop_built_ndbi_min"]) & ~water & ~thin_cloud_like
    rice = valid & (ndvi >= params["crop_rice_ndvi_min"]) & (lswi >= params["crop_rice_lswi_min"]) & (ndre >= params["crop_rice_ndre_min"]) & ~water & ~built
    winter_crop = (
        valid
        & (ndvi >= params["crop_winter_ndvi_min"])
        & (ndvi <= params["crop_winter_ndvi_max"])
        & (ndre >= params["crop_winter_ndre_min"])
        & (ndre <= params["crop_winter_ndre_max"])
        & (lswi < params["crop_rice_lswi_min"])
        & ~water
        & ~built
        & ~rice
    )
    other_crop = valid & (ndvi >= params["crop_other_ndvi_min"]) & ~water & ~built & ~rice & ~winter_crop

    result[water] = 1
    result[built] = 2
    result[rice] = 3
    result[winter_crop] = 4
    result[other_crop] = 5
    return result


def _estimate_crop_yield(
    valid: np.ndarray,
    crop_type: np.ndarray,
    ndvi: np.ndarray,
    ndre: np.ndarray,
    lswi: np.ndarray,
    evi: np.ndarray,
    params: dict,
) -> np.ndarray:
    result = np.full(ndvi.shape, FLOAT_NODATA_VALUE, dtype="float32")
    crop_mask = valid & np.isin(crop_type, [3, 4, 5])
    if not np.any(crop_mask):
        return result

    ndvi_score = np.clip((ndvi - 0.20) / 0.60, 0, 1)
    ndre_score = np.clip((ndre - 0.05) / 0.30, 0, 1)
    lswi_score = np.clip((lswi + 0.10) / 0.45, 0, 1)
    evi_score = np.clip((evi - 0.10) / 0.70, 0, 1)
    vigor_score = 0.45 * ndvi_score + 0.20 * ndre_score + 0.20 * lswi_score + 0.15 * evi_score

    class_bonus = np.zeros(ndvi.shape, dtype="float32")
    class_bonus[crop_type == 3] = params["yield_rice_bonus"]
    class_bonus[crop_type == 4] = params["yield_winter_bonus"]
    class_bonus[crop_type == 5] = params["yield_other_bonus"]

    min_yield = float(params["yield_min_t_ha"])
    max_yield = float(params["yield_max_t_ha"])
    yield_value = min_yield + np.clip(vigor_score + class_bonus, 0, 1) * (max_yield - min_yield)
    result[crop_mask] = yield_value[crop_mask].astype("float32")
    return result


def _mask(anomaly: np.ndarray, code: int, valid: np.ndarray) -> np.ndarray:
    result = np.full(anomaly.shape, NODATA_VALUE, dtype="uint8")
    result[valid] = (anomaly[valid] == code).astype("uint8")
    return result


def _placeholder_mask(valid: np.ndarray) -> np.ndarray:
    result = np.full(valid.shape, NODATA_VALUE, dtype="uint8")
    result[valid] = 0
    return result


def _mosaic_outputs(paths: Iterable[Path], output_path: Path) -> Path:
    source_paths = list(paths)
    if len(source_paths) == 1:
        output_path.write_bytes(source_paths[0].read_bytes())
        return output_path
    datasets = [rasterio.open(path) for path in source_paths]
    try:
        mosaic, transform = merge(datasets, method="first")
        profile = datasets[0].profile.copy()
        profile.update(height=mosaic.shape[1], width=mosaic.shape[2], transform=transform, compress="deflate", tiled=True)
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(mosaic)
    finally:
        for dataset in datasets:
            dataset.close()
    return output_path


def _clip_tif_to_geojson(input_tif: Path, output_tif: Path, aoi_path: str | Path) -> Path:
    geometries, vector_crs = _load_vector_geometries(aoi_path)
    with rasterio.open(input_tif) as src:
        src_geometries = [transform_geom(vector_crs, src.crs, geometry) for geometry in geometries]
        out_image, out_transform = mask(src, src_geometries, crop=True, nodata=src.nodata)
        profile = src.profile.copy()
        profile.update(height=out_image.shape[1], width=out_image.shape[2], transform=out_transform, compress="deflate", tiled=True)
        with rasterio.open(output_tif, "w", **profile) as dst:
            dst.write(out_image)
    return output_tif


def _load_vector_geometries(aoi_path: str | Path) -> tuple[list[dict], str | CRS]:
    path = Path(aoi_path)
    if path.suffix.lower() == ".shp":
        return _load_shp_geometries(path)

    if path.suffix.lower() not in {".geojson", ".json"}:
        raise ValueError("AOI clipping supports GeoJSON/JSON or local SHP with sidecar files.")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        return [feature["geometry"] for feature in data.get("features", []) if feature.get("geometry")], "EPSG:4326"
    if data.get("type") == "Feature":
        return [data["geometry"]], "EPSG:4326"
    if data.get("type") in {"Polygon", "MultiPolygon"}:
        return [data], "EPSG:4326"
    raise ValueError(f"Unsupported AOI GeoJSON: {path}")


def _load_shp_geometries(path: Path) -> tuple[list[dict], str | CRS]:
    try:
        import fiona

        geometries = []
        with fiona.open(path) as src:
            vector_crs = src.crs_wkt or CRS.from_user_input(src.crs or "EPSG:4326")
            for feature in src:
                geometry = feature.get("geometry")
                if geometry:
                    geometries.append(dict(geometry))
        if not geometries:
            raise ValueError(f"No geometry found in AOI shapefile: {path}")
        return geometries, vector_crs
    except ImportError:
        return _load_shp_geometries_pyshp(path)


def _load_shp_geometries_pyshp(path: Path) -> tuple[list[dict], str | CRS]:
    try:
        import shapefile
    except ImportError as exc:
        raise ValueError("SHP AOI requires fiona or pyshp. Install one of them or convert SHP to GeoJSON.") from exc

    prj = path.with_suffix(".prj")
    vector_crs = prj.read_text(encoding="utf-8", errors="ignore") if prj.exists() else "EPSG:4326"
    reader = shapefile.Reader(str(path))
    geometries = []
    for shape in reader.shapes():
        geo = shape.__geo_interface__
        if geo.get("type") in {"Polygon", "MultiPolygon"}:
            geometries.append(geo)
    if not geometries:
        raise ValueError(f"No polygon geometry found in AOI shapefile: {path}")
    return geometries, vector_crs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=LOCAL_SAFE_DIR)
    parser.add_argument("--output", default=LOCAL_OUTPUT_DIR)
    parser.add_argument("--acquisition")
    parser.add_argument("--all-products", action="store_true")
    args = parser.parse_args(argv)
    metadata = run_local_processing(args.input, args.output, args.acquisition, args.all_products)
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
