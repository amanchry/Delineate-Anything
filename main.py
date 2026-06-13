"""
main.py — Field Boundary Delineation Pipeline
==============================================
Edit the CONFIG section below, then run:
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

os.environ["GTIFF_SRS_SOURCE"] = "EPSG"

from osgeo import gdal
gdal.UseExceptions()
gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")

from src.sentinel2_downloader import load_aoi
from src.sentinel2_rgb_stac import download_rgb_geotiff
from src.esa_lulc import download_esa_worldcover
from delineate import delineate

logging.basicConfig(level=logging.INFO)

# ==============================================================================
#  CONFIG — Edit everything here
# ==============================================================================

# --- Input boundary ---
AOI_PATH     = "data/France_Admin_Boundary/gadm41_FRA_3.json"  # path to GeoJSON or Shapefile (.json / .shp)
AREA_NAME    = "France_Dinan"                          # short name → used as folder + output filename

# --- Sentinel-2 download ---
DATE_RANGE   = "2023-04-01/2023-10-31"   # YYYY-MM-DD/YYYY-MM-DD
MAX_CLOUD    = 5.0                      # max cloud cover % — increase if no scenes found

# --- Land cover mask (ESA WorldCover) ---
DOWNLOAD_LULC = True    # True  → download & use mask (removes detections on water/urban/forest)
                        # False → skip, run delineation without land cover filtering
LULC_YEAR     = 2021    # 2020 or 2021

# --- Output options ---
SAVE_PNG   = True    # True → save a Sentinel-2 preview PNG alongside the GeoTIFF
KEEP_TEMP  = False   # True → keep intermediate temp files (useful for debugging)

# --- Delineation model ---
MODEL = "large"      # "large" = 125 MB, higher accuracy
                     # "small" = 17.6 MB, faster

# ==============================================================================
#  PIPELINE — Do not edit below this line
# ==============================================================================

ROOT        = Path(__file__).parent
DATA_IMAGES = ROOT / "data" / "images"
DATA_MASKS  = ROOT / "data" / "masks"
DATA_OUT    = ROOT / "data" / "delineated"
DATA_TEMP   = ROOT / "data" / "temp"

DELINEATION_CONFIG = {
    "model":            [MODEL],
    "method":           "main",
    "super_resolution": None,
    "treat_as_vrt":     False,

    "mask_info": {
        "range":          101,
        "filter_classes": [10, 20, 50, 70, 80, 95, 100],  # remove detections here
        "clip_classes":   [90],                            # trim polygon edges here
    },

    "background_info": {
        "background_classes_from_mask": [],
        "additional_source":            None,
    },

    "data_loader": {
        "skip":         False,
        "bands":        [1, 2, 3],      # sentinel2_rgb_stac saves B04/B03/B02 as bands 1/2/3
        "nodata_band":  None,
        "nodata_value": [0, 0, 0],
        "min":          None,           # auto-computed from p1 percentile
        "max":          None,           # auto-computed from p99 percentile
    },

    "execution_planner": {
        "region_width":  -1,            # -1 = auto from available RAM
        "region_height": -1,
        "pixel_offset":  [-1, -1],
    },

    "postprocess_limits": {
        "num_workers":          -1,     # -1 = auto from CPU count
        "queue_tiles_capacity": 4,
        "max_tiles_inflight":   8,
    },

    "passes": [{
        "batch_size": -1,               # -1 = auto from GPU/MPS VRAM
        "tile_size":  None,
        "tile_step":  0.5,
        "model_args": [{
            "name":               MODEL,
            "minimal_confidence": 0.005,
            "use_half":           True,
        }],
        "delineation_config": {
            "pixel_area_threshold":                    512,
            "remaining_area_threshold":                0.8,
            "compose_merge_iou":                       0.8,
            "merge_iou":                               0.8,
            "merge_relative_area_threshold":           0.5,
            "merge_asymetric_pixel_area_threshold":    32,
            "merge_asymetric_relative_area_threshold": 0.7,
            "merging_edge_width":                      4,
            "merge_edge_iou":                          0.6,
            "merge_edge_pixels":                       192,
        },
    }],

    "polygonization_args": {
        "layer_name":         "fields",
        "override_if_exists": True,
    },

    "filtering_args": {
        "automatic_area_scale":                  True,
        "minimum_area_m2":                       2500,   # ~0.25 ha minimum field size
        "minimum_part_area_m2":                  0,
        "minimum_hole_area_m2":                  2500,
        "minimum_background_field_area_m2":      50000,
        "minimum_background_field_hole_area_m2": 25000,
        "middleground_offset":                   None,
        "minimum_middleground_field_area_m2":    10000,
        "minimum_middleground_field_hole_area_m2": 5000,
    },

    "simplification_args": {
        "simplify":          False,
        "epsilon_scale":     2,
        "num_workers":       -1,
        "raster_resolution": -1,
    },
}


def main():
    image_dir  = DATA_IMAGES
    image_path = image_dir / f"{AREA_NAME}.tif"
    mask_path  = DATA_MASKS / f"{AREA_NAME}.tif"
    png_path   = image_dir / f"{AREA_NAME}_preview.png" if SAVE_PNG else None

    image_dir.mkdir(parents=True, exist_ok=True)
    DATA_MASKS.mkdir(parents=True, exist_ok=True)
    DATA_OUT.mkdir(parents=True, exist_ok=True)
    DATA_TEMP.mkdir(parents=True, exist_ok=True)

    # -------------------------------------------------------------------------
    # 1. Load AOI
    # -------------------------------------------------------------------------
    print(f"\n[1/4] Loading AOI: {AOI_PATH}")
    bbox, aoi_geom = load_aoi(
        Path(AOI_PATH),
        filter_field='NAME_3',
        filter_value='Dinan',
    )
    print(f"      Bounds (WGS84): {[round(v, 4) for v in bbox]}")

    # -------------------------------------------------------------------------
    # 2. Download Sentinel-2 RGB
    # -------------------------------------------------------------------------
    print(f"\n[2/4] Sentinel-2 RGB → {image_path}")
    print(f"      Date range : {DATE_RANGE}  |  Max cloud: {MAX_CLOUD}%")
    download_rgb_geotiff(
        aoi_geom=aoi_geom,
        datetime_range=f"{DATE_RANGE.split('/')[0]}T00:00:00Z/{DATE_RANGE.split('/')[1]}T23:59:59Z",
        max_cloud=MAX_CLOUD,
        output_path=image_path,
        output_png=png_path,
        png_title=f"Sentinel-2 RGB — {AREA_NAME}",
    )

    # -------------------------------------------------------------------------
    # 3. Download ESA WorldCover LULC mask (optional)
    # -------------------------------------------------------------------------
    if DOWNLOAD_LULC:
        print(f"\n[3/4] ESA WorldCover {LULC_YEAR} → {mask_path}")
        download_esa_worldcover(
            aoi_geom=aoi_geom,
            output_path=mask_path,
            year=LULC_YEAR,
        )
        effective_mask = str(mask_path) if mask_path.exists() else None
    else:
        print(f"\n[3/4] Skipping LULC mask  (DOWNLOAD_LULC = False)")
        effective_mask = None

    # -------------------------------------------------------------------------
    # 4. Run delineation
    # -------------------------------------------------------------------------
    print(f"\n[4/4] Delineating fields for '{AREA_NAME}' ...")
    output_gpkg = DATA_OUT / f"{AREA_NAME}.gpkg"

    delineate(
        args={
            "config": {
                **DELINEATION_CONFIG,
                "execution_args": {
                    "src_folder":    str(image_dir),
                    "temp_folder":   str(DATA_TEMP),
                    "output_path":   str(output_gpkg),
                    "keep_temp":     KEEP_TEMP,
                    "mask_filepath": effective_mask,
                },
            },
            "input":     str(image_dir),
            "output":    str(output_gpkg),
            "temp":      str(DATA_TEMP),
            "keep_temp": KEEP_TEMP,
            "mask":      effective_mask,
        },
        verbose=False,
    )

    # -------------------------------------------------------------------------
    # Done
    # -------------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("Done.")
    print(f"  Raw output  : {output_gpkg}")
    print(f"  Simplified  : {DATA_OUT / (AREA_NAME + '.simp.gpkg')}")
    if SAVE_PNG and png_path:
        print(f"  Preview PNG : {png_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
