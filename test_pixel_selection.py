"""Exercise selection and resume against real synthetic GeoTIFF transfers."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import Affine

import download_optical as hls
from pixel_selection import SelectionPlanner, observation_key


class Feature:
    def __init__(self, geometry, properties=None):
        self.geometry, self.properties = geometry, properties or {}

    def toDictionary(self, names):
        return {name: self.properties[name] for name in names}


class Collection:
    def __init__(self, features):
        self.features = features

    def map(self, function):
        return Collection([function(f) for f in self.features])

    def getInfo(self):
        return {"features": [{"properties": f.properties} for f in self.features]}


class Image:
    def __init__(self, owner, values, names=hls.BANDS):
        self.owner, self.values, self.names = owner, np.asarray(values), tuple(names)

    def reproject(self, **kwargs):
        return self

    def select(self, name):
        return Image(self.owner, self.values[[self.names.index(name)]], [name])

    def neq(self, value):
        return Image(self.owner, self.values != value, self.names)

    def rename(self, name):
        return Image(self.owner, self.values, [name])

    def addBands(self, other):
        return Image(self.owner, np.concatenate([self.values, other.values]), self.names + other.names)

    def Or(self, other):
        return Image(self.owner, np.logical_or(self.values, other.values), self.names)

    def Not(self):
        return Image(self.owner, np.logical_not(self.values), self.names)

    def updateMask(self, mask):
        result = self.values.copy()
        result[:, ~mask.values[0].astype(bool)] = hls.NODATA
        return Image(self.owner, result, self.names)

    def unmask(self, value, **kwargs):
        return Image(self.owner, np.where(self.values == hls.NODATA, value, self.values), self.names)

    def clipToCollection(self, collection):
        allowed = np.zeros(self.values.shape[1:], dtype=bool)
        for feature in collection.features:
            rows, cols = self.owner.window(feature.geometry)
            allowed[rows, cols] = True
        result = self.values.astype('float32').copy()
        result[:, ~allowed] = hls.NODATA
        return Image(self.owner, result, self.names)

    def reduceRegions(self, collection, **kwargs):
        self.owner.reductions += 1
        self.owner.reduce_parameters.append(kwargs)
        features = []
        for f in collection.features:
            rows, cols = self.owner.window(f.geometry)
            p = dict(f.properties)
            for band, name in enumerate(self.names):
                p[name] = int(self.values[band, rows, cols].sum())
            features.append(Feature(f.geometry, p))
        return Collection(features)

    def getDownloadURL(self, parameters):
        transform = parameters['crs_transform']
        width, height = parameters['dimensions']
        rows, cols = self.owner.window([transform[2], transform[5] - height * 30,
                                       transform[2] + width * 30, transform[5]])
        values = self.values[:, rows, cols].astype('float32')
        with MemoryFile() as memory:
            with memory.open(driver='GTiff', width=width, height=height, count=8,
                             dtype='float32', crs=parameters['crs'], transform=Affine(*transform)) as dst:
                dst.write(values)
            body = memory.read()
        url = str(len(self.owner.responses))
        self.owner.responses[url] = body
        return url


class FakeEE:
    Feature = Feature
    FeatureCollection = Collection

    class Geometry:
        @staticmethod
        def Rectangle(bounds, **kwargs):
            class Bounds(list):
                def transform(self, *args): return self
            return Bounds(bounds)

    class Reducer:
        @staticmethod
        def sum():
            class Reducer:
                def unweighted(self): return self
            return Reducer()

    def __init__(self):
        self.sources, self.responses, self.http_calls = {}, {}, []
        self.reductions, self.reduce_parameters = 0, []
        self.Image = self

    def __call__(self, name):
        return self.sources[name]

    def constant(self, value):
        return Image(self, np.full((1, 64, 128), value), ['constant'])

    def window(self, bounds):
        left, bottom, right, top = bounds
        col = int(round((left - 300000) / 30))
        row = int(round((3300000 - top) / 30))
        return slice(row, row + int(round((top - bottom) / 30))), slice(col, col + int(round((right - left) / 30)))

    def session(self):
        owner = self
        class Response:
            def __init__(self, body): self.body = body
            def __enter__(self): return self
            def __exit__(self, *_args): pass
            def raise_for_status(self): pass
            def iter_content(self, _size): yield self.body
        class Session:
            def __init__(self): self.proxies = {}
            def close(self): pass
            def get(self, url, **kwargs):
                owner.http_calls.append(url)
                return Response(owner.responses[url])
        return Session()


class SelectionTests(unittest.TestCase):
    def setUp(self):
        hls.configure('L8')
        hls.SENSORS = dict(hls.CATALOG)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard = hls.MountGuard(self.root)
        self.ee = FakeEE()
        self.tiles = [hls.Tile(0, c, 300000 + c * 30, 3300000, 64, 64) for c in (0, 64)]
        self.crs, self.recipe = 'EPSG:32650', 'recipe'
        self.patch = patch.object(hls, 'build_image', side_effect=lambda ee, source, *_: source)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def scene(self, name, start=0, end=128, timestamp=1000, sensor='L8'):
        scene = {'id': hls.SENSORS[sensor][0] + '/' + name, 'index': name,
                 'sensor': sensor, 'time_ms': timestamp}
        values = np.empty((8, 64, 128), dtype='float32')
        for band, value in enumerate((.1, .3, .2, .5, 1.6, 64, 64, 0)):
            values[band] = value
        values[:, :, :start] = hls.NODATA
        values[:, :, end:] = hls.NODATA
        self.ee.sources[scene['id']] = Image(self.ee, values)
        return scene

    def planner(self, scenes):
        return SelectionPlanner(hls, self.ee, self.guard, scenes, self.tiles, self.crs,
                                self.recipe, None, 1, 'low')

    def transfer(self, planner, scenes, relevant=None):
        with patch('requests.Session', side_effect=self.ee.session), contextlib.redirect_stdout(io.StringIO()):
            with hls.TileDownloadPool(1, 'none') as pool:
                for scene in scenes:
                    planner.process(scene, self.tiles if relevant is None else relevant, pool)

    def record(self, scene):
        return json.loads((self.root / scene['sensor'] / scene['index'] / 'scene.json').read_text())

    def test_empty_and_full_overlap_skip_before_raster_transfer_and_keep_time_sensor(self):
        a = self.scene('A')
        b = self.scene('B')
        c = self.scene('C', timestamp=1001)
        d = self.scene('D', sensor='L9')
        empty = self.scene('EMPTY', start=128, timestamp=2000)
        scenes = [a, b, c, d, empty]
        planner = self.planner(scenes)
        self.transfer(planner, scenes)
        self.assertEqual(len(self.ee.http_calls), 6)  # A, C, D: 2 tiles each.
        self.assertEqual(self.record(b)['status'], 'skipped_overlap')
        self.assertEqual(self.record(empty)['status'], 'skipped_no_clear')
        self.assertEqual(self.record(b)['tiles'], [])
        self.assertEqual(self.record(c)['valid_pixels'], 8192)
        self.assertEqual(self.record(d)['valid_pixels'], 8192)
        self.assertNotEqual(observation_key(a), observation_key(c))
        self.assertNotEqual(observation_key(a), observation_key(d))
        self.assertTrue(all('crsTransform' in p and 'scale' not in p for p in self.ee.reduce_parameters))
        summary = json.loads((self.root / 'download_selection_summary.json').read_text())
        self.assertEqual(summary['skipped_no_clear_scenes'], 1)
        self.assertEqual(summary['skipped_overlap_scenes'], 1)

    def test_same_time_preserves_clear_union_and_fills_cloud_gaps(self):
        a, b = self.scene('A', end=96), self.scene('B', start=32)
        planner = self.planner([a, b])
        self.transfer(planner, [a, b])
        self.assertEqual(self.record(a)['valid_pixels'], 6144)
        self.assertEqual(self.record(b)['valid_pixels'], 2048)
        self.assertEqual(len(self.ee.http_calls), 3)  # B left tile entirely covered.
        masks = []
        for scene in (a, b):
            mask = np.zeros((64, 128), dtype=bool)
            for item in self.record(scene)['tiles']:
                tile = planner.tiles[item['file']]
                with rasterio.open(self.root / scene['sensor'] / scene['index'] / item['file']) as src:
                    mask[:, tile.col:tile.col + tile.width] = src.read(1) != hls.NODATA
            masks.append(mask)
        self.assertFalse((masks[0] & masks[1]).any())
        self.assertTrue((masks[0] | masks[1]).all())

    def test_future_legacy_partial_has_priority_and_keeps_original_bytes(self):
        a, b = self.scene('A', end=96), self.scene('B', start=32)
        tile = self.tiles[0]
        legacy = self.root / 'L8' / 'B' / tile.name
        with patch('requests.Session', side_effect=self.ee.session):
            hls.download_tile(self.ee.sources[b['id']], tile, legacy, self.crs, self.ee.session(),
                              self.guard, self.recipe, 'low', b['id'])
        original_hash = hls.file_hash(legacy)
        planner = self.planner([a, b])
        self.transfer(planner, [a, b])
        self.assertEqual(hls.file_hash(legacy), original_hash)
        self.assertEqual(self.record(a)['valid_pixels'], 4096)
        self.assertEqual(self.record(b)['valid_pixels'], 4096)
        self.assertEqual(len(self.ee.http_calls), 4)  # Existing B + 3 novel tiles.
        self.assertNotIn('selection_hash', json.loads(legacy.with_suffix('.json').read_text()))
        # A repeat reads the immutable plan, without another request.
        self.transfer(self.planner([a, b]), [a, b])
        self.assertEqual(len(self.ee.http_calls), 4)

    def test_failed_scene_resumes_optimized_tile_without_requesting_it_again(self):
        a = self.scene('A')
        planner = self.planner([a])
        original = hls.download_tile
        def failure(image, tile, *args, **kwargs):
            if tile.col == 64:
                raise hls.DownloadError('synthetic interruption')
            return original(image, tile, *args, **kwargs)
        with patch.object(hls, 'download_tile', side_effect=failure), self.assertRaises(hls.DownloadError):
            self.transfer(planner, [a])
        self.assertFalse((self.root / 'L8' / 'A' / 'scene.json').exists())
        saved = self.root / 'L8' / 'A' / self.tiles[0].name
        digest = hls.file_hash(saved)
        reductions = self.ee.reductions
        self.transfer(self.planner([a]), [a])
        self.assertEqual(self.ee.reductions, reductions)
        self.assertEqual(hls.file_hash(saved), digest)
        self.assertEqual(len(self.ee.http_calls), 2)

    def test_lost_preserved_tile_and_changed_plan_stop_without_transfer(self):
        a = self.scene('A')
        legacy = self.root / 'L8' / 'A' / self.tiles[0].name
        with patch('requests.Session', side_effect=self.ee.session):
            hls.download_tile(self.ee.sources[a['id']], self.tiles[0], legacy, self.crs,
                self.ee.session(), self.guard, self.recipe, 'low', a['id'])
        planner = self.planner([a])
        legacy.write_bytes(b'corrupt')
        with self.assertRaises(hls.DownloadError):
            self.planner([a])
        self.assertEqual(len(self.ee.http_calls), 1)
        self.assertEqual(legacy.read_bytes(), b'corrupt')
        b = self.scene('B', timestamp=2000)
        with self.assertRaises(hls.DownloadError):
            self.planner([a, b])

    def test_network_preflight_failure_is_recoverable_without_blank_skip_marker(self):
        import requests
        a = self.scene('A')
        planner = self.planner([a])
        with patch.object(Image, 'reduceRegions', side_effect=requests.exceptions.Timeout()), \
                self.assertRaises(hls.TransientDownloadError):
            self.transfer(planner, [a])
        self.assertEqual(len(self.ee.http_calls), 0)
        self.assertFalse((self.root / 'L8' / 'A' / 'scene.json').exists())

    def test_negative_offline_resume_probe_never_loads_roi_or_connects_network(self):
        args = hls.parser().parse_args(["--sensor", "L8", '--roi', 'unused.shp', '--start', '2013-01-01',
            '--end', '2013-02-01', '--out', str(self.root), '--resume-complete-only'])
        with patch.object(hls, 'load_roi', side_effect=AssertionError('ROI work for incomplete hint')), \
                patch.object(hls, 'initialize_ee', side_effect=AssertionError('network access')):
            self.assertEqual(hls.run(args), 3)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
