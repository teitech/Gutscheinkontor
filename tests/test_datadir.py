import dataclasses
import os
import tempfile
import time

import pytest

from app import db
from app.config import get_settings


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Eigenes Datenverzeichnis, damit Tests nicht an den Ordnern anderer Tests oder von pytest rütteln."""
    data_dir = tmp_path / "daten"
    base = get_settings()
    monkeypatch.setattr(db, "get_settings", lambda: dataclasses.replace(base, data_dir=str(data_dir)))
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)  # setzt den Wert nach dem Test zurück
    return data_dir


def test_temp_verzeichnis_liegt_im_datenverzeichnis(isolated):
    db.prepare_data_dir()
    tmp = isolated / "tmp"
    assert tmp.is_dir() and tempfile.gettempdir() == str(tmp)
    with tempfile.TemporaryFile() as f:  # Exporte, Importe und Uploads landen hier, nicht in /tmp
        f.write(b"x")
        assert f.name is not None
    assert (isolated / "attachments").is_dir()


def make_old(path, seconds=db.TMP_MAX_AGE_SECONDS + 60):
    long_ago = time.time() - seconds
    os.utime(path, (long_ago, long_ago))


def test_serverstart_raeumt_nur_alte_reste_auf(isolated):
    db.prepare_data_dir()
    tmp = isolated / "tmp"
    (tmp / "alt.bin").write_bytes(b"x")
    (tmp / "ordner").mkdir()
    (tmp / "ordner" / "datei").write_bytes(b"x")
    (tmp / "frisch.bin").write_bytes(b"x")  # z. B. ein Upload, der gerade läuft
    make_old(tmp / "alt.bin")
    make_old(tmp / "ordner")

    db.prepare_data_dir()  # wie bei "python -m app.cli ..." neben einer laufenden App: nichts löschen
    assert (tmp / "alt.bin").exists() and (tmp / "ordner").exists()

    db.prepare_data_dir(clean_tmp=True)  # wie beim Start des Servers
    assert not (tmp / "alt.bin").exists() and not (tmp / "ordner").exists()
    assert (tmp / "frisch.bin").exists()


def test_init_db_reicht_clean_tmp_durch(isolated):
    db.init_db()
    (isolated / "tmp" / "alt.bin").write_bytes(b"x")
    make_old(isolated / "tmp" / "alt.bin")
    db.init_db()
    assert (isolated / "tmp" / "alt.bin").exists()
    db.init_db(clean_tmp=True)
    assert not (isolated / "tmp" / "alt.bin").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="Root darf überall schreiben")
def test_nicht_beschreibbares_datenverzeichnis_gibt_klare_meldung(isolated):
    isolated.mkdir()
    isolated.chmod(0o500)
    try:
        with pytest.raises(RuntimeError) as caught:
            db.prepare_data_dir()
        message = str(caught.value)
        assert "nicht beschreibbar" in message and "chown" in message
        assert f"{os.getuid()}:{os.getgid()}" in message and "benanntes Docker-Volume" in message
    finally:
        isolated.chmod(0o700)
