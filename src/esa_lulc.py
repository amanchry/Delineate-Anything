"""
Download ESA WorldCover LULC tiles via Planetary Computer STAC.

- Searches the 'esa-worldcover' collection for the requested year
- Downloads all tiles that intersect the AOI bbox
- Mosaics tiles and clips to exact admin boundary
- Writes a single-band uint8 GeoTIFF (10 m resolution, EPSG:4326)
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import planetary_computer
import pyproj
import pystac_client
import requests
import rasterio
from rasterio.mask import mask as rasterio_mask
from rasterio.merge import merge
from shapely.geometry import mapping
from shapely.ops import transform as shapely_transform
from tqdm import tqdm

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"

VALID_YEARS = (2020, 2021)


def download_esa_worldcover(
    aoi_geom,
    output_path: Path,
    year: int = 2021,
    tiles_dir: Optional[Path] = None,
    keep_tiles: bool = False,
) -> Path:
    """
    Download ESA WorldCover tiles for an AOI and clip to exact boundary.

    Args:
        aoi_geom:    shapely geometry (WGS84) — bbox derived from its bounds
        output_path: destination .tif path
        year:        2020 or 2021
        tiles_dir:   directory for intermediate tile files
                     (default: <output_path.parent>/esa_tiles)
        keep_tiles:  keep intermediate tile files after mosaicking

    Returns:
        Path to the final clipped GeoTIFF
    """
    bbox = aoi_geom.bounds  # (min_lon, min_lat, max_lon, max_lat)
    if year not in VALID_YEARS:
        raise ValueError(f"year must be one of {VALID_YEARS}, got {year}")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        print(f"ESA WorldCover already exists, skipping: {output_path.name}")
        return output_path

    if tiles_dir is None:
        tiles_dir = output_path.parent / "esa_tiles"
    tiles_dir = Path(tiles_dir)
    tiles_dir.mkdir(parents=True, exist_ok=True)

    # --- Step 1: STAC search ---
    catalog = pystac_client.Client.open(
        PC_STAC_URL,
        modifier=planetary_computer.sign_inplace,
    )

    search = catalog.search(
        collections=["esa-worldcover"],
        bbox=list(bbox),
        datetime=f"{year}-01-01/{year}-12-31",
    )

    try:
        items = list(search.get_items())
    except Exception as e:
        raise RuntimeError(f"STAC search failed for year {year}: {e}") from e

    if not items:
        raise RuntimeError(
            f"No ESA WorldCover tiles found for {year} within bbox {bbox}"
        )

    print(f"Found {len(items)} tile(s) for {year}")

    # --- Step 2: Download tiles ---
    tile_paths: list[Path] = []
    for item in tqdm(items, desc=f"Downloading ESA WorldCover {year}"):
        signed = planetary_computer.sign(item)
        href = signed.assets["map"].href
        tile_path = tiles_dir / f"{item.id}.tif"
        tile_paths.append(tile_path)

        if tile_path.exists():
            continue

        with requests.get(href, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tile_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)

    # --- Step 3: Mosaic + clip ---
    print("Mosaicking tiles ...")
    src_files = [rasterio.open(p) for p in tile_paths]
    mosaic, mosaic_transform = merge(src_files, bounds=bbox)
    mosaic_crs = src_files[0].crs
    mosaic_nodata = src_files[0].nodata
    for f in src_files:
        f.close()

    arr = mosaic[0]  # single-band (H, W)

    profile = {
        "driver":     "GTiff",
        "dtype":      arr.dtype,
        "count":      1,
        "height":     arr.shape[0],
        "width":      arr.shape[1],
        "crs":        mosaic_crs,
        "transform":  mosaic_transform,
        "compress":   "lzw",
        "tiled":      True,
        "blockxsize": 256,
        "blockysize": 256,
        "nodata":     mosaic_nodata if mosaic_nodata is not None else 0,
    }

    if aoi_geom is not None:
        # ESA WorldCover is in EPSG:4326 — no reprojection needed
        out_crs = rasterio.crs.CRS(mosaic_crs)
        if out_crs.to_epsg() != 4326:
            t = pyproj.Transformer.from_crs(
                "EPSG:4326", out_crs, always_xy=True
            ).transform
            geom_clip = shapely_transform(t, aoi_geom)
        else:
            geom_clip = aoi_geom

        # Write mosaic to a temp in-memory file so rasterio.mask can clip it
        import io
        buf = io.BytesIO()
        with rasterio.open(buf, "w", **{**profile, "driver": "GTiff"}) as tmp:
            tmp.write(arr, 1)
        buf.seek(0)

        with rasterio.open(buf) as tmp:
            clipped, clip_transform = rasterio_mask(
                tmp,
                [mapping(geom_clip)],
                crop=True,
                nodata=profile["nodata"],
            )

        arr = clipped[0]
        profile.update(
            height=arr.shape[0],
            width=arr.shape[1],
            transform=clip_transform,
        )
        print("Exact boundary clip applied.")

    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(arr, 1)

    h, w = arr.shape
    print(f"ESA WorldCover saved: {output_path}  ({w} x {h} px)")

    if not keep_tiles:
        for p in tile_paths:
            p.unlink(missing_ok=True)

    return output_path
