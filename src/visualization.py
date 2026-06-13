"""
Visualization helpers for Sentinel-2 RGB GeoTIFFs.

- save_rgb_png       : render RGB GeoTIFF as PNG with boundary overlay + metadata caption
- save_metadata_json : write scene metadata to a JSON file
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pyproj
import rasterio
from matplotlib.lines import Line2D
from shapely.ops import transform as shapely_transform


def _percentile_stretch(arr: np.ndarray, lo: float = 2, hi: float = 98) -> np.ndarray:
    """Stretch each band to [0, 1] using percentile clipping, ignoring nodata (0)."""
    out = np.zeros_like(arr, dtype=np.float32)
    for i in range(arr.shape[0]):
        band = arr[i].astype(np.float32)
        valid = band[band > 0]
        if valid.size == 0:
            continue
        p_lo, p_hi = np.percentile(valid, [lo, hi])
        out[i] = np.clip((band - p_lo) / (p_hi - p_lo + 1e-6), 0, 1)
    return out


def _reproject_geom(geom, src_epsg: int, dst_crs):
    dst_epsg = dst_crs.to_epsg()
    if dst_epsg and dst_epsg == src_epsg:
        return geom
    t = pyproj.Transformer.from_crs(
        pyproj.CRS(f"EPSG:{src_epsg}"),
        pyproj.CRS(dst_crs.to_wkt()),
        always_xy=True,
    ).transform
    return shapely_transform(t, geom)


def save_rgb_png(
    rgb_tif: Path,
    output_png: Path,
    aoi_geom=None,
    metadata: dict | None = None,
    title: str = "Sentinel-2 RGB",
) -> Path:
    """
    Render an RGB GeoTIFF as a PNG with optional boundary overlay and metadata caption.

    Args:
        rgb_tif:     Path to the 3-band RGB GeoTIFF (B04/B03/B02)
        output_png:  Destination PNG path
        aoi_geom:    Shapely geometry (WGS84) to draw as boundary overlay
        metadata:    Dict returned by download_rgb_geotiff (scene id, date, cloud %)
        title:       Plot title
    """
    output_png = Path(output_png)
    output_png.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(rgb_tif) as src:
        rgb = src.read()        # (3, H, W) uint16
        raster_crs = src.crs
        t = src.transform

    rgb_display = _percentile_stretch(rgb)          # float32 0–1

    # Replace nodata pixels (all bands == 0) with white (1.0)
    nodata_mask = np.all(rgb == 0, axis=0)           # (H, W) bool
    rgb_display[:, nodata_mask] = 1.0

    rgb_hwc = np.moveaxis(rgb_display, 0, -1)       # (H, W, 3) for imshow

    extent = (
        t.c,
        t.c + t.a * rgb.shape[2],
        t.f + t.e * rgb.shape[1],
        t.f,
    )

    fig, ax = plt.subplots(figsize=(10, 10), facecolor="white")
    ax.set_facecolor("white")
    ax.imshow(rgb_hwc, extent=extent, origin="upper", interpolation="bilinear")

    # --- Exact boundary overlay ---
    if aoi_geom is not None:
        geom_proj = _reproject_geom(aoi_geom, 4326, raster_crs)
        gdf = gpd.GeoDataFrame(geometry=[geom_proj], crs=raster_crs)
        gdf.boundary.plot(ax=ax, color="red", linewidth=1.8)
        legend_handles = [Line2D([0], [0], color="red", linewidth=1.8, label="Admin boundary")]
        ax.legend(handles=legend_handles, loc="lower right", fontsize=9)

    # --- Metadata caption below plot ---
    if metadata:
        lines = []
        for s in metadata.get("scenes", []):
            lines.append(
                f"Scene: {s['id']}\n"
                f"Date: {s['date']}  |  Cloud: {s['cloud_pct']:.1f}%  "
                f"|  Tile: {s['tile']}  |  Platform: {s['platform']}"
            )
        ax.set_xlabel("\n\n".join(lines), fontsize=12, labelpad=10)

    ax.set_title(title, fontsize=14, fontweight="bold", pad=12)
    ax.set_aspect("equal")
    ax.tick_params(labelsize=10)

    plt.tight_layout()
    fig.savefig(output_png, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    print(f"PNG saved: {output_png}")
    return output_png


def save_metadata_json(metadata: dict, output_path: Path) -> Path:
    """Write scene metadata dict to a JSON file."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    print(f"Metadata saved: {output_path}")
    return output_path
