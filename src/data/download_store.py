"""Local staging, OS-released product locks and atomic publication.

The store interface is deliberately path-free for callers of the queue. A future
object store adapter must provide equivalent validation/publication guarantees.
"""

from contextlib import contextmanager, suppress
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from src.data import ingestion
from src.data.download_control import TransferAborted


def safe_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,190}", value)
            or value.endswith(".") or value.split(".")[0].upper() in
            {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}):
        raise TransferAborted("Identifiant de stockage non sûr.")
    return value


# Windows only: an antivirus or indexer may briefly hold a file that was just replaced, and
# the next os.replace then fails with PermissionError (WinError 5). Retry a few times with a
# bounded total wait (≈ 0.4 s); a lasting refusal is still raised. On POSIX a rename
# PermissionError is permanent, so it is never retried.
_REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.25) if os.name == "nt" else ()


def _replace(source, destination):
    for retry, delay in enumerate((*_REPLACE_RETRY_DELAYS, None)):
        try:
            os.replace(source, destination)
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)
        else:
            if retry:
                logging.getLogger(__name__).warning("atomic_json_replace_retried retries=%d", retry)
            return


def atomic_json(destination, value):
    """Write JSON atomically: readers see the previous complete file or the new one.

    On any failure the destination is left untouched, the temporary file is removed and
    the original error is raised.
    """
    temporary = destination.with_name(destination.name + "." + uuid4().hex + ".tmp")
    payload = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True)
    try:
        with temporary.open("x", encoding="utf-8") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        _replace(temporary, destination)
    except BaseException:
        with suppress(OSError):  # The original error, not a cleanup failure, is reported.
            temporary.unlink(missing_ok=True)
        raise


# Longest temporary suffix: ".<uuid4 hex>.part" (ours) + ".<8 chars>" (s3transfer).
_TEMPORARY_SUFFIX = 1 + 32 + 5 + 9


