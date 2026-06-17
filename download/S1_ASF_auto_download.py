"""
Sentinel-1 数据自动下载脚本，数据源为 Alaska Satellite Facility
时间：2026/05/21
版本：V1

"""
import argparse
import base64
import json
import os
import webbrowser
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from tqdm import tqdm

try:
    import asf_search as asf
except ImportError:
    asf = None


ASF_SEARCH_URL = "https://api.daac.asf.alaska.edu/services/search/param"
SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent
load_dotenv(APP_DIR / ".env")
load_dotenv(SCRIPT_DIR / ".env")

# =========================
# User Config (edit here)
# =========================
CFG_START_DATE = "2025-01-01"
CFG_END_DATE = "2025-12-30"
CFG_AOI_GEOJSON = ""
CFG_BEAM_MODE = "IW"
CFG_PROCESSING_LEVEL = "GRD_HD"
CFG_POLARIZATION = "VV+VH"
CFG_PLATFORM = "Sentinel-1A,Sentinel-1B,Sentinel-1C,Sentinel-1D"
CFG_MAX_RESULTS = 100

CFG_EARTHDATA_USERNAME = ""
CFG_EARTHDATA_PASSWORD = ""


CFG_OUTPUT_DIR = str(APP_DIR / "data" / "S1" / "DANGYANG" / "raw_zip")
CFG_EXTRACT_DIR = str(APP_DIR / "data" / "S1" / "DANGYANG" / "raw_safe")

CFG_EARTHDATA_TOKEN = ""

def parse_args():
    parser = argparse.ArgumentParser(description="Search and download Sentinel-1 raw zip from ASF and auto-extract.")
    parser.add_argument("--start-date", default=CFG_START_DATE, help="Start date, format YYYY-MM-DD.")
    parser.add_argument("--end-date", default=CFG_END_DATE, help="End date, format YYYY-MM-DD.")
    parser.add_argument("--aoi-geojson", default=CFG_AOI_GEOJSON, help="AOI GeoJSON absolute path. Set empty to disable.")
    parser.add_argument("--no-aoi", action="store_true", help="Disable AOI filter even if CFG_AOI_GEOJSON is set.")
    parser.add_argument("--beam-mode", default=CFG_BEAM_MODE, help="Sentinel-1 beam mode.")
    parser.add_argument("--processing-level", default=CFG_PROCESSING_LEVEL, help="ASF processing level.")
    parser.add_argument("--polarization", default=CFG_POLARIZATION, help="ASF polarization filter.")
    parser.add_argument("--platform", default=CFG_PLATFORM, help="Platform filter, e.g. Sentinel-1A,Sentinel-1B.")
    parser.add_argument("--max-results", type=int, default=CFG_MAX_RESULTS, help="Maximum results to search.")
    parser.add_argument("--username", default=CFG_EARTHDATA_USERNAME or os.getenv("EARTHDATA_USERNAME", ""), help="Earthdata username.")
    parser.add_argument("--password", default=CFG_EARTHDATA_PASSWORD or os.getenv("EARTHDATA_PASSWORD", ""), help="Earthdata password.")
    parser.add_argument("--token", default=CFG_EARTHDATA_TOKEN or os.getenv("EARTHDATA_TOKEN", ""), help="Earthdata bearer token. Preferred over username/password.")
    parser.add_argument("--output-dir", default=CFG_OUTPUT_DIR, help="Zip output absolute directory.")
    parser.add_argument("--extract-dir", default=CFG_EXTRACT_DIR, help="Extract output absolute directory.")
    parser.add_argument("--open-login-on-401", action="store_true", default=True, help="Open browser auth pages automatically when HTTP 401 occurs.")
    parser.add_argument("--no-open-login-on-401", dest="open_login_on_401", action="store_false", help="Do not open browser auth pages on HTTP 401.")
    parser.add_argument("--no-extract", action="store_true", help="Only download zip, do not extract.")
    parser.add_argument("--search-only", action="store_true", help="Only search and print results.")
    return parser.parse_args()


