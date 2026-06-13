"""
Sentinel-2 downloader utilities (CDSE).
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple

import geopandas as gpd
import requests
from shapely.geometry import box as shapely_box
from shapely.geometry import mapping as shapely_mapping
from shapely.geometry import shape as shapely_shape
from tqdm import tqdm


# -------------------------
# CDSE endpoints
# -------------------------
STAC_API_URL = "https://stac.dataspace.copernicus.eu/v1"
STAC_SEARCH_URL = f"{STAC_API_URL}/search"

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"

ODATA_CATALOGUE = "https://catalogue.dataspace.copernicus.eu/odata/v1"
ODATA_DOWNLOAD = "https://download.dataspace.copernicus.eu/odata/v1"


# -------------------------
# Data structures
# -------------------------
@dataclass(frozen=True)
class BestScene:
    """Result of STAC selection."""
    item: dict[str, Any]          # STAC feature dict
    coverage: float               # [0..1]
    cloud_cover: float            # percent


# -------------------------
# AOI utilities
# -------------------------
def admin_shp_to_bbox_wgs84(
    admin_shp: Path,
    filter_field: Optional[str] = None,
    filter_value: Optional[str] = None,
) -> tuple[float, float, float, float]:
    """
    Read admin boundary file and return bbox in EPSG:4326 (lon/lat).
    Optionally filter to rows where filter_field == filter_value.
    bbox order: (min_lon, min_lat, max_lon, max_lat)
    """
    gdf = gpd.read_file(admin_shp)
    if gdf.empty:
        raise RuntimeError(f"Admin file has no features: {admin_shp}")

    if filter_field is not None and filter_value is not None:
        gdf = gdf[gdf[filter_field] == filter_value]
        if gdf.empty:
            raise RuntimeError(
                f"No features found where {filter_field}='{filter_value}' in {admin_shp}"
            )

    gdf = gdf.to_crs(epsg=4326)
    minx, miny, maxx, maxy = gdf.total_bounds
    return (float(minx), float(miny), float(maxx), float(maxy))


def load_aoi(
    admin_shp: Path,
    filter_field: Optional[str] = None,
    filter_value: Optional[str] = None,
) -> tuple[tuple[float, float, float, float], Any]:
    """
    Read admin boundary file and return (bbox, exact_geometry) in EPSG:4326.

    Returns:
        bbox:     (min_lon, min_lat, max_lon, max_lat)
        geometry: dissolved shapely geometry of the filtered region
    """
    gdf = gpd.read_file(admin_shp)
    if gdf.empty:
        raise RuntimeError(f"Admin file has no features: {admin_shp}")

    if filter_field is not None and filter_value is not None:
        gdf = gdf[gdf[filter_field] == filter_value]
        if gdf.empty:
            raise RuntimeError(
                f"No features found where {filter_field}='{filter_value}' in {admin_shp}"
            )

    gdf = gdf.to_crs(epsg=4326)
    minx, miny, maxx, maxy = gdf.total_bounds
    bbox = (float(minx), float(miny), float(maxx), float(maxy))
    geom = gdf.dissolve().geometry.iloc[0]
    return bbox, geom


def aoi_geojson_from_admin_shapefile(admin_shp: Path) -> dict[str, Any]:
    """
    Dissolve admin boundaries into one AOI geometry (EPSG:4326) and return GeoJSON geometry.
    """
    gdf = gpd.read_file(admin_shp)
    if gdf.empty:
        raise RuntimeError(f"Admin boundaries are empty: {admin_shp}")
    gdf = gdf.to_crs(epsg=4326)
    geom = gdf.dissolve().geometry.iloc[0]
    return shapely_mapping(geom)


# -------------------------
# Time utilities
# -------------------------
def datetime_range_with_buffer(center_date_ymd: str, buffer_days: int) -> str:
    """
    Convert center date YYYY-MM-DD into a strict RFC3339 STAC range:
      YYYY-MM-DDT00:00:00Z/YYYY-MM-DDT23:59:59Z
    using ± buffer_days.

    This avoids CDSE STAC 400 errors that sometimes happen with date-only ranges.
    """
    center = datetime.strptime(center_date_ymd, "%Y-%m-%d").date()
    start = center - timedelta(days=buffer_days)
    end = center + timedelta(days=buffer_days)

    start_s = f"{start.isoformat()}T00:00:00Z"
    end_s = f"{end.isoformat()}T23:59:59Z"
    return f"{start_s}/{end_s}"


# -------------------------
# Local SAFE discovery
# -------------------------
def find_safe_by_exact_id(parent_dir: Path, item_id: str) -> Optional[Path]:
    """
    Return the .SAFE folder that exactly matches the STAC item id.

    - If item_id endswith ".SAFE", checks that folder name exactly.
    - Else checks "<item_id>.SAFE"
    """
    parent_dir = Path(parent_dir)
    if not parent_dir.exists():
        return None

    item_id = (item_id or "").strip()
    if not item_id:
        return None

    expected_name = item_id if item_id.endswith(".SAFE") else f"{item_id}.SAFE"
    expected_path = parent_dir / expected_name

    if expected_path.exists() and expected_path.is_dir():
        return expected_path
    return None


# -------------------------
# STAC search + scoring
# -------------------------
_STAC_PAGE_SIZE = 100  # safe limit without requiring fields extension


def _stac_post(payload: dict[str, Any]) -> dict[str, Any]:
    r = requests.post(STAC_SEARCH_URL, json=payload, timeout=60)
    if not r.ok or not r.text.strip():
        raise RuntimeError(
            f"STAC search failed: HTTP {r.status_code}\n"
            f"Response: {r.text[:1000]!r}"
        )
    try:
        return r.json() or {}
    except Exception:
        raise RuntimeError(
            f"STAC returned non-JSON (HTTP {r.status_code}):\n{r.text[:1000]!r}"
        )


def _stac_search_s2_l2a(
    *,
    aoi_geojson: Optional[dict[str, Any]] = None,
    bbox: Optional[tuple[float, float, float, float]] = None,
    datetime_range: str,
    max_cloud_cover: float,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """
    CDSE STAC paginated search. Follows 'next' links until all results collected
    or `limit` total items reached.
    Uses bbox search to avoid WAF rejection from large geometry payloads.
    """
    base_payload: dict[str, Any] = {
        "collections": ["sentinel-2-l2a"],
        "datetime": datetime_range,
        "query": {"eo:cloud_cover": {"lte": float(max_cloud_cover)}},
        "limit": _STAC_PAGE_SIZE,
    }
    if bbox is not None:
        base_payload["bbox"] = list(bbox)
    elif aoi_geojson is not None:
        base_payload["intersects"] = aoi_geojson
    else:
        raise ValueError("Provide either bbox or aoi_geojson")

    all_features: list[dict[str, Any]] = []
    payload = base_payload.copy()

    while True:
        data = _stac_post(payload)
        features = data.get("features", [])
        all_features.extend(features)

        if len(all_features) >= limit or len(features) < _STAC_PAGE_SIZE:
            break

        # Follow STAC pagination: look for a 'next' link with a body
        next_link = next(
            (lk for lk in data.get("links", []) if lk.get("rel") == "next"),
            None,
        )
        if next_link is None:
            break

        # CDSE encodes the next-page payload in link["body"]
        next_body = next_link.get("body")
        if next_body and isinstance(next_body, dict):
            payload = next_body
        else:
            # Fall back to token-based pagination if available
            token = next_link.get("token") or next_link.get("href", "").split("token=")[-1]
            if not token or token == next_link.get("href", ""):
                break
            payload = {**base_payload, "token": token}

    return all_features[:limit]


def _cloud_cover(item: dict[str, Any]) -> float:
    props = item.get("properties") or {}
    v = props.get("eo:cloud_cover")
    try:
        return float(v)
    except Exception:
        return 1e9


def _coverage_fraction(item: dict[str, Any], bbox: tuple[float, float, float, float]) -> float:
    """
    Coverage = area(intersection(item_footprint, bbox)) / area(bbox).
    """
    geom = item.get("geometry")
    if not geom:
        return 0.0

    aoi = shapely_box(*bbox)
    if aoi.area <= 0:
        return 0.0

    footprint = shapely_shape(geom)
    inter = footprint.intersection(aoi)
    if inter.is_empty:
        return 0.0

    return float(inter.area / aoi.area)


def stac_find_best_s2_l2a(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    max_cloud: float,
    *,
    limit: int = 200,
) -> tuple[Optional[dict[str, Any]], Optional[float], Optional[float]]:
    """
    Find best Sentinel-2 L2A item for AOI/time range.

    Selection:
      1) highest bbox coverage
      2) lowest eo:cloud_cover

    Returns: (best_item_feature, coverage_fraction, cloud_cover)
    """
    items = _stac_search_s2_l2a(
        bbox=bbox,
        datetime_range=datetime_range,
        max_cloud_cover=max_cloud,
        limit=limit,
    )
    if not items:
        return None, None, None

    scored = []
    for it in items:
        cov = _coverage_fraction(it, bbox)
        cloud = _cloud_cover(it)
        scored.append((cov, cloud, it))

    scored.sort(key=lambda t: (-t[0], t[1]))
    cov, cloud, best = scored[0]
    return best, cov, cloud


def _tile_id_from_item(item: dict[str, Any]) -> str:
    """Extract MGRS tile ID from a STAC item (e.g. '30UWU')."""
    props = item.get("properties") or {}
    tile = props.get("s2:mgrs_tile") or props.get("mgrs:utm_zone")
    if tile:
        return str(tile)
    # Fall back: parse from item id  e.g. S2A_MSIL2A_..._T30UWU_...
    import re
    m = re.search(r'_T(\d{2}[A-Z]{3})[_.]', str(item.get("id", "")))
    return m.group(1) if m else "UNKNOWN"


def stac_find_best_per_tile(
    bbox: tuple[float, float, float, float],
    datetime_range: str,
    max_cloud: float,
    *,
    full_coverage_threshold: float = 0.99,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """
    Search all scenes and return the minimum set needed to cover the AOI.

    If a single scene already covers >= full_coverage_threshold of the bbox,
    return only that scene (no multi-tile download needed).
    Otherwise return the best scene per tile.

    'Best' = highest AOI bbox coverage, then lowest cloud cover.
    """
    items = _stac_search_s2_l2a(
        bbox=bbox,
        datetime_range=datetime_range,
        max_cloud_cover=max_cloud,
        limit=limit,
    )
    if not items:
        return []

    # Score every item
    scored: list[tuple[float, float, dict[str, Any]]] = []
    for it in items:
        cov = _coverage_fraction(it, bbox)
        cloud = _cloud_cover(it)
        scored.append((cov, cloud, it))

    # Check if any single scene covers the full AOI
    full_cover = [(cov, cloud, it) for cov, cloud, it in scored if cov >= full_coverage_threshold]
    if full_cover:
        full_cover.sort(key=lambda t: (t[1], -t[0]))  # best: lowest cloud, then most coverage
        cov, cloud, best = full_cover[0]
        print(f"  Single scene covers full AOI — using only:")
        print(f"  Tile {_tile_id_from_item(best)}: {best.get('id')} | coverage={cov*100:.1f}% | cloud={cloud:.1f}%")
        return [best]

    # AOI spans multiple tiles — pick best per tile
    from collections import defaultdict
    tile_candidates: dict[str, list[tuple[float, float, dict[str, Any]]]] = defaultdict(list)
    for cov, cloud, it in scored:
        tile_candidates[_tile_id_from_item(it)].append((cov, cloud, it))

    best_per_tile = []
    for tile, candidates in sorted(tile_candidates.items()):
        candidates.sort(key=lambda t: (-t[0], t[1]))
        cov, cloud, best = candidates[0]
        print(f"  Tile {tile}: {best.get('id')} | coverage={cov*100:.1f}% | cloud={cloud:.1f}%")
        best_per_tile.append(best)

    return best_per_tile


def stac_search_all_s2_l2a(
    datetime_range: str,
    max_cloud: float,
    *,
    admin_shp: Optional[Path] = None,
    bbox: Optional[tuple[float, float, float, float]] = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """
    Return ALL Sentinel-2 L2A items matching the time range and cloud threshold.
    Uses bbox search (avoids WAF rejection from large geometry payloads).
    """
    if bbox is None:
        if admin_shp is None:
            raise ValueError("Provide either admin_shp or bbox")
        bbox = admin_shp_to_bbox_wgs84(admin_shp)

    return _stac_search_s2_l2a(
        bbox=bbox,
        datetime_range=datetime_range,
        max_cloud_cover=max_cloud,
        limit=limit,
    )


def ensure_all_safe_downloaded(
    items: list[dict[str, Any]],
    out_dir: Path,
    username: Optional[str] = None,
    password: Optional[str] = None,
    keep_zip: bool = False,
) -> list[Path]:
    """
    Download all STAC items, skipping any already present locally.
    Returns list of .SAFE paths in the same order as items.
    """
    safe_paths = []
    for i, item in enumerate(items, 1):
        item_id = str(item.get("id") or "")
        print(f"  [{i}/{len(items)}] {item_id}")
        path = ensure_safe_downloaded(
            out_dir=out_dir,
            item=item,
            username=username,
            password=password,
            keep_zip=keep_zip,
        )
        safe_paths.append(path)
    return safe_paths


# -------------------------
# Download via OData
# -------------------------
def _require_credentials(username: Optional[str], password: Optional[str]) -> tuple[str, str]:
    """
    If credentials are missing, raise with instructions (no prompting here).
    """
    u = (username or "").strip()
    p = (password or "").strip()
    if u and p:
        return u, p

    raise RuntimeError(
        "CDSE credentials are required to download Sentinel-2 products, but were not provided.\n\n"
        "What to do:\n"
        "1) Create a Copernicus Data Space Ecosystem (CDSE) account:\n"
        "2) Use your CDSE email + password as credentials.\n"
        "3) Provide them into main.py:\n"
    )


def _get_access_token(username: str, password: str) -> str:
    data = {
        "client_id": "cdse-public",
        "grant_type": "password",
        "username": username,
        "password": password,
    }
    r = requests.post(TOKEN_URL, data=data, timeout=60)
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise RuntimeError(f"No access_token in response: {r.text[:500]}")
    return token


def _odata_lookup_uuid_by_name(product_name: str) -> str:
    params = {
        "$filter": f"Name eq '{product_name}'",
        "$select": "Id,Name",
        "$top": "1",
    }
    r = requests.get(f"{ODATA_CATALOGUE}/Products", params=params, timeout=60)
    r.raise_for_status()
    value = (r.json() or {}).get("value", [])
    if not value:
        raise RuntimeError(f"Product not found in OData catalogue by Name: {product_name}")
    return value[0]["Id"]


def _download_product_zip(product_uuid: str, access_token: str, out_zip: Path) -> None:
    url = f"{ODATA_DOWNLOAD}/Products({product_uuid})/$value"
    headers = {"Authorization": f"Bearer {access_token}"}

    out_zip.parent.mkdir(parents=True, exist_ok=True)

    with requests.get(url, headers=headers, stream=True, timeout=300) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", "0") or "0")

        with open(out_zip, "wb") as f, tqdm(
            total=total if total > 0 else None,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=out_zip.name,
        ) as bar:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                bar.update(len(chunk))


def _unzip_safe(zip_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(out_dir)

    safe_dirs = [p for p in out_dir.iterdir() if p.is_dir() and p.name.endswith(".SAFE")]
    if not safe_dirs:
        raise FileNotFoundError(f"No .SAFE folder found after extracting: {zip_path}")
    return sorted(safe_dirs, key=lambda p: p.name)[0]


# -------------------------
# Public: ensure SAFE exists by exact STAC item id
# -------------------------
def ensure_safe_downloaded(
    *,
    out_dir: Path,
    item: dict[str, Any],
    username: Optional[str] = None,
    password: Optional[str] = None,
    keep_zip: bool = False,
) -> Path:
    """
    Ensure the `.SAFE` folder for a selected STAC item exists in out_dir.

    - First checks exact id: <item_id>.SAFE
    - If exists: returns path (no credentials needed)
    - If missing: downloads via OData (credentials required), unzips, returns extracted .SAFE

    Parameters:
      item: STAC Feature dict with "id"
      username/password: CDSE creds (email/password). Required only if download is needed.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    item_id = str(item.get("id") or "").strip()
    if not item_id:
        raise ValueError("STAC item missing 'id'")

    # Exact-id check
    existing = find_safe_by_exact_id(out_dir, item_id)
    if existing is not None:
        return existing

    # Need download -> require credentials
    u, p = _require_credentials(username, password)

    # OData Name is typically "<item_id>.SAFE"
    product_name = f"{item_id}.SAFE"
    token = _get_access_token(u, p)
    uuid = _odata_lookup_uuid_by_name(product_name)

    zip_path = out_dir / f"{item_id}.zip"
    _download_product_zip(uuid, token, zip_path)
    safe_path = _unzip_safe(zip_path, out_dir)

    if not keep_zip:
        try:
            zip_path.unlink()
        except OSError:
            pass

    return safe_path


