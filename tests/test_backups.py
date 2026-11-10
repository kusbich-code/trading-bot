"""Бэкапы базы: хранятся 10 последних, старые удаляются; трогаются только файлы бэкапов."""
import os
import time
from pathlib import Path

import pytest

from app.services import backups as bk


def make(tmp_path, n, prefix="trading_bot_before_x_"):
    d = tmp_path / "backups"
    d.mkdir(exist_ok=True)
    now = time.time()
    for i in range(n):
        f = d / f"{prefix}{i:02d}.sqlite3"
        f.write_bytes(b"x" * (1024 * (i + 1)))
        os.utime(f, (now - (n - i) * 3600, now - (n - i) * 3600))       # чем больше i, тем новее
    return d


def test_summary_counts_files_and_size(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make(tmp_path, 3)
    s = bk.summary()
    assert s["count"] == 3 and s["keep"] == bk.KEEP and s["total_mb"] == pytest.approx(6 / 1024, abs=0.01)
    assert s["newest"].endswith("02.sqlite3") and s["oldest"].endswith("00.sqlite3")


def test_prune_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    d = make(tmp_path, 13)
    (d / "notes.txt").write_text("не бэкап", encoding="utf-8")
    (d / "sub").mkdir()
    res = bk.prune()
    left = sorted(p.name for p in d.glob("*.sqlite3"))
    assert res["deleted"] == 3 and len(left) == 10 and left[0].endswith("03.sqlite3")
    assert (d / "notes.txt").exists() and (d / "sub").is_dir()
    assert res["freed_mb"] == pytest.approx(6 / 1024, abs=0.01)


def test_prune_without_backups_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert bk.prune() == {"deleted": 0, "freed_mb": 0.0, "kept": 0}
    assert bk.summary()["count"] == 0


def test_scripts_prune_after_new_backup(tmp_db, tmp_path, monkeypatch):
    """Новые бэкапы появляются при пересчёте истории и чистке: после каждого лишние удаляются сами."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    import cleanup_virtual
    make(tmp_path, 12)
    cleanup_virtual.backup_db(tmp_db.settings.DB_PATH)
    assert len(list((tmp_path / "backups").glob("*.sqlite3"))) == bk.KEEP
