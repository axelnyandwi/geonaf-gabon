"""Resolve selected STAC identities into immutable, bounded download plans."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import PurePosixPath
from urllib.parse import quote

from src.data import ingestion
from src.data.download_control import TransferAborted
from src.data.download_store import safe_id
from src.data.network import SecureStacIO
from src.data.stac_catalogue import _record
from src.network_config import NetworkSettings


@dataclass(frozen=True)
class DownloadPlan:
    identifier: str
    item_json: str
    roles_json: str
    provenance_json: str
    total_bytes: int

    @property
    def item(self):
        return json.loads(self.item_json)

    @property
    def roles(self):
        return json.loads(self.roles_json)


def fetch_selected_item(record):
    identifier = safe_id(record.stac_id)
    settings = NetworkSettings.from_environment()
    transport = SecureStacIO(settings)
    try:
        return transport.read_json(settings.stac_endpoint.rstrip("/") +
                                   "/collections/sentinel-2-l2a/items/" + quote(identifier, safe=""))
    finally:
        transport.session.close()


def make_plan(record, item, query):
    normalized = _record(item)
    identifier = safe_id(normalized.stac_id)
    if normalized.identity != record.identity or normalized.acquired_at_utc != record.acquired_at_utc:
        raise TransferAborted("L’identité ou la date STAC a changé ; relancer la recherche.")
    if not query.start <= normalized.acquired_at_utc.date() <= query.end:
        raise TransferAborted("Acquisition hors de la période sélectionnée.")
    properties = item["properties"]
    if (item.get("collection") != "sentinel-2-l2a" or
            str(properties.get("processing:level", "")).upper() not in ("L2", "L2A", "LEVEL-2A")):
        raise TransferAborted("Niveau de traitement Sentinel-2 L2A absent ou incompatible.")
    assets = item["assets"]
    roles = {}
    aliases = {"b04": ("B04_10m", "B04"), "b08": ("B08_10m", "B08"),
               "scl": ("SCL_20m", "SCL"), "calibration_xml": ("product_metadata", "calibration_xml")}
    for role, keys in aliases.items():
        candidates = [key for key in keys if key in assets]
        if not candidates:
            if role == "calibration_xml":
                candidates = [key for key, asset in assets.items()
                              if PurePosixPath(str(asset.get("href", ""))).name == "MTD_MSIL2A.xml"]
            else:
                band = {"b04": "B04", "b08": "B08", "scl": "SCL"}[role]
                candidates = [key for key, asset in assets.items()
                              if any(b.get("name") == band for b in asset.get("eo:bands", [])
                                     if isinstance(b, dict))
                              and asset.get("gsd", properties.get("gsd")) == (20 if role == "scl" else 10)]
        if len(candidates) != 1:
            raise TransferAborted(f"Asset requis absent ou ambigu : {role}.")
        roles[role] = candidates[0]
    required = {key: assets[key] for key in roles.values()}
    try:
        xml_checksum = ingestion._stac_checksum(assets[roles["calibration_xml"]])
    except ValueError:
        xml_checksum = None
    evidence_original_key = None
    if xml_checksum is None:
        candidates = [key for key, asset in assets.items() if key == "safe_manifest" or
                      PurePosixPath(str(asset.get("href", ""))).name == "manifest.safe"]
        if len(candidates) != 1:
            raise TransferAborted("Preuve SAFE absente ou ambiguë pour le XML.")
        evidence_original_key = candidates[0]
        required["safe_manifest"] = assets[evidence_original_key]
    names = []
    total = 0
    for key, asset in required.items():
        name = safe_id(PurePosixPath(ingestion.get_s3_key(asset)).name)
        if name.casefold() in ("acquisition_manifest.json", "download_provenance.json"):
            raise TransferAborted("Nom d’asset réservé à la provenance locale.")
        names.append(name.casefold())
        size = asset.get("file:size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            raise TransferAborted("Taille source absente ; estimation sûre impossible.")
        if key in (roles["calibration_xml"], "safe_manifest") and size > 1024**2:
            raise TransferAborted("Métadonnées supérieures au budget de validation XML/SAFE.")
        if key != roles["calibration_xml"] and ingestion._stac_checksum(asset) is None:
            raise TransferAborted("Checksum source requis absent.")
        total += size
    if len(set(names)) != len(names):
        raise TransferAborted("Collision de noms entre actifs requis.")
    selected_item = {"id": identifier, "collection": "sentinel-2-l2a",
                     "properties": properties, "assets": required}
    provenance = {"schema_version": "geonaf-download-v1", "query_fingerprint": query.fingerprint,
                  "aoi_id": query.aoi.aoi_id, "aoi_sha256": query.aoi.sha256,
                  "aoi_geometry": json.loads(query.aoi.geometry_json),
                  "period_utc": [query.start.isoformat(), query.end.isoformat()],
                  "profile_identity": query.profile_identity,
                  "resolved_at_utc": datetime.now(timezone.utc).isoformat(),
                  "safe_manifest_original_asset_id": evidence_original_key,
                  "item": selected_item}
    return DownloadPlan(identifier, json.dumps(selected_item, sort_keys=True), json.dumps(roles, sort_keys=True),
                        json.dumps(provenance, sort_keys=True), total)
