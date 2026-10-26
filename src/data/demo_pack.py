"""Download, verify and install the demonstration data pack (no Streamlit).

The pack is a ZIP archive hosted outside Git. Its SHA-256 is pinned: an
archive whose digest differs is never extracted. Extraction is streamed with
member, size and compression-ratio limits, rejects any path leaving the
staging directory, then every file is checked against ``PACK_MANIFEST.json``
(size and SHA-256, no unlisted file). The verified tree is moved into place
with one ``os.replace``: a reader sees either no pack or a complete one.

Public errors carry a category and a fixed message; they never include the
URL, local paths or the underlying exception text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
from urllib.parse import urlsplit
import uuid
import zipfile

PACK_MANIFEST = "PACK_MANIFEST.json"
INSTALL_MARKER = ".geonaf-install.json"
MAX_ARCHIVE_BYTES = 64 * 1024**2
MAX_EXTRACTED_BYTES = 160 * 1024**2
MAX_MEMBERS = 256
MAX_COMPRESSION_RATIO = 50
CHUNK = 1024 * 1024
# Release assets of github.com redirect to these hosts; any other host is refused.
ALLOWED_HOSTS = frozenset({"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"})
MANIFEST_NAMES = ("acquisition_manifest.json", "subset_manifest.json")

# Filled in only once an archive is published with the user's approval.
RELEASED_PACK: "DemoPackSpec | None" = None


class DemoPackError(RuntimeError):
    """Public failure: ``category`` in configuration, network, size, integrity, archive, storage."""

    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


@dataclass(frozen=True)
class DemoPackSpec:
    url: str
    sha256: str
    pack_id: str
    max_bytes: int = MAX_ARCHIVE_BYTES

    def __post_init__(self):
        parsed = urlsplit(self.url) if isinstance(self.url, str) else None
        if (parsed is None or parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise DemoPackError("configuration", "Adresse du paquet de démonstration non autorisée.")
        if not _is_digest(self.sha256):
            raise DemoPackError("configuration", "Empreinte SHA-256 du paquet invalide.")
        if not isinstance(self.pack_id, str) or not self.pack_id.replace("-", "").isalnum():
            raise DemoPackError("configuration", "Identifiant du paquet invalide.")
        if not isinstance(self.max_bytes, int) or not 0 < self.max_bytes <= MAX_ARCHIVE_BYTES:
            raise DemoPackError("configuration", "Taille maximale du paquet invalide.")


@dataclass(frozen=True)
class DataSource:
    path: Path
    origin: str                 # "local", "demo_pack" or "none"
    pack: dict | None = None    # PACK_MANIFEST.json of an installed pack


def _is_digest(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def spec_from_environment(environ=os.environ) -> DemoPackSpec | None:
    """Pinned release, or GEONAF_DEMO_PACK_URL/_SHA256/_ID together (all three or none)."""
    values = [environ.get(f"GEONAF_DEMO_PACK_{name}", "").strip() for name in ("URL", "SHA256", "ID")]
    if not any(values):
        return RELEASED_PACK
    if not all(values):
        raise DemoPackError("configuration", "Configuration du paquet incomplète (adresse, empreinte, identifiant).")
    return DemoPackSpec(url=values[0], sha256=values[1].lower(), pack_id=values[2])


def default_cache_root(environ=os.environ) -> Path:
    configured = environ.get("GEONAF_CACHE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()
    base = environ.get("XDG_CACHE_HOME", "").strip()
    return (Path(base) if base else Path.home() / ".cache") / "geonaf"


def has_local_acquisitions(data_dir: Path) -> bool:
    root = Path(data_dir) / "raw" / "sentinel2"
    return any(path.is_file() for name in MANIFEST_NAMES for path in root.glob(f"*/{name}"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _default_session():
    from src.data.network import secure_session
    from src.network_config import NetworkSettings

    return secure_session(NetworkSettings.from_environment())


def download_archive(spec: DemoPackSpec, destination: Path, *, session=None,
                     timeout: tuple[float, float] = (10.0, 60.0)) -> str:
    """Stream the archive to ``destination``; size-bounded and digest-checked. Returns the digest."""
    session = session or _default_session()
    digest, received = hashlib.sha256(), 0
    try:
        with session.get(spec.url, stream=True, timeout=timeout, allow_redirects=True) as response:
            if urlsplit(response.url).hostname not in ALLOWED_HOSTS or not response.url.startswith("https://"):
                raise DemoPackError("network", "Redirection vers un hôte non autorisé.")
            if response.status_code != 200:
                raise DemoPackError("network", f"Téléchargement refusé (HTTP {int(response.status_code)}).")
            declared = response.headers.get("Content-Length")
            declared = int(declared) if declared and declared.isdigit() else None
            if declared is not None and declared > spec.max_bytes:
                raise DemoPackError("size", "Archive plus volumineuse que la limite autorisée.")
            with destination.open("wb") as target:
                for chunk in response.iter_content(CHUNK):
                    received += len(chunk)
                    if received > spec.max_bytes:
                        raise DemoPackError("size", "Archive plus volumineuse que la limite autorisée.")
                    digest.update(chunk)
                    target.write(chunk)
            if declared is not None and received != declared:
                raise DemoPackError("network", "Téléchargement interrompu (archive incomplète).")
    except DemoPackError:
        destination.unlink(missing_ok=True)
        raise
    except Exception:
        destination.unlink(missing_ok=True)
        raise DemoPackError("network", "Téléchargement du paquet de démonstration impossible.") from None
    if digest.hexdigest() != spec.sha256:
        destination.unlink(missing_ok=True)
        raise DemoPackError("integrity", "Empreinte de l'archive différente de l'empreinte attendue.")
    return digest.hexdigest()


def _member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or "\\" in name or name.startswith("/") or path.is_absolute() or ".." in path.parts
            or any(":" in part for part in path.parts) or name != path.as_posix()):
        raise DemoPackError("archive", "L'archive contient un chemin non autorisé.")
    return path


def safe_extract(archive: Path, staging: Path) -> None:
    """Extract regular files only, with streamed size accounting (headers are not trusted)."""
    try:
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_MEMBERS:
                raise DemoPackError("archive", "L'archive contient trop de fichiers.")
            names, total = set(), 0
            for member in members:
                path = _member_path(member.filename.rstrip("/") if member.is_dir() else member.filename)
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise DemoPackError("archive", "L'archive contient un lien ou un fichier spécial.")
                if path.as_posix().casefold() in names:
                    raise DemoPackError("archive", "L'archive contient des chemins en double.")
                names.add(path.as_posix().casefold())
                if member.is_dir():
                    continue
                if member.compress_size and member.file_size / member.compress_size > MAX_COMPRESSION_RATIO:
                    raise DemoPackError("archive", "Taux de compression anormal dans l'archive.")
                target = staging.joinpath(*path.parts)
                if not target.resolve().is_relative_to(staging.resolve()):
                    raise DemoPackError("archive", "L'archive contient un chemin non autorisé.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    for chunk in iter(lambda: source.read(CHUNK), b""):
                        total += len(chunk)
                        if total > MAX_EXTRACTED_BYTES:
                            raise DemoPackError("size", "Contenu décompressé plus volumineux que la limite.")
                        output.write(chunk)
    except DemoPackError:
        raise
    except Exception:
        raise DemoPackError("archive", "Archive illisible ou corrompue.") from None


def verify_pack(directory: Path, pack_id: str | None = None) -> dict:
    """Every listed file exists with its size and SHA-256; no unlisted file is accepted."""
    try:
        manifest = json.loads((directory / PACK_MANIFEST).read_text(encoding="utf-8"))
        files = manifest["files"]
        listed = {}
        for item in files:
            path = _member_path(item["path"])
            listed[path.as_posix()] = item
    except DemoPackError:
        raise
    except Exception:
        raise DemoPackError("integrity", "Manifeste du paquet absent ou illisible.") from None
    if pack_id is not None and manifest.get("pack_id") != pack_id:
        raise DemoPackError("integrity", "Identifiant du paquet différent de celui attendu.")
    present = {path.relative_to(directory).as_posix() for path in directory.rglob("*")
               if path.is_file() and path.name not in {PACK_MANIFEST, INSTALL_MARKER}}
    if present != set(listed):
        raise DemoPackError("integrity", "Fichiers du paquet manquants ou non déclarés.")
    for relative, item in listed.items():
        path = directory / relative
        if path.stat().st_size != item.get("bytes") or sha256_file(path) != item.get("sha256"):
            raise DemoPackError("integrity", "Un fichier du paquet ne correspond pas à son empreinte.")
    return manifest


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def install_pack(spec: DemoPackSpec, cache_root: Path, *, session=None, clock=None) -> tuple[Path, dict]:
    """Return the verified installation directory, downloading it when missing or invalid."""
    cache_root = Path(cache_root)
    final = cache_root / f"{spec.pack_id}-{spec.sha256[:16]}"
    if final.is_dir():
        try:
            marker = json.loads((final / INSTALL_MARKER).read_text(encoding="utf-8"))
            if marker.get("archive_sha256") == spec.sha256:
                return final, verify_pack(final, spec.pack_id)
        except (OSError, ValueError, DemoPackError):
            pass
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        archive, staging = cache_root / f".download-{token}.zip", cache_root / f".staging-{token}"
        staging.mkdir()
    except OSError:
        raise DemoPackError("storage", "Cache local du paquet inaccessible.") from None
    try:
        download_archive(spec, archive, session=session)
        safe_extract(archive, staging)
        manifest = verify_pack(staging, spec.pack_id)
        installed_at = (clock or (lambda: datetime.now(timezone.utc)))()
        (staging / INSTALL_MARKER).write_text(json.dumps({
            "archive_sha256": spec.sha256, "pack_id": spec.pack_id,
            "installed_at_utc": installed_at.isoformat().replace("+00:00", "Z")}), encoding="utf-8")
        if final.exists():  # Invalid or partial earlier install: set aside, then replace.
            os.replace(final, cache_root / f".stale-{token}")
        try:
            os.replace(staging, final)
        except OSError:
            # Another process installed the same archive meanwhile: keep it if it verifies.
            return final, verify_pack(final, spec.pack_id)
        return final, manifest
    except OSError:
        raise DemoPackError("storage", "Installation du paquet impossible dans le cache local.") from None
    finally:
        for leftover in (archive, staging, cache_root / f".stale-{token}"):
            _remove(leftover)


def create_archive(directory: Path, destination: Path) -> str:
    """Deterministic ZIP of a verified pack (sorted names, fixed timestamps). Returns its SHA-256."""
    verify_pack(directory)
    files = sorted(path for path in directory.rglob("*") if path.is_file() and path.name != INSTALL_MARKER)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(directory).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as source, bundle.open(info, "w") as target:
                shutil.copyfileobj(source, target, CHUNK)
    return sha256_file(destination)


def pack_summary(manifest: dict) -> list[str]:
    """Public provenance lines, built from the pack manifest only (no hard-coded results)."""
    lines = [f"Paquet de démonstration {manifest.get('pack_id')} v{manifest.get('pack_version')} — "
             f"{manifest.get('aoi', {}).get('statement', '')}"]
    sentinel2 = manifest.get("sentinel2", {})
    for item in sentinel2.get("acquisitions", []):
        quality = item.get("quality", {})
        status = ("exclue des tendances (couverture exploitable très faible)"
                  if quality.get("trend_use") == "excluded_low_coverage" else "utilisable, non validée")
        lines.append(f"Sentinel-2 acquis le {item['acquired_at_utc'][:10]} (ingéré le "
                     f"{item['ingested_at_utc'][:10]}) : couverture valide "
                     f"{quality.get('valid_coverage_percent', float('nan')):.2f} % — {status}.")
    lines.append(f"Extraits sans rééchantillonnage de tuiles {sentinel2.get('tile', '')} vérifiées. "
                 f"{sentinel2.get('attribution', '')}.")
    for layer in manifest.get("external_layers", []):
        license_ = layer.get("license", {})
        lines.append(f"{layer.get('product') or layer.get('source_id')} — {license_.get('attribution', '')} "
                     f"({license_.get('spdx_id', '')}).")
    boundaries = manifest.get("boundaries", {})
    if boundaries:
        lines.append(boundaries.get("statement", ""))
        for item in boundaries.get("releases", []):
            lines.append(f"geoBoundaries {item.get('release')} {item.get('level')} — {item.get('source')}, "
                         f"limites de {item.get('year_represented')}, {item.get('units')} unités "
                         f"({', '.join(item.get('legal_status', []))}) — {item.get('license')}.")
    lines += list(manifest.get("limits", []))
    return lines


def resolve_data_dir(configured: Path, *, spec: DemoPackSpec | None, cache_root: Path,
                     installer=install_pack) -> DataSource:
    """Local data take precedence; the pack is installed only when no local acquisition exists."""
    configured = Path(configured)
    if has_local_acquisitions(configured):
        pack = configured / PACK_MANIFEST
        if pack.is_file():  # GABON_GEOAI_DATA_DIR pointing to an unpacked pack.
            return DataSource(configured, "demo_pack", verify_pack(configured))
        return DataSource(configured, "local")
    if spec is None:
        return DataSource(configured, "none")
    path, manifest = installer(spec, cache_root)
    return DataSource(path, "demo_pack", manifest)
