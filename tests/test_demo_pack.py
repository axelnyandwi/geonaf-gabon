"""Demo pack loader: download limits, archive safety, integrity and atomic install (no network)."""

import hashlib
import json
import stat
import zipfile
from pathlib import Path

import pytest

from src.data import demo_pack
from src.data.demo_pack import (
    DemoPackError, DemoPackSpec, create_archive, install_pack, pack_summary, resolve_data_dir,
    spec_from_environment, verify_pack,
)

URL = "https://github.com/owner/repo/releases/download/v1/pack.zip"


class FakeResponse:
    def __init__(self, body, *, url=URL, status=200, length="auto", fail_after=None):
        self.body, self.url, self.status_code, self.fail_after = body, url, status, fail_after
        self.headers = {} if length is None else {"Content-Length": str(len(body) if length == "auto" else length)}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        for start in range(0, len(self.body), 7):
            if self.fail_after is not None and start >= self.fail_after:
                raise ConnectionError("reset by peer at /secret/path")
            yield self.body[start:start + 7]


class FakeSession:
    def __init__(self, response):
        self.response, self.calls = response, 0

    def get(self, url, **kwargs):
        self.calls += 1
        assert kwargs["stream"] is True and kwargs["timeout"]
        return self.response


def _pack(directory: Path, files=None) -> Path:
    files = files or {"aoi/akanda.geojson": b'{"type": "FeatureCollection"}', "raw/a.tif": b"\x00\x01" * 50}
    for name, content in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(content)
    manifest = {"pack_id": "demo", "pack_version": "1", "files": [
        {"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
        for name, content in sorted(files.items())]}
    (directory / "PACK_MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def _archive(tmp_path) -> bytes:
    source = _pack(tmp_path / "source")
    create_archive(source, tmp_path / "pack.zip")
    return (tmp_path / "pack.zip").read_bytes()


def _spec(body, **changes):
    return DemoPackSpec(url=changes.pop("url", URL), sha256=hashlib.sha256(body).hexdigest(), pack_id="demo",
                        **changes)


def _zip(entries) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, content, mode in entries:
            info = zipfile.ZipInfo(name)
            info.external_attr = mode << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, content)
    return buffer.getvalue()


def _no_leftovers(cache):
    return not cache.exists() or not [path for path in cache.iterdir() if path.name.startswith(".")]


def test_install_then_reuse_without_download(tmp_path):
    body = _archive(tmp_path)
    session, cache = FakeSession(FakeResponse(body)), tmp_path / "cache"
    path, manifest = install_pack(_spec(body), cache, session=session)
    assert manifest["pack_id"] == "demo" and (path / "raw" / "a.tif").read_bytes() == b"\x00\x01" * 50
    assert json.loads((path / ".geonaf-install.json").read_text())["archive_sha256"] == _spec(body).sha256
    assert install_pack(_spec(body), cache, session=session)[0] == path and session.calls == 1
    assert _no_leftovers(cache)


def test_wrong_archive_digest_is_never_extracted(tmp_path):
    body = _archive(tmp_path)
    spec = DemoPackSpec(url=URL, sha256="0" * 64, pack_id="demo")
    with pytest.raises(DemoPackError) as error:
        install_pack(spec, tmp_path / "cache", session=FakeSession(FakeResponse(body)))
    assert error.value.category == "integrity"
    assert not list((tmp_path / "cache").glob("demo-*")) and _no_leftovers(tmp_path / "cache")


@pytest.mark.parametrize("length", ["auto", None], ids=["declared", "streamed"])
def test_oversized_archive_is_refused(tmp_path, length):
    body = _archive(tmp_path)
    spec = _spec(body, max_bytes=len(body) - 1)
    with pytest.raises(DemoPackError) as error:
        install_pack(spec, tmp_path / "cache", session=FakeSession(FakeResponse(body, length=length)))
    assert error.value.category == "size" and _no_leftovers(tmp_path / "cache")


@pytest.mark.parametrize("response", [
    lambda body: FakeResponse(body, length=len(body) + 10),
    lambda body: FakeResponse(body, fail_after=14),
], ids=["truncated", "connection_reset"])
def test_interrupted_download_leaves_nothing(tmp_path, response):
    body = _archive(tmp_path)
    with pytest.raises(DemoPackError) as error:
        install_pack(_spec(body), tmp_path / "cache", session=FakeSession(response(body)))
    assert error.value.category == "network"
    assert "secret" not in str(error.value) and str(tmp_path) not in str(error.value)
    assert _no_leftovers(tmp_path / "cache") and not list((tmp_path / "cache").glob("demo-*"))


def test_http_errors_and_foreign_redirects_are_refused(tmp_path):
    body = _archive(tmp_path)
    for response in (FakeResponse(body, status=404), FakeResponse(body, url="https://evil.example/pack.zip")):
        with pytest.raises(DemoPackError) as error:
            install_pack(_spec(body), tmp_path / "cache", session=FakeSession(response))
        assert error.value.category == "network"


def test_corrupted_archive_with_matching_digest_is_refused(tmp_path):
    body = _archive(tmp_path)
    corrupted = body[:40] + bytes(len(body) - 80) + body[-40:]
    with pytest.raises(DemoPackError) as error:
        install_pack(_spec(corrupted), tmp_path / "cache", session=FakeSession(FakeResponse(corrupted)))
    assert error.value.category in {"archive", "integrity"}


@pytest.mark.parametrize("name,mode", [
    ("../evil.txt", stat.S_IFREG | 0o644), ("/abs.txt", stat.S_IFREG | 0o644),
    ("C:/evil.txt", stat.S_IFREG | 0o644), ("a\\..\\..\\evil.txt", stat.S_IFREG | 0o644),
    ("link", stat.S_IFLNK | 0o777),
])
def test_malicious_members_are_refused(tmp_path, name, mode):
    body = _zip([("PACK_MANIFEST.json", "{}", stat.S_IFREG | 0o644), (name, "x", mode)])
    with pytest.raises(DemoPackError) as error:
        install_pack(_spec(body), tmp_path / "cache", session=FakeSession(FakeResponse(body)))
    assert error.value.category == "archive"
    assert not (tmp_path / "evil.txt").exists() and not (tmp_path.parent / "evil.txt").exists()


def test_duplicate_members_and_compression_bombs_are_refused(tmp_path):
    duplicate = _zip([("a.txt", "1", stat.S_IFREG | 0o644), ("A.txt", "2", stat.S_IFREG | 0o644)])
    bomb = _zip([("bomb.bin", b"\x00" * 5_000_000, stat.S_IFREG | 0o644)])
    for body in (duplicate, bomb):
        with pytest.raises(DemoPackError) as error:
            install_pack(_spec(body), tmp_path / "cache", session=FakeSession(FakeResponse(body)))
        assert error.value.category == "archive"


def test_unlisted_or_modified_files_fail_verification(tmp_path):
    directory = _pack(tmp_path / "pack")
    verify_pack(directory, "demo")
    (directory / "extra.txt").write_text("x")
    with pytest.raises(DemoPackError, match="non déclarés"):
        verify_pack(directory)
    (directory / "extra.txt").unlink()
    (directory / "raw" / "a.tif").write_bytes(b"\x00\x02" * 50)
    with pytest.raises(DemoPackError, match="empreinte"):
        verify_pack(directory)


def test_damaged_installation_is_downloaded_again(tmp_path):
    body = _archive(tmp_path)
    session, cache = FakeSession(FakeResponse(body)), tmp_path / "cache"
    path, _ = install_pack(_spec(body), cache, session=session)
    (path / "raw" / "a.tif").write_bytes(b"tampered")
    assert install_pack(_spec(body), cache, session=session)[0] == path and session.calls == 2
    assert (path / "raw" / "a.tif").read_bytes() == b"\x00\x01" * 50 and _no_leftovers(cache)


@pytest.mark.parametrize("url", ["http://github.com/x.zip", "https://evil.example/x.zip",
                                 "https://user:pass@github.com/x.zip"])
def test_spec_refuses_unsafe_urls(url):
    with pytest.raises(DemoPackError) as error:
        DemoPackSpec(url=url, sha256="a" * 64, pack_id="demo")
    assert error.value.category == "configuration" and "pass" not in str(error.value)


def test_environment_configuration_is_all_or_nothing(monkeypatch):
    monkeypatch.setattr(demo_pack, "RELEASED_PACK", None)
    assert spec_from_environment({}) is None
    with pytest.raises(DemoPackError):
        spec_from_environment({"GEONAF_DEMO_PACK_URL": URL})
    spec = spec_from_environment({"GEONAF_DEMO_PACK_URL": URL, "GEONAF_DEMO_PACK_SHA256": "A" * 64,
                                  "GEONAF_DEMO_PACK_ID": "demo"})
    assert spec.sha256 == "a" * 64


def test_local_data_take_precedence_over_the_pack(tmp_path):
    def installer(*args):
        raise AssertionError("aucun téléchargement attendu")

    local = tmp_path / "data"
    (local / "raw" / "sentinel2" / "P").mkdir(parents=True)
    (local / "raw" / "sentinel2" / "P" / "acquisition_manifest.json").write_text("{}")
    spec = DemoPackSpec(url=URL, sha256="a" * 64, pack_id="demo")
    assert resolve_data_dir(local, spec=spec, cache_root=tmp_path, installer=installer).origin == "local"
    assert resolve_data_dir(tmp_path / "empty", spec=None, cache_root=tmp_path, installer=installer).origin == "none"
    source = resolve_data_dir(tmp_path / "empty", spec=spec, cache_root=tmp_path,
                              installer=lambda spec, root: (tmp_path / "pack", {"pack_id": "demo"}))
    assert (source.origin, source.path) == ("demo_pack", tmp_path / "pack")


def test_unpacked_pack_as_data_dir_is_verified(tmp_path):
    directory = _pack(tmp_path / "pack", {"raw/sentinel2/P/subset_manifest.json": b"{}"})
    source = resolve_data_dir(directory, spec=None, cache_root=tmp_path)
    assert source.origin == "demo_pack" and source.pack["pack_id"] == "demo"


def test_summary_reports_low_coverage_dates_from_the_manifest():
    lines = pack_summary({"pack_id": "p", "pack_version": "2", "aoi": {"statement": "Carré."},
                          "sentinel2": {"tile": "T32NNF", "attribution": "Copernicus", "acquisitions": [
                              {"acquired_at_utc": "2025-01-20T09:33:21Z", "ingested_at_utc": "2026-10-22T11:50Z",
                               "quality": {"valid_coverage_percent": 2.7188, "trend_use": "excluded_low_coverage"}}]},
                          "external_layers": [], "limits": ["Limite."]})
    assert "2025-01-20" in lines[1] and "2.72 %" in lines[1] and "exclue des tendances" in lines[1]
    assert lines[-1] == "Limite."
