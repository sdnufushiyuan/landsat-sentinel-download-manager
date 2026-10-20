"""Offline concurrency/resume checks; no Earth Engine authentication or network."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
import rasterio
from rasterio.transform import Affine
from pyproj import CRS
from shapely.geometry import box, mapping

import download_optical as hls


class Session:
    def __init__(self):
        self.proxies = {}
        self.trust_env = True
        self.closed = False
        self.owners = set()

    def close(self):
        self.closed = True


class ParallelChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.tiles = [hls.Tile(0, index, 300000 + index * 120, 3300000, 4, 3)
                      for index in range(6)]
        self.scene = {"sensor": "L8", "index": "synthetic", "id": "synthetic-scene"}
        self.sessions = []

    def tearDown(self):
        self.temporary.cleanup()

    def new_session(self):
        session = Session()
        self.sessions.append(session)
        return session

    def call(self, pool, tiles=None, emit=lambda _value: None):
        return pool.download_scene(MagicMock(), tiles or self.tiles, self.root / "scene",
                                   "EPSG:32650", hls.MountGuard(self.root), "recipe", "low",
                                   self.scene, emit)

    def test_overlap_bounded_submission_independent_reused_sessions_and_serial_signing(self):
        release, both_active = threading.Event(), threading.Event()
        lock = threading.Lock()
        calls, active, peak, sign_active, sign_peak = [], 0, 0, 0, 0
        logs, results, errors = [], [], []
        image = MagicMock()

        def sign(_parameters):
            nonlocal sign_active, sign_peak
            with lock:
                sign_active += 1
                sign_peak = max(sign_peak, sign_active)
            time.sleep(0.01)
            with lock:
                sign_active -= 1
            return "https://example.invalid/synthetic"

        image.getDownloadURL.side_effect = sign

        def download(image, tile, _path, _crs, session, *_args, url_lock=None, cancel_event=None):
            nonlocal active, peak
            session.owners.add(threading.get_ident())
            hls.get_download_url(image, {}, url_lock, cancel_event)
            with lock:
                calls.append(tile.col)
                active += 1
                peak = max(peak, active)
                if active == 2:
                    both_active.set()
            try:
                self.assertTrue(release.wait(5))
                # Force the first pair to finish out of order.
                if tile.col == 0:
                    time.sleep(0.05)
                return {"valid_pixels": tile.col}, tile.col == 2
            finally:
                with lock:
                    active -= 1

        with patch("requests.Session", side_effect=self.new_session), patch.object(hls, "download_tile", side_effect=download):
            with hls.TileDownloadPool(2, "http://127.0.0.1:8118") as pool:
                def owner():
                    try:
                        results.append(pool.download_scene(image, self.tiles, self.root / "scene",
                            "EPSG:32650", hls.MountGuard(self.root), "recipe", "low", self.scene, logs.append))
                    except BaseException as error:
                        errors.append(error)
                thread = threading.Thread(target=owner)
                thread.start()
                try:
                    self.assertTrue(both_active.wait(5))
                    time.sleep(0.03)
                    self.assertEqual(len(calls), 2, "No other tile may be queued before either finishes")
                finally:
                    release.set()
                    thread.join(5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
                self.call(pool, self.tiles[:2])  # Same workers reuse their own sessions across scenes.
            self.assertEqual(peak, 2)
            self.assertEqual(sign_peak, 1)
            self.assertEqual(len(self.sessions), 2)
            self.assertTrue(all(len(session.owners) == 1 and session.closed and not session.trust_env
                                and session.proxies["https"] == "http://127.0.0.1:8118"
                                for session in self.sessions))
            self.assertEqual([item["file"] for item in results[0]], [tile.name for tile in self.tiles])
            self.assertIn(self.tiles[1].name, logs[0])
            self.assertTrue(logs[0].startswith("  1/6 "))

    def test_fatal_failure_wins_over_transient_and_all_workers_drain_without_new_tiles(self):
        barrier, drained = threading.Barrier(2), threading.Event()
        calls = []

        def download(_image, tile, *_args, cancel_event=None, **_kwargs):
            calls.append(tile.col)
            barrier.wait(5)
            if tile.col == 0:
                raise hls.TransientDownloadError("temporary network")
            self.assertTrue(cancel_event.wait(5))
            drained.set()
            raise hls.DownloadError("NAS capacity")

        with patch("requests.Session", side_effect=self.new_session), patch.object(hls, "download_tile", side_effect=download):
            with self.assertRaisesRegex(hls.DownloadError, "NAS capacity") as caught:
                with hls.TileDownloadPool(2, "none") as pool:
                    self.call(pool)
            self.assertNotIsInstance(caught.exception, hls.TransientDownloadError)
            self.assertTrue(drained.is_set())
            self.assertCountEqual(calls, [0, 1])
            self.assertTrue(all(session.closed for session in self.sessions))

    def test_keyboard_interrupt_drains_cancels_waiting_worker_and_closes_sessions(self):
        barrier, drained = threading.Barrier(2), threading.Event()
        calls = []

        def download(_image, tile, *_args, cancel_event=None, **_kwargs):
            calls.append(tile.col)
            barrier.wait(5)
            if tile.col == 0:
                raise KeyboardInterrupt()
            self.assertTrue(cancel_event.wait(5))
            drained.set()
            hls.check_download_cancelled(cancel_event)

        with patch("requests.Session", side_effect=self.new_session), patch.object(hls, "download_tile", side_effect=download):
            with self.assertRaises(KeyboardInterrupt):
                with hls.TileDownloadPool(2, "none") as pool:
                    self.call(pool)
            self.assertTrue(drained.is_set())
            self.assertCountEqual(calls, [0, 1])
            self.assertTrue(all(session.closed for session in self.sessions))

    def test_cancel_interrupts_real_network_retry_delay_without_another_request(self):
        import requests
        event, waiting = threading.Event(), threading.Event()
        image, session = MagicMock(), MagicMock()
        image.getDownloadURL.return_value = "https://example.invalid/signed?token=private"
        session.get.side_effect = requests.ConnectionError("private URL")
        errors = []
        def owner():
            try:
                hls.download_tile(image, self.tiles[0], self.root / self.tiles[0].name,
                    "EPSG:32650", session, hls.MountGuard(self.root), "recipe", "low", "scene",
                    url_lock=threading.Lock(), cancel_event=event)
            except BaseException as error:
                errors.append(error)
        with patch("builtins.print", side_effect=lambda *_args, **_kwargs: waiting.set()):
            thread = threading.Thread(target=owner)
            thread.start()
            try:
                self.assertTrue(waiting.wait(5))
                event.set()
            finally:
                event.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], hls.DownloadCancelled)
        self.assertEqual(image.getDownloadURL.call_count, 1)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_failed_scene_has_no_marker_then_resumes_exact_cached_tile_and_order(self):
        tiles = self.tiles[:3]
        roi = box(300000, 3299910, 300360, 3300000)
        geographic = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {}, "geometry": mapping(box(116, 30, 116.01, 30.01))}]}
        scene = {"index": "synthetic", "time_ms": 0, "projected_footprint": mapping(roi)}
        ee = MagicMock()
        ee.__version__ = "offline-test"
        ee.Dictionary.return_value.getInfo.return_value = {
            "L8": {"count": 1, "items": [scene]}, "L9": {"count": 0, "items": []}}
        image = MagicMock()
        image.getDownloadURL.side_effect = lambda parameters: str(parameters["crs_transform"][2])
        contents = {}
        for tile in tiles:
            values = np.empty((8, tile.height, tile.width), dtype="float32")
            for index, value in enumerate((0.1, 0.3, 0.2, 0.5, 1.6, 64, 64, 0)):
                values[index] = value
            path = self.root / ("source-" + tile.name)
            with rasterio.open(path, "w", driver="GTiff", width=tile.width, height=tile.height,
                count=8, dtype="float32", crs="EPSG:32650", transform=Affine(*tile.transform)) as dataset:
                dataset.write(values)
            contents[str(tile.left)] = path.read_bytes()
        failed, committed = threading.Event(), threading.Event()
        http_calls = []
        real_download = hls.download_tile
        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def raise_for_status(self): pass
            def iter_content(self, _size): yield self.body
        class HTTP(Session):
            def get(self, url, **_kwargs):
                http_calls.append(url)
                return Response(contents[url])
        def new_http():
            session = HTTP()
            self.sessions.append(session)
            return session
        def interrupted(image, tile, *args, **kwargs):
            if tile.col == 1 and not failed.is_set():
                self.assertTrue(committed.wait(5))
                failed.set()
                raise hls.DownloadError("synthetic fatal")
            result = real_download(image, tile, *args, **kwargs)
            if tile.col == 0:
                committed.set()
            return result
        args = hls.parser().parse_args(["--sensor", "L8", "--roi", "unused.shp", "--start", "2013-01-01", "--end", "2013-02-01",
            "--out", str(self.root / "out"), "--proxy", "none", "--project", "offline", "--workers", "8",
            "--no-download-optimization"])
        with patch.object(hls, "load_roi", return_value=(roi, CRS("EPSG:32650"), geographic)), \
             patch.object(hls, "make_tiles", return_value=tiles), \
             patch.object(hls, "initialize_ee", return_value=ee), \
             patch.object(hls, "build_image", return_value=image), \
             patch("requests.Session", side_effect=new_http), contextlib.redirect_stdout(io.StringIO()):
            with patch.object(hls, "download_tile", side_effect=interrupted):
                with self.assertRaisesRegex(hls.DownloadError, "synthetic fatal"):
                    hls.run(args)
            directory = self.root / "out" / "L8" / "synthetic"
            self.assertFalse((directory / "scene.json").exists())
            self.assertTrue((directory / tiles[0].name).exists())
            saved_hash = hls.file_hash(directory / tiles[0].name)
            before = json.loads((self.root / "out" / "run_manifest.json").read_text())["recipe_hash"]
            args.workers = 1  # Changing performance settings preserves the recipe/cache.
            hls.run(args)
            record = json.loads((directory / "scene.json").read_text())
            self.assertEqual([item["file"] for item in record["tiles"]], [tile.name for tile in tiles])
            self.assertEqual(record["recipe_hash"], before)
            self.assertEqual(hls.file_hash(directory / tiles[0].name), saved_hash)
            self.assertEqual(http_calls.count(str(tiles[0].left)), 1)
            self.assertFalse(any(".part" in path.name for path in directory.iterdir()))
            self.assertTrue(all(session.closed for session in self.sessions))

    def test_workers_validation_before_authentication_and_serial_legacy_call(self):
        for value in (0, 9, True, 1.5):
            with self.subTest(workers=value), self.assertRaises(hls.DownloadError):
                hls.TileDownloadPool(value, "none")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            hls.parser().parse_args(["--sensor", "L8", "--roi", "x", "--start", "2013-01-01", "--end", "2013-02-01",
                                    "--out", "x", "--workers", "9"])
        with patch("requests.Session", side_effect=self.new_session), \
             patch.object(hls, "download_tile", return_value=({"valid_pixels": 1}, False)) as download, \
             patch.object(hls, "ThreadPoolExecutor") as executor:
            with hls.TileDownloadPool(1, "none") as pool:
                self.call(pool, self.tiles[:2])
            executor.assert_not_called()
            self.assertEqual(download.call_count, 2)
            self.assertEqual(download.call_args.kwargs, {})
            self.assertEqual(len(self.sessions), 1)
            self.assertTrue(self.sessions[0].closed)


if __name__ == "__main__":
    unittest.main()
