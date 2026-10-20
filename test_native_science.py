"""Synthetic pixel math plus actual GeoTIFF validation; no EE/auth/network."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import nullcontext
import numpy as np
import rasterio
from rasterio.transform import Affine
from shapely.geometry import box
import archive
import download_optical as optical
from watch_progress import estimate
from collections import deque


class Image:
    def __init__(self, values, names=None):
        self.values = np.asarray(values, dtype=float).reshape(-1)
        self.names = list(names or ["x"] * len(self.values))
    def select(self, names, renamed=None):
        names = [names] if isinstance(names, str) else names
        return Image([self.values[self.names.index(name)] for name in names], renamed or names)
    def _op(self, other, operation):
        return Image(operation(self.values, other.values if isinstance(other, Image) else other), self.names)
    def multiply(self, x): return self._op(x, np.multiply)
    def add(self, x): return self._op(x, np.add)
    def subtract(self, x): return self._op(x, np.subtract)
    def divide(self, x): return self._op(x, np.divide)
    def pow(self, x): return self._op(x, np.power)
    def abs(self): return Image(np.abs(self.values), self.names)
    def eq(self, x): return self._op(x, np.equal)
    def gt(self, x): return self._op(x, np.greater)
    def lte(self, x): return self._op(x, np.less_equal)
    def And(self, x): return self._op(x, np.logical_and)
    def Or(self, x): return self._op(x, np.logical_or)
    def bitwiseAnd(self, x): return Image(np.bitwise_and(self.values.astype(int), x), self.names)
    def rightShift(self, x): return Image(np.right_shift(self.values.astype(int), x), self.names)
    def mask(self): return Image(np.isfinite(self.values), self.names)
    def reduce(self, _): return Image([np.min(self.values)])
    def updateMask(self, mask): return Image(np.where(mask.values, self.values, np.nan), self.names)
    def rename(self, name): return Image(self.values, [name])
    def addBands(self, bands):
        bands = [bands] if isinstance(bands, Image) else bands
        return Image(np.concatenate([self.values, *[item.values for item in bands]]), self.names + sum([item.names for item in bands], []))
    def clipToCollection(self, _): return self
    def toFloat(self): return self
    def unmask(self, value, **_): return Image(np.nan_to_num(self.values, nan=value), self.names)


class EE:
    class Reducer:
        @staticmethod
        def min(): return "min"


class ScienceTests(unittest.TestCase):
    def tearDown(self):
        optical.configure("L8")
        optical.MAX_TRANSFER_BPS = 0
    def source(self, sensor, qa=64, aq=64, sat=0):
        if sensor == "S2":
            return Image([1000, 3000, 2000, qa, aq], ["B4", "B8A", "B12", "SCL", "AOT"])
        return Image([11000, 18000, 14500, qa, aq, sat], ["SR_B4", "SR_B5", "SR_B7", "QA_PIXEL", "SR_QA_AEROSOL", "QA_RADSAT"])
    def test_landsat_scale_offset_and_indices_for_both_spacecraft(self):
        for sensor in ("L8", "L9"):
            optical.configure(sensor)
            result = optical.build_image(EE, self.source(sensor), sensor, None, 1, "low")
            red, nir, swir = np.array([11000, 18000, 14500]) * .0000275 - .2
            np.testing.assert_allclose(result.values, [red, nir, swir, (nir-red)/(nir+red), (1-swir)**2/(2*swir), 64, 64, 0])
            self.assertEqual(result.names, list(optical.BANDS))
    def test_sentinel_scale_scl_and_aot_units(self):
        optical.configure("S2")
        for scl in (4, 5):
            result = optical.build_image(EE, self.source("S2", scl, 120), "S2", None, 1, "low")
            np.testing.assert_allclose(result.values, [.1, .3, .2, .5, 1.6, scl, .12, 0])
        optical.configure("S2", max_aot=.1)
        result = optical.build_image(EE, self.source("S2", 4, 120), "S2", None, 1, "low")
        self.assertTrue(np.all(result.values == optical.NODATA))
    def test_all_landsat_rejected_quality_bits_server_and_local_agree(self):
        optical.configure("L8")
        bad = [(64 | (1 << bit), 64, 0) for bit in (0, 1, 2, 3, 4, 5, 7)]
        bad += [(64, aq, 0) for aq in (0, 128, 192, 65, 68)]
        bad += [(64, 64, 1 << bit) for bit in (3, 4, 6, 11)]
        for qa, aq, sat in bad:
            with self.subTest(qa=qa, aq=aq, sat=sat):
                self.assertFalse(optical.strict_clear_land(qa, "low", aq, sat))
                result = optical.build_image(EE, self.source("L8", qa, aq, sat), "L8", None, 1, "low")
                self.assertTrue(np.all(result.values == optical.NODATA))
    def test_all_scl_classes_server_and_local_agree(self):
        optical.configure("S2")
        for scl in range(12):
            result = optical.build_image(EE, self.source("S2", scl, 50), "S2", None, 1, "low")
            self.assertEqual(bool(optical.strict_clear_land(scl)), scl in (4, 5))
            self.assertEqual(bool(np.all(result.values != optical.NODATA)), scl in (4, 5))
    def test_landsat_climatology_is_explicit_and_not_valid_low_by_default(self):
        self.assertFalse(optical.strict_clear_land(64, "low", 0, 0))
        self.assertTrue(optical.strict_clear_land(64, "climatology-low", 0, 0))
    def test_grid_sizes_and_no_fake_ten_metre_landsat(self):
        for size in (10, 20, 30):
            optical.configure("S2", size)
            tiles = optical.make_tiles(box(5, 5, 1005, 1005), 64)
            self.assertEqual(tiles[0].transform[0], size)
        with self.assertRaises(optical.DownloadError): optical.configure("L8", 10)
    def test_bandwidth_cap_waits_only_for_deficit_and_is_cancellable(self):
        import threading
        optical.MAX_TRANSFER_BPS = 100
        with patch.object(optical.time, 'monotonic', return_value=11), patch.object(optical.time, 'sleep') as sleep:
            optical.pace_transfer(200, 10)
            sleep.assert_called_once_with(1)
            sleep.reset_mock()
            optical.pace_transfer(50, 10)
            sleep.assert_not_called()
            cancelled = threading.Event()
            cancelled.set()
            with self.assertRaises(optical.DownloadCancelled): optical.pace_transfer(200, 10, cancelled)
    def test_real_geotiff_rejects_wrong_quality_or_wrong_index(self):
        with tempfile.TemporaryDirectory() as directory:
            for sensor, qa, aq in (("L8", 64, 64), ("L9", 64, 64), ("S2", 4, .12)):
                optical.configure(sensor)
                tile = optical.Tile(0, 0, 300000, 3300000, 3, 2)
                path = Path(directory)/f"{sensor}.tif"
                values = np.array([.1, .3, .2, .5, 1.6, qa, aq, 0], dtype="float32")[:, None, None] * np.ones((8, 2, 3), dtype="float32")
                with rasterio.open(path, "w", driver="GTiff", count=8, width=3, height=2, dtype="float32", crs="EPSG:32650", transform=Affine(*tile.transform)) as dest: dest.write(values)
                self.assertEqual(optical.inspect_tile(path, tile, "EPSG:32650", annotate=True)["valid_pixels"], 6)
                with rasterio.open(path, "r+") as dest: dest.write(np.full((2, 3), .8, dtype="float32"), 3)
                with self.assertRaisesRegex(optical.DownloadError, "NDVI/STR"):
                    optical.inspect_tile(path, tile, "EPSG:32650")


class ArchiveTests(unittest.TestCase):
    def config(self):
        root = Path(tempfile.gettempdir()).resolve()
        return {"start": "2021-10-01", "end_exclusive": "2021-12-01", "output_root": str(root/"native-test-output"),
            "regions": [{"code": "big", "roi": str(root/"big.shp"), "crs": "EPSG:32650", "area_m2": 1e8},
                        {"code": "small", "roi": str(root/"small.shp"), "crs": "EPSG:32650", "area_m2": 1e4}]}
    def test_month_boundaries_and_sensor_availability(self):
        config = self.config()
        jobs = archive.tasks(config)
        self.assertEqual(len(jobs), 12)  # L9 starts October31, then November.
        l9 = [j for j in jobs if j["sensor"] == "L9"]
        self.assertEqual(l9[0]["start"], "2021-10-31")
        self.assertEqual(l9[0]["end"], "2021-11-01")
        self.assertEqual(archive.months("2020-02-28", "2020-03-02"), [("2020-02-28", "2020-03-01"), ("2020-03-01", "2020-03-02")])
    def test_all_sensors_separate_from_each_other_and_hls(self):
        jobs = archive.tasks(self.config())
        self.assertEqual(len(set(j["out"] for j in jobs)), len(jobs))
        self.assertEqual(archive.ordered_regions(self.config(), jobs), ["small", "big"])
    def test_inventory_not_reused_after_dates_roi_or_science_change(self):
        config = self.config()
        before = archive.config_hash(config)
        config["workers"], config["proxy"] = 8, "none"
        self.assertEqual(before, archive.config_hash(config))
        config["s2_resolution"] = 20
        self.assertNotEqual(before, archive.config_hash(config))
    def test_child_argv_is_not_shell_and_sensor_resolution_are_explicit(self):
        config = self.config()
        job = archive.tasks(config)[0]
        argv = archive.command(config, job)
        self.assertEqual(argv[argv.index("--sensor")+1], job["sensor"])
        self.assertIn("--resolution", argv)
        self.assertIn(job["out"], argv)
    def test_eta_requires_enough_time_and_new_candidates(self):
        samples = deque()
        self.assertEqual(estimate(samples, 0, 10, 100), (None, None))
        self.assertEqual(estimate(samples, 60, 12, 100), (None, None))
        speed, eta = estimate(samples, 180, 16, 100)
        self.assertEqual(speed, 2)
        self.assertEqual(eta, 2520)
    def test_runner_finishes_all_three_sensors_of_small_region_before_big(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.config()
            config['output_root'] = temp
            all_jobs = archive.tasks(config)
            inventory = {'counts': {job['key']: 1 for job in all_jobs},
                         'roi_hashes': {r['code']: optical.hash_object({}) for r in config['regions']}}
            called = []
            class Child:
                def __init__(self, argv, **_):
                    self.directory = Path(argv[argv.index('--out')+1])
                    called.append(self.directory.relative_to(temp).parts[:2])
                    self.directory.mkdir(parents=True, exist_ok=True)
                    (self.directory/'run_manifest.json').write_text(json.dumps({'scenes':[{}]}))
                def wait(self, **_): return 0
                def poll(self): return 0
            with patch.object(archive, 'output_lock', side_effect=lambda _: nullcontext()), \
                 patch.object(optical, 'load_roi', return_value=(None, None, {})), \
                 patch.object(archive.subprocess, 'Popen', side_effect=Child), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(archive.run_archive(config, inventory), 0)
            self.assertEqual([code for code, _ in called], ['small']*6+['big']*6)
            for code in ('small','big'):
                self.assertEqual(json.loads((Path(temp)/code/'region_complete.json').read_text())['status'], 'all_months_validated')
    def test_failure_stops_next_month_and_never_marks_region_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            config = self.config()
            config['output_root'] = temp
            jobs = archive.tasks(config)
            inventory = {'counts': {j['key']: 1 for j in jobs}, 'roi_hashes': {r['code']: optical.hash_object({}) for r in config['regions']}}
            from unittest.mock import MagicMock
            child = MagicMock()
            child.wait.return_value = 1
            child.poll.return_value = 1
            with patch.object(archive, 'output_lock', side_effect=lambda _: nullcontext()), \
                 patch.object(optical, 'load_roi', return_value=(None,None,{})), \
                 patch.object(archive.subprocess, 'Popen', return_value=child) as popen, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(archive.run_archive(config, inventory), 1)
            self.assertEqual(popen.call_count, 1)
            self.assertEqual(list(Path(temp).glob('*/region_complete.json')), [])


if __name__ == "__main__":
    unittest.main()
