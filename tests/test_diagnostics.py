import pytest

from src.diagnostics import safe_call, safe_exception_message


def test_safe_exception_message_hides_supplied_path_with_spaces(tmp_path):
    private_path = tmp_path / "professional folder" / "missing raster.tif"
    error = FileNotFoundError(f"Cannot open {private_path}: not found")

    message = safe_exception_message(error, paths=(private_path,))

    assert str(private_path) not in message
    assert "not found" in message
    assert "<chemin local masqué>" in message


def test_safe_exception_message_hides_windows_path_without_known_path():
    error = OSError(r"failed to read C:\private\folder with spaces\raster.tif: denied")

    message = safe_exception_message(error)

    assert "C:\\private" not in message
    assert "denied" in message


def test_safe_exception_message_hides_posix_path_without_known_path():
    error = OSError("failed to read /srv/private/raster.tif: denied")

    message = safe_exception_message(error)

    assert "/srv/private" not in message
    assert "denied" in message


def test_safe_call_preserves_exception_type_and_hides_path(tmp_path):
    private_path = tmp_path / "private raster.tif"

    def fail(path):
        raise FileNotFoundError(f"Missing: {path}")

    with pytest.raises(FileNotFoundError) as caught:
        safe_call(fail, private_path, paths=(private_path,))

    assert str(private_path) not in str(caught.value)
    assert "Missing" in str(caught.value)
