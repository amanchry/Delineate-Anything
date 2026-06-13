# Delineate Anything — Local Run Guide
### My Setup: Sentinel-2 (10m) | Apple Silicon Mac | conda env: condaml

---

## Overview

**Input:** A GeoJSON file with the boundary of your area of interest (one polygon/feature).

**What the pipeline does:**
1. Downloads the best cloud-free Sentinel-2 RGB tile for your area (Planetary Computer STAC — free, no account needed)
2. Optionally downloads the ESA WorldCover land cover mask for your area
3. Runs the YOLO-based field boundary delineation model
4. Outputs field polygons as a GeoPackage (`.gpkg`)

---

## Project Structure

```
Delineate-Anything/
  run_pipeline.py          ← MAIN SCRIPT — runs the full workflow
  delineate.py             ← delineation engine (called by run_pipeline.py)
  shift.py                 ← post-process: fix spatial offset
  simplify.py              ← post-process: re-run simplification standalone
  conf_sample.yaml         ← delineation config (bands, thresholds, filters)
  batch_sample.yaml        ← batch config template
  download/
    sentinel2_rgb_stac.py  ← downloads Sentinel-2 RGB via Planetary Computer
    esa_lulc.py            ← downloads ESA WorldCover LULC mask
  src/
    visualization.py       ← PNG preview helper (used by sentinel2_rgb_stac)
  data/
    images/
      <AreaName>/          ← Sentinel-2 GeoTIFF saved here automatically
    masks/
      <AreaName>.tif       ← ESA WorldCover mask saved here automatically
    delineated/            ← output .gpkg files saved here
    temp/                  ← temporary files (auto-deleted after run)
```

---

## Step 1 — Activate Environment

```bash
conda activate condaml
cd /Volumes/ExternalSSD/Field_Boundary_\ Delineation/Delineate-Anything
```

---

## Step 2 — Prepare Your GeoJSON Boundary

Your input is a **GeoJSON file** with a single polygon or multipolygon of your area.

Requirements:
- CRS must be **WGS84 (EPSG:4326)**
- Can be a FeatureCollection, Feature, Polygon, or MultiPolygon
- One geometry is used (first feature if multiple exist)

Example `myarea.geojson`:
```json
{
  "type": "Feature",
  "geometry": {
    "type": "Polygon",
    "coordinates": [[[73.0, 30.0], [74.0, 30.0], [74.0, 31.0], [73.0, 31.0], [73.0, 30.0]]]
  },
  "properties": {}
}
```

You can export a boundary from QGIS → right-click layer → Export → Save Features As → GeoJSON (EPSG:4326).

---

## Step 3 — Run the Full Pipeline

### Standard run (with ESA WorldCover land cover mask):
```bash
python run_pipeline.py --aoi myarea.geojson --name MyArea
```

### Without land cover mask (faster, no filtering of non-agricultural areas):
```bash
python run_pipeline.py --aoi myarea.geojson --name MyArea --no-mask
```

### With custom date range and cloud threshold:
```bash
python run_pipeline.py \
  --aoi myarea.geojson \
  --name MyArea \
  --date-range 2023-04-01/2023-09-30 \
  --max-cloud 15
```

### Save a preview PNG of the downloaded Sentinel-2 image:
```bash
python run_pipeline.py --aoi myarea.geojson --name MyArea --save-png
```

### Skip downloads (data already exists, just re-run delineation):
```bash
python run_pipeline.py --aoi myarea.geojson --name MyArea --skip-download
```

---

## All Flags

| Flag | Default | Description |
|---|---|---|
| `--aoi` | required | Path to your GeoJSON boundary file |
| `--name` | required | Short name for the area (used as folder/file name) |
| `--date-range` | `2023-04-01/2023-10-31` | Sentinel-2 search date range |
| `--max-cloud` | `10.0` | Max cloud cover % for scene selection |
| `--lulc-year` | `2021` | ESA WorldCover year: `2020` or `2021` |
| `--no-mask` | off | Skip ESA WorldCover download — run without land cover filter |
| `--save-png` | off | Save a preview PNG of the downloaded Sentinel-2 image |
| `--keep-temp` | off | Keep temporary files after delineation (for debugging) |
| `--skip-download` | off | Skip downloads, go straight to delineation |

---

## What the Pipeline Does Internally

```
run_pipeline.py
│
├── [1/4] Load AOI from GeoJSON
│         → shapely geometry (WGS84)
│
├── [2/4] Download Sentinel-2 RGB
│         → download/sentinel2_rgb_stac.py
│         → finds best (least-cloudy) scene per MGRS tile via Planetary Computer STAC
│         → mosaics tiles if AOI spans multiple
│         → clips exact boundary, masks nodata
│         → saves: data/images/<Name>/<Name>.tif  (3-band uint16 RGB)
│         → saves: data/images/<Name>/<Name>.json (scene metadata)
│         → saves: data/images/<Name>/<Name>_preview.png (if --save-png)
│
├── [3/4] Download ESA WorldCover LULC  [skipped with --no-mask]
│         → download/esa_lulc.py
│         → downloads all intersecting tiles from Planetary Computer STAC
│         → mosaics and clips to boundary
│         → saves: data/masks/<Name>.tif  (single-band uint8, class values 10-100)
│
└── [4/4] Run delineation
          → writes batch_<Name>.yaml config
          → calls: python delineate.py -b batch_<Name>.yaml
          → output: data/delineated/<Name>.gpkg
          → output: data/delineated/<Name>.simp.gpkg
```