def load_aoi(aoi_geojson):
    if not aoi_geojson:
        return None
    path = Path(aoi_geojson)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.exists():
        raise FileNotFoundError(f"AOI file not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
        if not features:
            raise ValueError(f"AOI has no Feature: {path}")
        return features[0]["geometry"]
    if data.get("type") == "Feature":
        return data["geometry"]
    return data


def geometry_to_wkt(geometry):
    geom_type = geometry.get("type")
    coords = geometry.get("coordinates", [])
    if geom_type == "Polygon":
        ring = coords[0]
        return "POLYGON((" + ",".join(f"{pt[0]} {pt[1]}" for pt in ring) + "))"
    if geom_type == "MultiPolygon":
        parts = []
        for poly in coords:
            ring = poly[0]
            parts.append("((" + ",".join(f"{pt[0]} {pt[1]}" for pt in ring) + "))")
        return "MULTIPOLYGON(" + ",".join(parts) + ")"
    raise ValueError(f"Unsupported AOI geometry type for ASF intersectsWith: {geom_type}")


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=3, max=10),
    retry=retry_if_exception_type((requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.RequestException)),
    reraise=True,
)
def search_asf(params):
    response = requests.get(ASF_SEARCH_URL, params=params, timeout=120)
    if response.status_code != 200:
        raise requests.exceptions.HTTPError(
            f"ASF search failed: {response.status_code} | {response.text[:1000]}",
            response=response,
        )
    return response.json()


def build_search_params(args):
    params = {
        "dataset": "SENTINEL-1",
        "beamMode": args.beam_mode,
        "processingLevel": args.processing_level,
        "polarization": args.polarization,
        "platform": args.platform,
        "start": f"{args.start_date}T00:00:00Z",
        "end": f"{args.end_date}T23:59:59Z",
        "maxResults": str(args.max_results),
        "output": "geojson",
    }
    geometry = None if args.no_aoi else load_aoi(args.aoi_geojson)
    if geometry:
        params["intersectsWith"] = geometry_to_wkt(geometry)
    return params


def feature_bounds(geometry):
    if not geometry:
        return None
    coords = []

    def collect(value):
        if not value:
            return
        if isinstance(value[0], (int, float)):
            coords.append(value)
            return
        for item in value:
            collect(item)

    collect(geometry.get("coordinates", []))
    if not coords:
        return None
    xs = [xy[0] for xy in coords]
    ys = [xy[1] for xy in coords]
    return [[min(ys), min(xs)], [max(ys), max(xs)]]


def emit_search_results(features):
    items = []
    for feature in features:
        prop = feature.get("properties", {})
        scene = prop.get("sceneName") or prop.get("fileID") or prop.get("granuleName")
        geometry = feature.get("geometry")
        items.append(
            {
                "id": scene,
                "title": scene,
                "datetime": prop.get("startTime"),
                "collection": "SENTINEL-1",
                "processing_level": prop.get("processingLevel"),
                "polarization": prop.get("polarization"),
                "geometry": geometry,
                "bounds": feature_bounds(geometry),
                "preview": prop.get("browse") or prop.get("thumbnail") or "",
            }
        )
    print("__SEARCH_RESULTS_JSON__" + json.dumps({"items": items}, ensure_ascii=False))


def extract_zip(zip_path, extract_dir):
    zip_path = Path(zip_path)
    extract_dir = Path(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)
    safe_dir = extract_dir / f"{zip_path.stem}.SAFE"
    if safe_dir.exists():
        print(f"[SKIP-EXTRACT] {safe_dir}")
        return True
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
        print(f"[EXTRACTED] {zip_path.name}")
        return True
    except zipfile.BadZipFile:
        print(f"[EXTRACT-FAILED] bad zip: {zip_path}")
        return False
    except OSError as exc:
        print(f"[EXTRACT-FAILED] {zip_path}: {exc}")
        return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=5, max=20),
    retry=retry_if_exception_type((requests.exceptions.Timeout, requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError, requests.exceptions.RequestException)),
    reraise=True,
)
def download_file(url, save_path, username, password, token=""):
    if asf is None:
        raise RuntimeError("Missing dependency: run `python -m pip install asf_search` first.")

    save_path = Path(save_path)
    part_path = save_path.with_suffix(save_path.suffix + ".part")
    if part_path.exists():
        part_path.unlink()

    with asf.ASFSession() as session:
        session.headers.update({"User-Agent": "S1-ASF-Auto-Downloader/1.0"})
        if token:
            session.auth_with_token(token)
        else:
            session.auth_with_creds(username, password)
        with session.get(url, stream=True, timeout=300, allow_redirects=True) as response:
            response.raise_for_status()
            total_size = int(response.headers.get("content-length", 0))
            with part_path.open("wb") as f:
                with tqdm(
                    total=total_size if total_size > 0 else None,
                    unit="B",
                    unit_scale=True,
                    unit_divisor=1024,
                    desc=save_path.name,
                    leave=True,
                ) as pbar:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            pbar.update(len(chunk))
    part_path.replace(save_path)
    return True


