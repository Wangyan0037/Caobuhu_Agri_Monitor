from __future__ import annotations

import argparse
import json
import logging
import netrc
import os
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeRemainingColumn, TransferSpeedColumn
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

try:
    import shapefile
except ImportError:
    shapefile = None

try:
    from rasterio.warp import transform_geom
except ImportError:
    transform_geom = None


SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent
load_dotenv(APP_DIR / ".env")
load_dotenv(SCRIPT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(SCRIPT_DIR / "download.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)
console = Console()
DOWNLOAD_PROGRESS_MARKER = "__DOWNLOAD_PROGRESS_JSON__"


DEFAULT_START_DATE = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
DEFAULT_END_DATE = datetime.now(timezone.utc).date().isoformat()
DEFAULT_OUTPUT_DIR = APP_DIR / "data" / "raw_zip"
DEFAULT_EXTRACT_DIR = APP_DIR / "data" / "raw_safe"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search and download Sentinel-2 products from Copernicus Data Space.")
    parser.add_argument("--start-date", default=os.getenv("CDSE_START_DATE", DEFAULT_START_DATE))
    parser.add_argument("--end-date", default=os.getenv("CDSE_END_DATE", DEFAULT_END_DATE))
    parser.add_argument("--collection", default=os.getenv("CDSE_COLLECTION", "sentinel-2-l2a"), choices=["sentinel-2-l1c", "sentinel-2-l2a"])
    parser.add_argument("--contains", default=os.getenv("CDSE_CONTAINS", ""))
    parser.add_argument("--product-ids", default=os.getenv("CDSE_PRODUCT_IDS", ""), help="Comma/space separated product IDs to download from the search result.")
    parser.add_argument("--cloud-max", type=float, default=None, help="Maximum eo:cloud_cover percent, e.g. 30.")
    parser.add_argument("--aoi", default=os.getenv("CDSE_AOI", ""))
    parser.add_argument("--aoi-geojson", dest="aoi")
    parser.add_argument("--aoi-simplify", type=float, default=float(os.getenv("CDSE_AOI_SIMPLIFY", "0.0001")))
    parser.add_argument("--limit", type=int, default=int(os.getenv("CDSE_LIMIT", "100")))
    parser.add_argument("--output-dir", default=os.getenv("CDSE_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))
    parser.add_argument("--extract-dir", default=os.getenv("CDSE_EXTRACT_DIR", str(DEFAULT_EXTRACT_DIR)))
    parser.add_argument("--no-extract", action="store_true")
    parser.add_argument("--max-workers", type=int, default=int(os.getenv("CDSE_MAX_WORKERS", "2")))
    parser.add_argument("--search-only", action="store_true")
    return parser.parse_args()


def collection_from_product(product_type: str) -> str:
    product_type = (product_type or "").lower()
    if product_type in {"sentinel-2-l1c", "sentinel-2-l2a"}:
        return product_type
    return "sentinel-2-l2a"


def parse_contains_keywords(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").replace(",", " ").split() if part.strip()]


def parse_product_ids(value: str) -> set[str]:
    return {part.strip() for part in str(value or "").replace(",", " ").split() if part.strip()}


def emit_download_progress(product_id: str, percent: int, status: str) -> None:
    payload = {
        "id": product_id,
        "percent": max(0, min(100, int(percent))),
        "status": status,
    }
    print(DOWNLOAD_PROGRESS_MARKER + json.dumps(payload, ensure_ascii=False), flush=True)


def filter_by_cloud(features: list[dict], cloud_max: float | None) -> list[dict]:
    if cloud_max is None:
        return features
    filtered = []
    for feature in features:
        cloud = (feature.get("properties") or {}).get("eo:cloud_cover")
        if cloud is None:
            filtered.append(feature)
            continue
        try:
            if float(cloud) < float(cloud_max):
                filtered.append(feature)
        except (TypeError, ValueError):
            filtered.append(feature)
    return filtered


def feature_bounds(geometry: dict | None) -> list[list[float]] | None:
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


def emit_search_results(features: list[dict], product_items: list[dict]) -> None:
    by_id = {item.get("id"): item for item in product_items}
    items = []
    for feature in features:
        product_id = feature.get("id")
        if product_id not in by_id:
            continue
        props = feature.get("properties") or {}
        geometry = feature.get("geometry")
        assets = feature.get("assets") or {}
        thumbnail = ""
        for asset in assets.values():
            roles = asset.get("roles") or []
            href = asset.get("href") or ""
            if "thumbnail" in roles or "overview" in roles or "thumbnail" in href.lower():
                thumbnail = href
                break
        items.append(
            {
                "id": product_id,
                "title": product_id,
                "datetime": props.get("datetime"),
                "cloud_cover": props.get("eo:cloud_cover"),
                "collection": props.get("collection") or props.get("platform"),
                "geometry": geometry,
                "bounds": feature_bounds(geometry),
                "preview": thumbnail,
            }
        )
    print("__SEARCH_RESULTS_JSON__" + json.dumps({"items": items}, ensure_ascii=False))


class CDSEClient:
    IDENTITY_HOST = "identity.dataspace.copernicus.eu"

    def __init__(self, username: str | None = None, password: str | None = None):
        netrc_username, netrc_password = self._read_netrc_credentials()
        self.username = username or os.getenv("CDSE_USERNAME") or netrc_username
        self.password = password or os.getenv("CDSE_PASSWORD") or netrc_password
        self.token = None
        self.base_url = f"https://{self.IDENTITY_HOST}/auth/realms/CDSE"
        self._token_lock = threading.Lock()
        if not self.username or not self.password:
            raise RuntimeError(
                "缺少 Copernicus Data Space 账号密码。请在 Caobuhu_Agri_Monitor\\.env 中设置 CDSE_USERNAME/CDSE_PASSWORD，"
                "或在用户目录 _netrc/.netrc 中配置 identity.dataspace.copernicus.eu。"
            )

    @classmethod
    def _read_netrc_credentials(cls) -> tuple[str | None, str | None]:
        for path in (Path.home() / "_netrc", Path.home() / ".netrc"):
            if not path.exists():
                continue
            try:
                auth = netrc.netrc(str(path)).authenticators(cls.IDENTITY_HOST)
            except (netrc.NetrcParseError, OSError):
                continue
            if auth:
                login, _, password = auth
                return login, password
        return None, None

    def login(self) -> bool:
        with self._token_lock:
            token_url = f"{self.base_url}/protocol/openid-connect/token"
            response = requests.post(
                token_url,
                data={
                    "grant_type": "password",
                    "username": self.username,
                    "password": self.password,
                    "client_id": "cdse-public",
                },
                timeout=30,
            )
            if response.status_code == 200:
                self.token = response.json()["access_token"]
                console.print("[green]Copernicus 登录成功[/green]")
                return True
            console.print(f"[red]Copernicus 登录失败：{response.status_code} {response.text}[/red]")
            return False

    def _auth_headers(self, extra_headers: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"}
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _request_with_token_refresh(self, method: str, url: str, **kwargs) -> requests.Response:
        headers = kwargs.pop("headers", None)
        response = requests.request(method, url, headers=self._auth_headers(headers), **kwargs)
        if response.status_code == 401 and self.login():
            response = requests.request(method, url, headers=self._auth_headers(headers), **kwargs)
        return response

    def search_products(
        self,
        collection: str,
        start_date: str,
        end_date: str,
        limit: int,
        aoi_path: str | None,
        aoi_simplify: float,
    ) -> list[dict]:
        payload = {
            "collections": [collection],
            "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
            "limit": limit,
            "sortby": [{"field": "properties.datetime", "direction": "desc"}],
        }

        intersects = load_intersects(aoi_path, aoi_simplify)
        if intersects:
            payload["intersects"] = intersects

        console.print(f"[cyan]检索 {collection}: {start_date} 至 {end_date}[/cyan]")
        response = self._request_with_token_refresh(
            "POST",
            "https://stac.dataspace.copernicus.eu/v1/search",
            json=payload,
            timeout=180,
        )
        if response.status_code != 200:
            console.print(f"[red]检索失败：{response.status_code} {response.text}[/red]")
            return []
        try:
            payload = response.json()
        except requests.exceptions.JSONDecodeError:
            snippet = (response.text or "").strip()[:600]
            content_type = response.headers.get("Content-Type", "")
            console.print(f"[red]检索失败：Copernicus STAC 返回非 JSON 响应。HTTP {response.status_code} Content-Type={content_type}[/red]")
            if snippet:
                console.print(f"[red]{snippet}[/red]")
            return []
        features = payload.get("features", [])
        console.print(f"[green]检索到 {len(features)} 个产品[/green]")
        if len(features) < limit:
            console.print(f"[green]实际检索到的数据总数：{len(features)} 个产品[/green]")
        return features

    @staticmethod
    def zip_download_url(item: dict) -> str | None:
        for asset in item.get("assets", {}).values():
            href = asset.get("alternate", {}).get("https", {}).get("href", "")
            match = re.search(r"Products\(([^)]+)\)", href)
            if match:
                return f"https://zipper.dataspace.copernicus.eu/odata/v1/Products({match.group(1)})/$value"
        return None

    def remote_size(self, download_url: str) -> int | None:
        try:
            response = self._request_with_token_refresh("HEAD", download_url, allow_redirects=True, timeout=60)
            if response.status_code in (200, 206):
                length = response.headers.get("Content-Length")
                return int(length) if length else None
        except Exception:
            return None
        return None

    @staticmethod
    def is_valid_zip(zip_path: Path) -> bool:
        try:
            with zipfile.ZipFile(zip_path) as zip_obj:
                return zip_obj.testzip() is None
        except (zipfile.BadZipFile, OSError):
            return False

    @staticmethod
    def extract_safe_zip(zip_path: Path, extract_dir: Path) -> bool:
        extract_dir.mkdir(parents=True, exist_ok=True)
        expected_safe_dir = extract_dir / f"{zip_path.stem}.SAFE"
        if expected_safe_dir.exists():
            console.print(f"[green]已存在解压目录，跳过：{expected_safe_dir}[/green]")
            return True
        try:
            with zipfile.ZipFile(zip_path) as zip_obj:
                zip_obj.extractall(extract_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            console.print(f"[red]解压失败：{zip_path} {exc}[/red]")
            return False
        return True

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.RequestException,
        )),
        reraise=True,
    )
    def download_product(self, product_id: str, download_url: str, output_dir: Path, extract_dir: Path | None) -> bool:
        emit_download_progress(product_id, 1, "准备下载")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{product_id}.zip"
        part_path = output_dir / f"{product_id}.zip.part"
        expected_size = self.remote_size(download_url)

        if output_path.exists():
            size_ok = expected_size is None or output_path.stat().st_size == expected_size
            if size_ok and self.is_valid_zip(output_path):
                if extract_dir:
                    ok = self.extract_safe_zip(output_path, extract_dir)
                    emit_download_progress(product_id, 100 if ok else 0, "已存在/已解压" if ok else "解压失败")
                    return ok
                emit_download_progress(product_id, 100, "已存在")
                return True
            output_path.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)

        response = self._request_with_token_refresh("GET", download_url, stream=True, timeout=300)
        if response.status_code not in (200, 206):
            logger.error("下载失败 %s HTTP %s %s", product_id, response.status_code, response.text[:500])
            emit_download_progress(product_id, 0, "下载失败")
            return False

        total_size = expected_size or int(response.headers.get("Content-Length", 0) or 0)
        downloaded = 0
        last_emit_time = 0.0
        last_emit_percent = -1
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"下载 {product_id}", total=total_size or None)
            with part_path.open("wb") as file_obj:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        file_obj.write(chunk)
                        downloaded += len(chunk)
                        progress.update(task, completed=downloaded)
                        percent = int(downloaded * 100 / total_size) if total_size else 5
                        now = time.monotonic()
                        if percent != last_emit_percent and (percent - last_emit_percent >= 2 or now - last_emit_time >= 1.0):
                            emit_download_progress(product_id, percent, "下载中")
                            last_emit_percent = percent
                            last_emit_time = now

        if expected_size is not None and part_path.stat().st_size != expected_size:
            part_path.unlink(missing_ok=True)
            emit_download_progress(product_id, 0, "文件不完整")
            return False
        part_path.replace(output_path)
        if extract_dir:
            emit_download_progress(product_id, 98, "解压中")
            ok = self.extract_safe_zip(output_path, extract_dir)
            emit_download_progress(product_id, 100 if ok else 0, "完成" if ok else "解压失败")
            return ok
        emit_download_progress(product_id, 100, "完成")
        return True

    def batch_download(self, product_items: list[dict], output_dir: Path, extract_dir: Path | None, max_workers: int) -> tuple[int, int]:
        max_workers = max(1, min(int(max_workers), 10))
        success_count = 0
        fail_count = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.download_product, item["id"], item["download_url"], output_dir, extract_dir): item["id"]
                for item in product_items
            }
            for future in as_completed(futures):
                try:
                    if future.result():
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as exc:
                    logger.exception("下载异常：%s", exc)
                    emit_download_progress(futures[future], 0, "下载异常")
                    fail_count += 1
        return success_count, fail_count