---

## Input Data Explained

### Sentinel-2 GeoTIFF (`data/images/<Name>/<Name>.tif`)

| Property | Value |
|---|---|
| Bands | 3 (Band 1 = Red/B04, Band 2 = Green/B03, Band 3 = Blue/B02) |
| Data type | uint16 (values 0–10000, surface reflectance × 10000) |
| Resolution | 10m |
| Nodata | 0 (black pixels) |
| CRS | UTM zone of the area (auto from Sentinel-2) |
| Config setting | `bands: [1, 2, 3]` — already in correct R,G,B order |

### ESA WorldCover Mask (`data/masks/<Name>.tif`)

| Class Value | Land Cover | Action in delineation |
|---|---|---|
| 10 | Tree cover | `filter_classes` — remove detections here |
| 20 | Shrubland | `filter_classes` |
| 30 | Grassland | keep (could be agricultural) |
| **40** | **Cropland** | **keep — this is the target** |
| 50 | Built-up | `filter_classes` — remove detections here |
| 60 | Bare/sparse vegetation | keep or filter |
| 70 | Snow and ice | `filter_classes` |
| 80 | Permanent water | `filter_classes` |
| 90 | Herbaceous wetland | `clip_classes` — trim edges |
| 95 | Mangroves | `filter_classes` |
| 100 | Moss and lichen | `filter_classes` |

These class filter settings are already configured in `conf_sample.yaml`:
```yaml
mask_info:
  range: 101
  filter_classes: [10, 20, 50, 70, 80, 95, 100]
  clip_classes: [90]
```

---

## Step 4 — View Output in QGIS

1. Open QGIS
2. Drag and drop `data/delineated/<Name>.simp.gpkg` onto the map
3. Use the `fields` layer
4. Style by `area` field to visualize field sizes

**Output fields:**

| Field | Type | Description |
|---|---|---|
| `id` | Integer | Unique field ID |
| `bg` | Boolean | `0` = model-detected, `1` = background fill from LCLU |
| `area` | Float | Field area in square meters (divide by 10000 for hectares) |

---

## Step 5 — Optional Post-processing

### Fix spatial offset (if fields look shifted in QGIS)
```bash
python shift.py \
  -i data/delineated/MyArea.gpkg \
  -o data/delineated/MyArea.shifted.gpkg \
  -s data/images/MyArea/MyArea.tif \
  -x 1 -y -1
```

### Re-run simplification with different smoothing
```bash
# Edit simp_sample.yaml first: set src/dst paths and adjust epsilon_scale
python simplify.py -c simp_sample.yaml
```

### Export to other formats
```bash
# GeoJSON
ogr2ogr -f GeoJSON data/delineated/MyArea.geojson data/delineated/MyArea.simp.gpkg fields

# Shapefile
ogr2ogr -f "ESRI Shapefile" data/delineated/MyArea_shp/ data/delineated/MyArea.simp.gpkg fields
```

---

## Common Issues

| Problem | Cause | Fix |
|---|---|---|
| `No scenes found` | Date range or cloud threshold too strict | Widen `--date-range` or increase `--max-cloud` |
| `Incompatible tiff files` | Mixed CRS or pixel sizes between tiles | Use `treat_as_vrt: true` in `conf_sample.yaml` |
| No output polygons | All pixels are nodata | Check `nodata_value` in conf — set to `null` if unsure |
| Fields over water/cities | No mask or wrong classes | Run without `--no-mask` and verify ESA WorldCover downloaded |
| Slow on CPU | MPS not detected | Run `python -c "import torch; print(torch.backends.mps.is_available())"` |
| Memory error | Region too large for RAM | Set `region_width: 8192` and `region_height: 8192` in `conf_sample.yaml` |

---

## File-wise Breakdown

### `run_pipeline.py` — Full Workflow Orchestrator
Takes a GeoJSON boundary → downloads data → runs delineation. One command runs everything.

### `delineate.py` — Delineation Engine
Called internally by `run_pipeline.py`. Can also be called directly with a batch YAML config.

### `download/sentinel2_rgb_stac.py` — Sentinel-2 Downloader
Connects to Microsoft Planetary Computer STAC. Finds the least-cloudy scene per MGRS tile, reads B04/B03/B02 bands via COG streaming (no full .SAFE download), mosaics and clips to AOI.

### `download/esa_lulc.py` — ESA WorldCover Downloader
Downloads ESA WorldCover LULC tiles from Planetary Computer, mosaics them, clips to AOI. Supports 2020 and 2021 versions.

### `src/visualization.py` — Preview Helper
Used by `sentinel2_rgb_stac.py`. Renders a 3-band GeoTIFF as a PNG with percentile stretch, boundary overlay (red line), and scene metadata caption.

### `shift.py` — Geometry Shifter
Post-processing tool to fix systematic spatial offset between detected boundaries and the source image. Runs one worker per CPU core.

### `simplify.py` — Standalone Simplification CLI
Re-run geometry simplification on an existing `.gpkg` without re-running inference.

### Config Files

| File | Purpose |
|---|---|
| `conf_sample.yaml` | Full inference config — model, bands, thresholds, area filters, simplification |
| `batch_sample.yaml` | Batch config template (auto-generated by `run_pipeline.py`) |
| `simp_sample.yaml` | Config for standalone `simplify.py` |
| `delineation_config_guide.md` | Full parameter reference |
