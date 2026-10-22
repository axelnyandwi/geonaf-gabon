"""Bounded background orchestration. No Streamlit, credentials or UI callbacks.

Explicit prepare/start/cancel commands; snapshots are safe public metadata.
Restart never starts transfers: re-prepare selection and validate staging first.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from threading import Event, RLock
from time import perf_counter

from src.data import ingestion
from src.data.download_control import DownloadBudget, TransferControl, TransferAborted, TransferCancelled
from src.data.download_store import LocalDownloadStore, atomic_json, safe_id
from src.data.network import NetworkError
from src.services.download_plan import fetch_selected_item, make_plan
from src.services.local_preview import _manifest, _load_observation, LocalObservation


STATES = frozenset({"pending", "downloading", "completed", "failed", "cancelled", "skipped"})


def _validate_observation(path, manifest):
    acquired = datetime.fromisoformat(manifest["acquired_at_utc"].replace("Z", "+00:00"))
    _load_observation(LocalObservation(acquired, path, manifest["product_id"]))


class DownloadQueue:
    def __init__(self, data_dir, *, store=None, resolver=fetch_selected_item,
                 client_factory=ingestion.create_cdse_s3_client):
        self.store = store if store is not None else LocalDownloadStore(
            data_dir, manifest_reader=_manifest, observation_validator=_validate_observation)
        self.resolver, self.client_factory = resolver, client_factory
        self._lock = RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="geonaf-queue")
        self._future = None
        self._cancel = Event()
        self._rows, self._plans = {}, {}
        self.phase = "idle"
        self.error = None
        self.budget = DownloadBudget()
        self.control = TransferControl(self.budget, self._cancel)
        try:
            previous = self.store.read_history()
            if previous:
                count = len(previous.get("rows", []))
                if count:
                    self.error = (f"Historique de {count} acquisition(s). Réestimer la sélection pour vérifier "
                                  "les fichiers et reprendre ; aucun transfert repris automatiquement.")
                for row in previous.get("rows", [])[:16]:
                    identifier = safe_id(row["id"])
                    self._rows[identifier] = {"id": identifier, "state": "pending", "bytes": 0,
                                              "expected_bytes": 0, "seconds": 0,
                                              "error": "Historique à revérifier ; réestimer avant reprise."}
        except (OSError, ValueError, TypeError, KeyError):
            self.error = "Historique illisible ; réestimer la sélection avant reprise."

    @property
    def busy(self):
        return self._future is not None and not self._future.done()

    def snapshot(self):
        with self._lock:
            return {"phase": self.phase, "busy": self.busy, "error": self.error,
                    "network_bytes": self.control.transferred,
                    "rows": [dict(row) for row in self._rows.values()]}

    def _persist(self, *, final=False):
        with self._lock:
            snapshot = self.snapshot()
            if final:
                # Written from the worker itself, whose future is not done yet.
                snapshot["busy"] = False
            self.store.write_history(snapshot)

    def prepare(self, selection, budget):
        with self._lock:
            if self.busy:
                raise TransferAborted("Une opération est déjà en cours.")
            if not selection.records or len(selection.records) > 16:
                raise TransferAborted("Sélectionner entre 1 et 16 acquisitions par opération.")
            self.budget = budget
            self._cancel = Event()
            self.control = TransferControl(budget, self._cancel)
            self._rows, self._plans = {}, {}
            self.phase, self.error = "estimating", None
            for record in selection.records:
                self._rows.setdefault(record.stac_id, {"id": record.stac_id, "state": "pending",
                                                      "bytes": 0, "expected_bytes": 0,
                                                      "error": None, "seconds": 0})
            self._future = self._executor.submit(self._prepare, selection)

    def _prepare(self, selection):
        try:
            for record in selection.records:
                self.control.check()
                if record.stac_id in self._plans:
                    continue
                plan = make_plan(record, self.resolver(record), selection.query)
                self.store.check_paths(plan)
                with self.store.lock(plan.identifier):
                    local = self.store.existing(plan)
                with self._lock:
                    self._plans[plan.identifier] = plan
                    self._rows[plan.identifier].update(expected_bytes=plan.total_bytes,
                                                      state="skipped" if local else "pending")
                self._persist()
            required = sum(plan.total_bytes for identifier, plan in self._plans.items()
                           if self._rows[identifier]["state"] != "skipped")
            if required > self.budget.network_bytes:
                raise TransferAborted("Volume estimé supérieur au budget réseau.")
            self.store.check_space(self.budget, required)
            with self._lock:
                self.phase = "ready"
        except Exception as error:
            self._fail_pending(error)
        finally:
            self._persist(final=True)

    def _fail_pending(self, error):
        message = self._public_error(error)
        with self._lock:
            self.error, self.phase = message, "finished"
            for row in self._rows.values():
                if row["state"] in ("pending", "downloading"):
                    row.update(state="cancelled" if isinstance(error, TransferCancelled) else "failed", error=message)

    @staticmethod
    def _public_error(error):
        if isinstance(error, TransferAborted):
            return str(error)
        if isinstance(error, NetworkError):
            return (f"Réseau {error.category}, HTTP {error.status_code or '—'}. "
                    "Vérifier TLS, accès CDSE ou réessayer après temporisation.")
        return "Validation ou accès local impossible ; fichiers conservés. Vérifier actifs, intégrité et accès CDSE."

    def start(self):
        with self._lock:
            if self.busy or self.phase != "ready":
                raise TransferAborted("Estimation complète requise avant téléchargement.")
            self.phase = "running"
            self._future = self._executor.submit(self._run)

    def cancel(self):
        self._cancel.set()

    def _run(self):
        try:
            with ThreadPoolExecutor(max_workers=self.budget.workers, thread_name_prefix="geonaf-transfer") as workers:
                futures = [workers.submit(self._download, plan) for identifier, plan in self._plans.items()
                           if self._rows[identifier]["state"] == "pending"]
                for future in futures:
                    future.result()
        except Exception as error:
            self._fail_pending(error)
        finally:
            with self._lock:
                self.phase = "finished"
            self._persist(final=True)

    def _download(self, plan):
        started = perf_counter()
        client = None
        try:
            self.control.check()
            with self.store.lock(plan.identifier):
                if self.store.existing(plan):
                    with self._lock:
                        self._rows[plan.identifier]["state"] = "skipped"
                    return
                with self._lock:
                    self._rows[plan.identifier]["state"] = "downloading"
                self._persist()
                folder = self.store.prepare_stage(plan)
                # A validated manifest left after interruption only needs publication.
                if not (folder / "acquisition_manifest.json").is_file():
                    client = self.client_factory()
                    queue = self
                    class JobControl:
                        def check(self):
                            queue.control.check()
                            queue.store.check_space(queue.budget)

                        def consume(self, size):
                            self.check()
                            queue.control.consume(size)
                            with queue._lock:
                                queue._rows[plan.identifier]["bytes"] += max(0, size)
                    control = JobControl()
                    ingestion.ingest_sentinel2_product(
                        client, list(plan.item.get("bbox", (0, 0, 1, 1))), "", "", folder.parent,
                        product_id=plan.identifier, product_item=plan.item,
                        asset_names=list(plan.roles.values()), manifest_asset_roles=plan.roles,
                        source_integrity_mode="strict", control=control)
                self.control.check()
                provenance = folder / "download_provenance.json"
                atomic_json(provenance, json.loads(plan.provenance_json))
                self.store.publish(folder, plan)
                with self._lock:
                    self._rows[plan.identifier]["state"] = "completed"
        except Exception as error:
            with self._lock:
                self._rows[plan.identifier].update(
                    state="cancelled" if isinstance(error, TransferCancelled) else "failed",
                    error=self._public_error(error))
        finally:
            if client is not None and callable(getattr(client, "close", None)):
                client.close()
            with self._lock:
                self._rows[plan.identifier]["seconds"] = perf_counter() - started
                self._persist()

    def close(self):
        self.cancel()
        self._executor.shutdown(wait=True)