def load_intersects(aoi_path: str | None, simplify_tolerance: float) -> dict | None:
    if not aoi_path:
        return None
    path = Path(aoi_path)
    if not path.is_absolute():
        path = APP_DIR / path
    if not path.exists():
        return None

    if path.suffix.lower() == ".shp":
        if shapefile is None:
            raise RuntimeError("读取 SHP 需要 pyshp：pip install pyshp")
        for suffix in [".shx", ".dbf"]:
            if not path.with_suffix(suffix).exists():
                raise ValueError(f"SHP 文件不完整，缺少同名 {suffix} 文件：{path.with_suffix(suffix)}")
        reader = shapefile.Reader(str(path))
        shapes = [shape.__geo_interface__ for shape in reader.shapes()]
        if not shapes:
            raise ValueError(f"AOI SHP 没有要素：{path}")
        geometry = shapes[0] if len(shapes) == 1 else {"type": "GeometryCollection", "geometries": shapes}
        prj = path.with_suffix(".prj")
        if not prj.exists():
            raise ValueError(f"SHP 缺少同名 .prj 投影文件，无法转换为经纬度检索区域：{prj}")
        if transform_geom is None:
            raise ValueError("读取 SHP 投影转换需要 rasterio，请安装 rasterio 或改用 EPSG:4326 GeoJSON。")
        crs = prj.read_text(encoding="utf-8", errors="ignore")
        geometry = transform_geom(crs, "EPSG:4326", geometry)
        return geometry

    if path.suffix.lower() not in {".geojson", ".json"}:
        raise ValueError(f"不支持的 AOI 格式：{path.suffix}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        features = data.get("features", [])
        if not features:
            raise ValueError(f"AOI 文件没有 Feature：{path}")
        if len(features) == 1:
            return features[0]["geometry"]
        return {"type": "GeometryCollection", "geometries": [feature["geometry"] for feature in features if feature.get("geometry")]}
    if data.get("type") == "Feature":
        return data["geometry"]
    return data


def main() -> None:
    args = parse_args()
    collection = collection_from_product(args.collection)
    client = CDSEClient()
    if not client.login():
        sys.exit(1)

    features = client.search_products(
        collection=collection,
        start_date=args.start_date,
        end_date=args.end_date,
        limit=args.limit,
        aoi_path=args.aoi,
        aoi_simplify=args.aoi_simplify,
    )
    raw_count = len(features)
    features = filter_by_cloud(features, args.cloud_max)
    if args.cloud_max is not None:
        console.print(f"[yellow]云量过滤：eo:cloud_cover < {args.cloud_max:g}% ，保留 {len(features)} 个，剔除 {raw_count - len(features)} 个[/yellow]")
    product_items = [{"id": item.get("id"), "download_url": client.zip_download_url(item)} for item in features if item.get("id")]
    keywords = parse_contains_keywords(args.contains)
    if keywords:
        before_keyword = len(product_items)
        product_items = [item for item in product_items if any(k.lower() in item["id"].lower() for k in keywords)]
        console.print(f"[yellow]产品名过滤：保留 {len(product_items)} 个，剔除 {before_keyword - len(product_items)} 个[/yellow]")
    selected_ids = parse_product_ids(args.product_ids)
    if selected_ids:
        before_selected = len(product_items)
        product_items = [item for item in product_items if item["id"] in selected_ids]
        console.print(f"[yellow]手动选择过滤：保留 {len(product_items)} 个，剔除 {before_selected - len(product_items)} 个[/yellow]")
    product_items = [item for item in product_items if item.get("download_url")]
    console.print(f"[green]最终可下载/预览产品：{len(product_items)} 个[/green]")

    if args.search_only:
        emit_search_results(features, product_items)
        for index, item in enumerate(product_items, 1):
            console.print(f"{index}. {item['id']}")
        return
    if not product_items:
        console.print("[red]没有可下载产品[/red]")
        sys.exit(1)

    success, failed = client.batch_download(
        product_items,
        output_dir=Path(args.output_dir),
        extract_dir=None if args.no_extract else Path(args.extract_dir),
        max_workers=args.max_workers,
    )
    console.print(f"[green]下载完成：成功 {success}，失败 {failed}[/green]")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
