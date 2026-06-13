"""
Download a Sentinel-2 RGB GeoTIFF via Planetary Computer STAC.

- Connects to Microsoft Planetary Computer STAC
- Finds the best (least-cloudy, most-covering) scene per S2 tile
- Reads B04 / B03 / B02 bands directly via COG streaming (no .SAFE download)
- Mosaics all tiles and clips to the AOI bbox
- Writes a single 3-band GeoTIFF (R, G, B)
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np
import planetary_computer
import pyproj
import pystac_client
import rasterio
from rasterio.features import geometry_mask
from rasterio.merge import merge
from rasterio.windows import from_bounds
from shapely.geometry import box, mapping
from shapely.ops import transform as shapely_transform

from src.visualization import save_rgb_png

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
RGB_BANDS = ("B04", "B03", "B02")  # Red, Green, Blue


def _reproject_bbox(bbox_wgs84: tuple, dst_crs) -> tuple:
    if dst_crs.to_epsg() == 4326:
        return bbox_wgs84
    t = pyproj.Transformer.from_crs("EPSG:4326", dst_crs, always_xy=True)
    minx, miny = t.transform(bbox_wgs84[0], bbox_wgs84[1])
    maxx, maxy = t.transform(bbox_wgs84[2], bbox_wgs84[3])
    return (minx, miny, maxx, maxy)


def _tile_id(item) -> str:
    """Extract MGRS tile ID from a STAC item."""
    tile = item.properties.get("s2:mgrs_tile") or item.properties.get("mgrs:utm_zone")
    if tile:
        return str(tile)
    m = re.search(r'_T(\d{2}[A-Z]{3})[_.]', item.id)
    return m.group(1) if m else "UNKNOWN"


def _coverage(item, bbox: tuple) -> float:
    """Fraction of bbox covered by the item footprint."""
    from shapely.geometry import shape
    aoi = box(*bbox)
    if aoi.area <= 0:
        return 0.0
    geom = item.geometry
    if geom is None:
        return 0.0
    footprint = shape(geom)
    inter = footprint.intersection(aoi)
    return float(inter.area / aoi.area)


def _best_per_tile(items, bbox: tuple) -> list:
    """
    Group items by tile and return the best item per tile.
    Best = highest bbox coverage, then lowest cloud cover.
    If any single tile covers the full AOI (>= 99%), return only that one.
    """
    scored = [
        (_coverage(it, bbox), it.properties.get("eo:cloud_cover", 999), it)
        for it in items
    ]

    # Single scene covers full AOI — no mosaic needed
    full = [(cov, cloud, it) for cov, cloud, it in scored if cov >= 0.99]
    if full:
        full.sort(key=lambda t: (t[1], -t[0]))
        cov, cloud, best = full[0]
        print(f"  Single tile covers full AOI: {best.id} | coverage={cov*100:.1f}% | cloud={cloud:.1f}%")
        return [best]

    # Group by tile, pick best per tile
    tiles: dict[str, list] = defaultdict(list)
    for cov, cloud, it in scored:
        tiles[_tile_id(it)].append((cov, cloud, it))

    best_items = []
    for tile, candidates in sorted(tiles.items()):
        candidates.sort(key=lambda t: (-t[0], t[1]))
        cov, cloud, best = candidates[0]
        print(f"  Tile {tile}: {best.id} | coverage={cov*100:.1f}% | cloud={cloud:.1f}%")
        best_items.append(best)

    return best_items


def download_rgb_geotiff(
    aoi_geom,
    datetime_range: str,
    max_cloud: float,
    output_path: Path,
    output_png: Optional[Path] = None,
    png_title: str = "Sentinel-2 RGB",
) -> tuple[Path, dict]:
    """
    Find the best Sentinel-2 L2A scenes per tile via Planetary Computer STAC,
    mosaic them, clip to the exact AOI boundary, and save an RGB GeoTIFF.
    Optionally renders a PNG with boundary overlay and scene metadata caption.

    Args:
        aoi_geom:         shapely geometry (WGS84) — bbox derived from its bounds
        datetime_range:   date range e.g. '2019-01-01/2019-12-31'
        max_cloud:        max cloud cover threshold (%)
        output_path:      destination .tif path
        output_png:       if provided, save a visualisation PNG to this path
        png_title:        title for the PNG figure

    Returns:
        (path to GeoTIFF, metadata dict)
    """
    bbox = aoi_geom.bounds  # (min_lon, min_lat, max_lon, max_lat)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = output_path.with_suffix(".json")

    # --- Skip if already downloaded ---
    if output_path.exists():
        print(f"RGB GeoTIFF already exists, skipping download: {output_path.name}")
        metadata = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
        if output_png is not None and not Path(output_png).exists():
            save_rgb_png(rgb_tif=output_path, output_png=output_png,
                         aoi_geom=aoi_geom, metadata=metadata, title=png_title)
        return output_path, metadata

    # --- Step 1: STAC search ---
    catalog = pystac_client.Client.open(
        PC_STAC_URL,
        modifier=planetary_computer.sign_inplace,
    )

    search = catalog.search(
        collections=["sentinel-2-l2a"],
        intersects=mapping(box(*bbox)),
        datetime=datetime_range,
        query={"eo:cloud_cover": {"lt": max_cloud}},
        limit=500,
    )

    items = list(search.get_items())
    if not items:
        raise RuntimeError("No scenes found. Try relaxing the cloud threshold or date range.")

    print(f"Found {len(items)} scenes — selecting best per tile ...")
    selected = _best_per_tile(items, bbox)
    print(f"{len(selected)} tile(s) selected\n")

    # Collect metadata from selected scenes
    metadata = {
        "scenes": [
            {
                "id":         it.id,
                "date":       it.properties.get("datetime", "")[:10],
                "cloud_pct":  it.properties.get("eo:cloud_cover"),
                "tile":       _tile_id(it),
                "platform":   it.properties.get("platform", ""),
            }
            for it in selected
        ],
        "datetime_range": datetime_range,
        "max_cloud":      max_cloud,
        "bbox":           list(bbox),
    }

    # --- Step 2: Read each band window from each tile ---
    # tile_arrays[band_index] = list of (array, profile) per tile
    tile_band_data: list[list[tuple[np.ndarray, dict]]] = [[] for _ in RGB_BANDS]

    for item in selected:
        for b_idx, band in enumerate(RGB_BANDS):
            href = item.assets[band].href
            print(f"  {item.id[:40]}… {band}")
            with rasterio.open(href) as src:
                bbox_src = _reproject_bbox(bbox, src.crs)
                window = from_bounds(*bbox_src, transform=src.transform)
                data = src.read(1, window=window)
                profile = src.profile.copy()
                profile.update(
                    driver="GTiff",
                    count=1,
                    height=data.shape[0],
                    width=data.shape[1],
                    transform=src.window_transform(window),
                )
            tile_band_data[b_idx].append((data, profile))

    # --- Step 3: Mosaic tiles per band, then stack R/G/B ---
    print("\nMosaicking and stacking bands ...")
    rgb_bands: list[np.ndarray] = []
    out_profile: Optional[dict] = None

    for b_idx, band_tiles in enumerate(tile_band_data):
        if len(band_tiles) == 1:
            arr, prof = band_tiles[0]
            if out_profile is None:
                out_profile = prof.copy()
            rgb_bands.append(arr)
        else:
            # Write each tile to an in-memory file and merge
            import io
            mem_files = []
            for arr, prof in band_tiles:
                buf = io.BytesIO()
                with rasterio.open(buf, "w", **prof) as tmp:
                    tmp.write(arr, 1)
                buf.seek(0)
                mem_files.append(rasterio.open(buf))

            mosaic, transform = merge(mem_files, bounds=bbox if out_profile is None else None)
            for f in mem_files:
                f.close()

            arr = mosaic[0]
            if out_profile is None:
                out_profile = band_tiles[0][1].copy()
                out_profile.update(
                    height=arr.shape[0],
                    width=arr.shape[1],
                    transform=transform,
                )
            rgb_bands.append(arr)

    # --- Step 4: Apply exact boundary mask ---
    rgb = np.stack(rgb_bands, axis=0)  # (3, H, W)

    if aoi_geom is not None:
        # Reproject geometry from WGS84 to the output raster CRS
        out_crs = rasterio.crs.CRS(out_profile["crs"])
        src_proj = pyproj.CRS("EPSG:4326")
        dst_proj = pyproj.CRS(out_crs.to_epsg() if out_crs.to_epsg() else out_crs.to_wkt())
        projector = pyproj.Transformer.from_crs(src_proj, dst_proj, always_xy=True).transform
        geom_proj = shapely_transform(projector, aoi_geom)

        # True where pixels are OUTSIDE the boundary
        outside = geometry_mask(
            [geom_proj],
            transform=out_profile["transform"],
            invert=False,
            out_shape=(out_profile["height"], out_profile["width"]),
        )
        rgb[:, outside] = 0  # set pixels outside boundary to 0 (nodata)
        out_profile["nodata"] = 0
        print("Exact boundary mask applied.")

    # --- Step 5: Write 3-band GeoTIFF ---
    out_profile.update(
        count=3,
        compress="lzw",
        tiled=True,
        blockxsize=256,
        blockysize=256,
    )
    with rasterio.open(output_path, "w", **out_profile) as dst:
        dst.write(rgb)

    h, w = rgb.shape[1], rgb.shape[2]
    print(f"\nSaved: {output_path}  ({w} × {h} px)")
    sidecar.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    if output_png is not None:
        save_rgb_png(
            rgb_tif=output_path,
            output_png=output_png,
            aoi_geom=aoi_geom,
            metadata=metadata,
            title=png_title,
        )

    return output_path, metadata


