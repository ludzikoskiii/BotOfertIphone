import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtGui import QColor, QPixmap  # noqa: E402

from phonebot.ui.images import THUMB_SIZE, ThumbnailCache  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _pixmap():
    pm = QPixmap(THUMB_SIZE)
    pm.fill(QColor("red"))
    return pm


def test_memory_cache_is_bounded_lru(app, tmp_path):
    cache = ThumbnailCache(tmp_path, max_in_memory=3)
    for i in range(3):
        cache._remember(f"u{i}", _pixmap())
    cache.get("u0")  # u0 użyta — najstarsza jest teraz u1
    cache._remember("u3", _pixmap())
    assert list(cache._mem) == ["u2", "u0", "u3"]


def test_disk_cache_pruned_by_age(app, tmp_path):
    cache = ThumbnailCache(tmp_path)
    old = tmp_path / f"a_{THUMB_SIZE.width()}.jpg"
    new = tmp_path / f"b_{THUMB_SIZE.width()}.jpg"
    other = tmp_path / "c_320.jpg"  # inny rozmiar — należy do innej pamięci podręcznej
    for f in (old, new, other):
        f.write_bytes(b"x")
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    os.utime(other, (past, past))
    assert cache.prune_disk(30) == 1
    assert not old.exists() and new.exists() and other.exists()


def test_queue_deduplicates_requests(app, tmp_path):
    cache = ThumbnailCache(tmp_path)
    cache._pump = lambda: None  # bez sieci
    for _ in range(3):
        cache.get("https://example.invalid/a.jpg")
    assert list(cache._queue) == ["https://example.invalid/a.jpg"]