def open_auth_pages_once(auth_state, oauth_url=None):
    if auth_state.get("opened"):
        return False
    urls = []
    if oauth_url and "urs.earthdata.nasa.gov/oauth/authorize" in oauth_url:
        urls.append(oauth_url)
    urls.extend(
        [
            "https://urs.earthdata.nasa.gov/",
            "https://sentinel1.asf.alaska.edu/",
        ]
    )
    for u in urls:
        webbrowser.open(u)
    auth_state["opened"] = True
    for u in urls:
        print(f"[OPEN-BROWSER] {u}")
    return True


def extract_oauth_url(exc):
    response = getattr(exc, "response", None)
    if response is None:
        return None
    url = getattr(response, "url", None)
    if url and "urs.earthdata.nasa.gov/oauth/authorize" in url:
        return url
    return None


def wait_for_manual_authorization_once(auth_state):
    if auth_state.get("waited"):
        return
    input("[ACTION] If an Earthdata/ASF auth page opened, finish authorization in the browser, then press Enter to retry this file...")
    auth_state["waited"] = True


def _decode_jwt_payload(token):
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid JWT format")
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(payload + padding)
    return json.loads(decoded.decode("utf-8"))


def check_token_expiry_or_raise(token):
    if not token:
        return
    try:
        payload = _decode_jwt_payload(token)
    except Exception as exc:
        raise RuntimeError(f"Invalid EARTHDATA token: {exc}") from exc

    exp = payload.get("exp")
    if exp is None:
        raise RuntimeError("Invalid EARTHDATA token: missing 'exp' claim")
    if not isinstance(exp, (int, float)):
        raise RuntimeError("Invalid EARTHDATA token: 'exp' is not numeric")

    now_ts = datetime.now(timezone.utc).timestamp()
    exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc)
    if now_ts >= float(exp):
        raise RuntimeError(
            f"EARTHDATA token expired at {exp_dt.isoformat()}. Please generate a new token and update CFG_EARTHDATA_TOKEN or EARTHDATA_TOKEN."
        )


def main():
    args = parse_args()
    auth_state = {"opened": False, "waited": False}

    if not args.search_only and (not args.token) and (not args.username or not args.password):
        raise RuntimeError("Missing Earthdata auth. Set --token (recommended) or --username/--password")
    if args.token:
        check_token_expiry_or_raise(args.token)

    params = build_search_params(args)
    data = search_asf(params)
    features = data.get("features", [])

    print(f"[ASF检索] 检索到 {len(features)} 景 Sentinel-1 数据")
    if len(features) < args.max_results:
        print(f"[ASF检索] 实际检索到的数据总数：{len(features)} 景")
    for i, item in enumerate(features[:5], 1):
        prop = item.get("properties", {})
        print(f"  {i}. {prop.get('sceneName')} | {prop.get('processingLevel')} | {prop.get('polarization')} | {prop.get('startTime')}")

    if args.search_only:
        emit_search_results(features)
        return

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    success = 0
    failed = 0

    for item in features:
        prop = item.get("properties", {})
        url = prop.get("url")
        scene = prop.get("sceneName")
        if not url or not scene:
            continue

        zip_path = output_dir / f"{scene}.zip"
        if zip_path.exists():
            print(f"[SKIP] {zip_path.name}")
            if not args.no_extract:
                extract_zip(zip_path, args.extract_dir)
            success += 1
            continue

        while True:
            try:
                print(f"[DOWNLOADING] {scene}")
                download_file(url, zip_path, args.username, args.password, args.token)
                if not args.no_extract and not extract_zip(zip_path, args.extract_dir):
                    failed += 1
                    break
                success += 1
                break
            except Exception as exc:
                if "401" in str(exc):
                    print(f"[FAILED] {scene}: {exc}")
                    print("[HINT] Earthdata/ASF script authentication failed.")
                    print("[HINT] Browser Log Out only means the browser is logged in; it does not authenticate this Python download session.")
                    print("[HINT] The script now uses ASFSession. If 401 continues, regenerate CFG_EARTHDATA_TOKEN or use valid username/password.")
                    if args.open_login_on_401:
                        oauth_url = extract_oauth_url(exc)
                        opened = open_auth_pages_once(auth_state, oauth_url=oauth_url)
                        if not opened:
                            print("[HINT] Auth page was already opened once in this run; not opening it again.")
                    wait_for_manual_authorization_once(auth_state)
                    continue
                print(f"[FAILED] {scene}: {exc}")
                failed += 1
                break

    print("=" * 60)
    print(f"[SUMMARY] success={success} failed={failed} total={len(features)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