def ensure_sentinel2_safe(
    *,
    out_dir: Path,
    admin_shp_path: Path,
    center_date_ymd: str,
    buffer_days: int,
    max_cloud_cover: float,
    username: Optional[str] = None,
    password: Optional[str] = None,
    keep_zip: bool = False,
    limit: int = 200,
) -> Tuple[Path, BestScene]:
    """
    One-stop helper:
    - Builds datetime range = center_date ± buffer_days
    - STAC search for best item (coverage then cloud)
    - Ensure .SAFE exists locally by exact id (download if missing)

    Returns:
      (safe_path, BestScene(item, coverage, cloud_cover))
    """
    datetime_range = datetime_range_with_buffer(center_date_ymd, buffer_days)

    bbox = admin_shp_to_bbox_wgs84(admin_shp_path)
    aoi = aoi_geojson_from_admin_shapefile(admin_shp_path)

    best_item, cov, cloud = stac_find_best_s2_l2a(
        bbox,
        datetime_range,
        max_cloud_cover,
        aoi_geojson=aoi,
        limit=limit,
    )
    if best_item is None or cov is None or cloud is None:
        raise RuntimeError("No STAC items found for the given constraints.")

    safe_path = ensure_safe_downloaded(
        out_dir=out_dir,
        item=best_item,
        username=username,
        password=password,
        keep_zip=keep_zip,
    )

    return safe_path, BestScene(item=best_item, coverage=cov, cloud_cover=cloud)
