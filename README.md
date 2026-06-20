# Delineate Anything: Resolution-Agnostic Field Boundary Delineation on Satellite Imagery
<a href='https://lavreniuk.github.io/Delineate-Anything/'><img src='https://img.shields.io/badge/Project-Page-Green'></a>
<a href='https://arxiv.org/abs/2504.02534'><img src='https://img.shields.io/badge/Paper-DelAny-red'></a>
<a href='https://arxiv.org/abs/2511.13417'><img src='https://img.shields.io/badge/Paper-DelAnyFlow-red'></a>
<a href='https://delineate-anything.projects.earthengine.app/view/ua2024fields'><img src='https://img.shields.io/badge/Map-UA_Fields_2024-blue'></a>
<a href='https://huggingface.co/datasets/MykolaL/FBIS-22M'><img src='https://img.shields.io/badge/Dataset-HuggingFace-DA0000'></a>
<a href='https://colab.research.google.com/drive/10KSLwYDTgU-WhpqqG39yyvB6K8MdB0X9?usp=sharing'><img src='https://img.shields.io/badge/Colab-Demo-F9AB00'></a>

<p align="center">
  <img src="figs/logo.jpg" alt="intro" width="448"/>
</p>


by [Mykola Lavreniuk](https://scholar.google.com/citations?hl=en&user=-oFR-RYAAAAJ), [Nataliia Kussul](https://scholar.google.com/citations?user=e3TWBuwAAAAJ&hl=en), [Andrii Shelestov](https://scholar.google.com/citations?user=tqoQKZAAAAAJ&hl=en), [Bohdan Yailymov](https://scholar.google.com/citations?user=XaN-oukAAAAJ&hl=en), [Yevhenii Salii](https://scholar.google.com/citations?user=4jgAsBIAAAAJ&hl=en), [Volodymyr Kuzin](https://www.researchgate.net/profile/Volodymyr-Kuzin), [Zoltan Szantoi](https://scholar.google.com/citations?user=P_pyhi8AAAAJ&hl=en)

**Delineate Anything** is a resolution-agnostic deep learning framework for accurate agricultural field boundary detection from satellite imagery. Trained on the 22M+ instances in the FBIS-22M dataset, Delineate Anything sets a new SOTA by accurately delineating individual agricultural field boundaries across diverse satellite resolutions and geographic regions.

![intro](figs/intro.jpg)


## 🔗 Pre-trained Models

| Method                 | mAP@0.5 | mAP@0.5:0.95 | Latency (ms) | Size     | Download |
|------------------------|---------|--------------|--------------|----------|----------|
| **Delineate Anything S** | 0.632   | 0.383        | 16.8         | 17.6 MB  | [Download](https://huggingface.co/MykolaL/DelineateAnything/resolve/main/DelineateAnything-S.pt?download=true) |
| **Delineate Anything**   | 0.720   | 0.477        | 25.0         | 125 MB   | [Download](https://huggingface.co/MykolaL/DelineateAnything/resolve/main/DelineateAnything.pt?download=true) |

## ⚙️ Environment Setup

**Linux:**
```bash
mkdir -p ~/miniconda3
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh -O ~/miniconda3/miniconda.sh
bash ~/miniconda3/miniconda.sh -b -u -p ~/miniconda3
rm ~/miniconda3/miniconda.sh

source ~/miniconda3/bin/activate
conda install -c conda-forge gdal
pip install -r requirements.txt
```

**Windows:**
```bash
conda create --prefix=./.conda python=3.11
conda activate ./.conda
conda install -c conda-forge gdal
pip install -r requirements.txt
```

**macOS (Apple Silicon):**
```bash
conda activate <your-env>
conda install -c conda-forge gdal
pip install -r requirements.txt
```

---

## 🚀 Running the Pipeline

There are two ways to run depending on whether you want automatic data download or bring your own imagery.

---

### Option A — Full Automated Workflow via `main.py`

Use this if you want to **automatically download Sentinel-2 imagery and an ESA WorldCover land cover mask** for any region, then delineate field boundaries — all in one command.

**1. Edit `main.py` — set your region and options at the top of the file:**

```python
# --- Input boundary ---
AOI_PATH     = "data/boundaries/myarea.geojson"  # GeoJSON or Shapefile of your area
AREA_NAME    = "MyArea"                          # name used for output files

# If using a multi-feature admin shapefile, filter to one region:
FILTER_FIELD = "NAME_3"   # attribute column  (set None for plain GeoJSON)
FILTER_VALUE = "Dinan"    # value to match    (set None for plain GeoJSON)

# --- Sentinel-2 download ---
DATE_RANGE   = "2023-04-01/2023-10-31"
MAX_CLOUD    = 10.0        # max cloud cover %

# --- Land cover mask ---
DOWNLOAD_LULC = True       # False = skip mask, delineate without land cover filter
LULC_YEAR     = 2021       # 2020 or 2021

# --- Model ---
MODEL = "large"            # "large" (accurate) or "small" (faster)
```

**2. Run:**

```bash
python main.py
```

**What happens:**
- Downloads the best cloud-free Sentinel-2 RGB tile for your AOI → `data/images/<AREA_NAME>/`
- Downloads ESA WorldCover land cover mask → `data/masks/<AREA_NAME>.tif` _(if `DOWNLOAD_LULC = True`)_
- Runs field boundary delineation
- Saves output to `data/delineated/<AREA_NAME>.gpkg` and `<AREA_NAME>.simp.gpkg`

> Downloads are skipped automatically on re-runs if the files already exist.

---

### Option B — Bring Your Own RGB Image

Use this if you already have a GeoTIFF (from any satellite source) and want to run delineation directly.

**1. Place your image(s) in a named subfolder:**

```
data/
  images/
    MyArea/
      image.tif        ← your RGB GeoTIFF (one or more tiles, same CRS and pixel size)
  masks/
    MyArea.tif         ← optional: land cover mask (ESA WorldCover or similar)
```

**2. Edit `batch_sample.yaml`:**

```yaml
base_config: conf_sample.yaml

data_root:   data/images
output_root: data/delineated
temp_root:   data/temp
mask_root:   data/masks
keep_temp:   false

include:
  - MyArea        # must match your subfolder name

exclude: null
override: null
```

**3. Edit `conf_sample.yaml` — set your band order:**

```yaml
data_loader:
  bands: [3, 2, 1]        # adjust to match RGB bands in your image (GDAL 1-based index)
  nodata_value: [0, 0, 0] # black pixels = nodata
```

**4. Run:**

```bash
python delineate.py -b batch_sample.yaml
```

Output is saved to `data/delineated/MyArea.gpkg` and `MyArea.simp.gpkg`.

---

### Optional Post-processing

**Fix spatial offset** (if output polygons look shifted in QGIS):
```bash
python shift.py -i data/delineated/MyArea.gpkg -o data/delineated/MyArea.shifted.gpkg \
  -s data/images/MyArea/image.tif -x 1 -y -1
```

**Re-run simplification standalone** (without re-running inference):
```bash
# Edit simp_sample.yaml to set src/dst paths, then:
python simplify.py -c simp_sample.yaml
```

ℹ️ For a full parameter reference see [delineation_config_guide.md](delineation_config_guide.md)


## License
This project is licensed under the AGPL-3.0 License.

## Acknowledgements
This code is based on [Ultralytics](https://github.com/ultralytics/ultralytics).

## Citation
If you find our work useful in your research, please consider citing it:
```
@article{lavreniuk2025delineateanything,
      title={Delineate Anything: Resolution-Agnostic Field Boundary Delineation on Satellite Imagery}, 
      author={Mykola Lavreniuk and Nataliia Kussul and Andrii Shelestov and Bohdan Yailymov and Yevhenii Salii and Volodymyr Kuzin and Zoltan Szantoi},
      year={2025},
      journal={arXiv preprint arXiv:2504.02534},
}

@article{lavreniuk2025delineateanythingflow,
      title={Delineate Anything Flow: Fast, Country-Level Field Boundary Detection from Any Source}, 
      author={Mykola Lavreniuk and Nataliia Kussul and Andrii Shelestov and Yevhenii Salii and Volodymyr Kuzin and Sergii Skakun and Zoltan Szantoi},
      year={2025},
      journal={https://arxiv.org/abs/2511.13417},
}
```
