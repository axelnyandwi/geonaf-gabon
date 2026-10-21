"""Configuration générale et chemins du projet."""

import os
from pathlib import Path
from dotenv import load_dotenv

APP_TITLE = "GEONAF"
TAGLINE = "Observer · Comprendre · Prédire · Simuler"
PROJECT_STATUS = "Le projet est actuellement en phase de recherche et de conception."

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")
_data_directory = os.environ.get("GABON_GEOAI_DATA_DIR", "data").strip() or "data"
_data_path = Path(_data_directory).expanduser()
DATA_DIR = (_data_path if _data_path.is_absolute() else PROJECT_ROOT / _data_path).resolve()
RAW_DATA_DIR = DATA_DIR / "raw"
INTERIM_DATA_DIR = DATA_DIR / "interim"
PROCESSED_DATA_DIR = DATA_DIR / "processed"
# Copernicus Data Space Ecosystem - S3

from src.network_config import STAC_API_URL as DEFAULT_STAC_ENDPOINT
from src.network_config import CDSE_S3_ENDPOINT as DEFAULT_S3_ENDPOINT

# Validation and TLS initialization happen only when a network client is used.
STAC_API_URL = os.getenv("GEONAF_STAC_ENDPOINT", DEFAULT_STAC_ENDPOINT)
CDSE_S3_ENDPOINT = os.getenv("GEONAF_S3_ENDPOINT", DEFAULT_S3_ENDPOINT)
CDSE_S3_BUCKET = "eodata"

CDSE_S3_ACCESS_KEY = os.environ.get("CDSE_S3_ACCESS_KEY")
CDSE_S3_SECRET_KEY = os.environ.get("CDSE_S3_SECRET_KEY")