class LocalDownloadStore:
    def __init__(self, data_dir, *, manifest_reader=None, observation_validator=None,
                 max_path=259 if os.name == "nt" else 4095):
        self._read_manifest = manifest_reader
        self._verify_observation = observation_validator
        # Windows without LongPathsEnabled rejects paths over MAX_PATH (260 incl. NUL).
        self.max_path = max_path
        self.root = Path(data_dir).resolve()
        self.work = self._confined(self.root / "downloads")
        self.final = self._confined(self.root / "raw" / "sentinel2")
        self.work.mkdir(parents=True, exist_ok=True)
        self.final.mkdir(parents=True, exist_ok=True)

    def _confined(self, path):
        if not path.resolve().is_relative_to(self.root):
            raise TransferAborted("Chemin de stockage hors du répertoire autorisé.")
        for ancestor in (path, *path.parents):
            if ancestor == self.root:
                break
            if ancestor.is_symlink():
                raise TransferAborted("Lien symbolique interdit dans le stockage des téléchargements.")
        return path

    def _stage_path(self, identifier):
        # Ingestion writes into <parent>/<product id>; a short hashed parent keeps
        # temporary names under Windows MAX_PATH (the full id appears only once).
        key = hashlib.sha256(safe_id(identifier).encode("ascii")).hexdigest()[:16]
        return self._confined(self.work / "staging" / key / identifier)

    def stage(self, identifier):
        folder = self._stage_path(identifier)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def check_paths(self, plan):
        """Reject before any transfer when a staging or final path cannot be created."""
        staging, final = self._stage_path(plan.identifier), self.final / plan.identifier
        names = [Path(ingestion.get_s3_key(asset)).name for asset in plan.item["assets"].values()]
        names += ["acquisition_manifest.json", "download_provenance.json"]
        longest = max(len(str(staging / name)) + _TEMPORARY_SUFFIX for name in names)
        longest = max(longest, *(len(str(final / name)) for name in names))
        if longest > self.max_path:
            raise TransferAborted(f"Chemin local trop long ({longest} > {self.max_path} caractères) ; "
                                  "rapprocher le dossier de données de la racine ou activer les chemins longs.")

    @contextmanager
    def lock(self, identifier):
        path = self._confined(self.work / "locks" / (safe_id(identifier) + ".lock"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as stream:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    stream.write(b"0")
                    stream.flush()
                    stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise TransferAborted("Acquisition déjà verrouillée par un autre téléchargement.") from None
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    raise TransferAborted("Acquisition déjà verrouillée par un autre téléchargement.") from None
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)

    def validate(self, folder, plan):
        self._confined(folder)
        path = folder / "acquisition_manifest.json"
        if path.stat().st_size > 2 * 1024**2:
            raise TransferAborted("Manifeste local trop volumineux.")
        manifest = self._read_manifest(path)
        item = plan.item
        if (manifest["product_id"] != plan.identifier or
                datetime.fromisoformat(manifest["acquired_at_utc"].replace("Z", "+00:00")) !=
                datetime.fromisoformat(item["properties"]["datetime"].replace("Z", "+00:00"))):
            raise TransferAborted("Identité locale différente de la sélection STAC.")
        for record in manifest["assets"].values():
            self._confined(folder / record["path"])
        # Reuse the scientific consumer: full SHA-256, grids, BOA and identities.
        self._verify_observation(path, manifest)
        for role, key in plan.roles.items():
            record = manifest["assets"][role]
            try:
                expected = ingestion._stac_checksum(item["assets"][key])
            except ValueError:
                if role != "calibration_xml":
                    raise
                expected = None
            if expected is not None and ingestion._file_checksum(folder / record["path"], expected[0]) != expected[1]:
                raise TransferAborted("Checksum source différent du produit local.")
        evidence = manifest.get("integrity", {}).get("evidence", {}).get("safe_manifest")
        try:
            xml_declared = ingestion._stac_checksum(item["assets"][plan.roles["calibration_xml"]])
        except ValueError:
            xml_declared = None
        if xml_declared is None and not evidence:
            # Without a STAC checksum, only a verified SAFE proof binds the XML to its source.
            raise TransferAborted("XML de calibration sans checksum ni preuve SAFE ; publication refusée.")
        if evidence:
            proof = self._confined(folder / evidence["path"])
            if ingestion._file_sha256(proof) != evidence["sha256"]:
                raise TransferAborted("Preuve SAFE locale corrompue.")
            safe = item["assets"].get("safe_manifest")
            checksum = ingestion._stac_checksum(safe or {})
            if checksum is None or ingestion._file_checksum(proof, checksum[0]) != checksum[1]:
                raise TransferAborted("Preuve SAFE différente de la source actuelle.")
            xml = folder / manifest["assets"]["calibration_xml"]["path"]
            algorithm, digest = ingestion._safe_manifest_component_checksum(proof, item["assets"][plan.roles["calibration_xml"]])
            if ingestion._file_checksum(xml, algorithm) != digest:
                raise TransferAborted("XML différent de la preuve SAFE.")
        return manifest

    def existing(self, plan):
        candidates = []
        expected = self._confined(self.final / plan.identifier)
        if expected.exists():
            candidates.append(expected)
        for path in self.final.glob("*/acquisition_manifest.json"):
            if path.parent == expected:
                continue
            try:
                self._confined(path)
                if path.stat().st_size <= 2 * 1024**2 and self._read_manifest(path)["product_id"] == plan.identifier:
                    candidates.append(path.parent)
            except (ValueError, OSError):
                continue
        if len(candidates) > 1:
            raise TransferAborted("Identité locale ambiguë ; produits existants conservés.")
        if candidates:
            try:
                self.validate(candidates[0], plan)
            except Exception:
                raise TransferAborted("Produit local invalide ; aucun écrasement. Vérifier ou isoler les fichiers avant reprise.") from None
            return True
        return False

    def prepare_stage(self, plan):
        folder = self.stage(plan.identifier)
        expected = {}
        for asset in plan.item["assets"].values():
            name = Path(ingestion.get_s3_key(asset)).name
            try:
                checksum = ingestion._stac_checksum(asset)
            except ValueError:
                checksum = None
            if checksum:
                expected[name] = checksum
        # Only our staging is reorganized. Existing scientific products are untouched.
        # Unknown/partial bytes are retained in quarantine and count toward quota.
        for path in tuple(folder.iterdir()):
            checksum = expected.get(path.name)
            if path.is_symlink() or not path.is_file():
                raise TransferAborted("Contenu de staging non sûr.")
            if path.name == "acquisition_manifest.json":
                try:
                    self.validate(folder, plan)
                    return folder
                except Exception:
                    pass
            if checksum is None or ingestion._file_checksum(path, checksum[0]) != checksum[1]:
                quarantine = self._confined(folder.parent / "quarantine")
                quarantine.mkdir(exist_ok=True)
                path.rename(quarantine / (uuid4().hex + "-" + path.name))
        return folder

    def check_space(self, budget, required=0):
        occupied = 0
        for path in self.work.rglob("*"):
            try:
                if path.is_file():
                    occupied += path.stat().st_size
            except FileNotFoundError:
                # Another worker may atomically publish its directory meanwhile.
                continue
        if occupied + required > budget.storage_bytes:
            raise TransferAborted("Budget de stockage temporaire insuffisant ; fichiers conservés.")
        if shutil.disk_usage(self.root).free < budget.reserve_bytes + required:
            raise TransferAborted("Espace disque insuffisant pour la réserve configurée.")

    def publish(self, folder, plan):
        self.validate(folder, plan)
        destination = self._confined(self.final / plan.identifier)
        if destination.exists():
            raise TransferAborted("Collision de produit ; aucun produit existant remplacé.")
        # Same filesystem; consumers only see a complete, validated directory.
        folder.rename(destination)

    def read_history(self):
        path = self._confined(self.work / "queue.json")
        if path.is_file() and path.stat().st_size <= 1024**2:
            return json.loads(path.read_text(encoding="utf-8"))
        return None

    def write_history(self, snapshot):
        atomic_json(self._confined(self.work / "queue.json"), snapshot)
