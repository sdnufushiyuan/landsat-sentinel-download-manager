"""Explicit small EE probe: at most 5 scenes/sensor, one tiny ROI/tile each."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import requests
from shapely.geometry import box
import geopandas as gpd
import numpy as np
import rasterio
import archive
import download_optical as optical


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    config = archive.read_config(args.config)
    project, _ = optical.resolve_project(config.get('project'))
    ee = optical.initialize_ee(project, config.get('proxy'))
    ee.data.setDeadline(120000)
    region = next(item for item in config['regions'] if item['code'] == 'SDR')
    roi, crs, _ = optical.load_roi(region['roi'], region['crs'])
    center = roi.representative_point()
    small = roi.intersection(box(center.x-960, center.y-960, center.x+960, center.y+960))
    frame = gpd.GeoDataFrame(geometry=[small], crs=crs).to_crs('EPSG:4326')
    geometry = ee.Geometry(frame.geometry.iloc[0].__geo_interface__, proj='EPSG:4326', geodesic=False)
    collection = ee.FeatureCollection([ee.Feature(geometry)])
    guard = optical.MountGuard(args.out)
    guard.check()
    proxy = config.get('proxy')
    session = requests.Session()
    session.trust_env = False
    if optical.parse_proxy(proxy):
        session.proxies.update({'http': proxy, 'https': proxy})
    report = {'created_utc': datetime.now(timezone.utc).isoformat(), 'ee_api_version': ee.__version__,
        'scope': 'SDR interior ~1.92km square, 30m output, at most 5 candidates/sensor',
        'sensors': {}, 'bulk_download_started': False}
    try:
        for sensor, (asset, _) in optical.CATALOG.items():
            optical.configure(sensor, 30)
            cloud = 'CLOUDY_PIXEL_PERCENTAGE' if sensor == 'S2' else 'CLOUD_COVER'
            candidates = ee.ImageCollection(asset).filterBounds(geometry).filterDate('2023-05-01', '2023-10-01').sort(cloud).limit(5)
            ids = candidates.aggregate_array('system:index').getInfo()
            tile = optical.make_tiles(small, 128)[0]
            saved = None
            for index in ids:
                scene_id = asset+'/'+index
                source = ee.Image(scene_id)
                image = optical.build_image(ee, source, sensor, collection, 1, config.get('aerosol','low')).reproject(crs=crs.to_string(), crsTransform=tile.transform)
                tile_geometry = ee.Geometry.Rectangle(list(tile.bounds), proj=crs.to_string(), geodesic=False).transform('EPSG:4326', 1)
                count = image.select('red').neq(optical.NODATA).reduceRegion(reducer=ee.Reducer.sum().unweighted(),
                    geometry=tile_geometry, crs=crs.to_string(), crsTransform=tile.transform,
                    maxPixels=16384, tileScale=4).get('red').getInfo()
                if not count:
                    continue
                recipe = {'sensor': sensor, 'collection': asset, 'crs': crs.to_string(), 'transform': tile.transform,
                    'mask': optical.quality_recipe(sensor, config.get('aerosol','low')), 'roi': frame.geometry.iloc[0].__geo_interface__}
                path = args.out/sensor/tile.name
                stats, reused = optical.download_tile(image, tile, path, crs.to_string(), session, guard,
                    optical.hash_object(recipe), config.get('aerosol','low'), scene_id)
                with rasterio.open(path) as dataset:
                    values = dataset.read()
                    valid = values[0] != optical.NODATA
                    red, nir, swir = values[:3, valid]
                    ndvi_error = float(np.max(np.abs(values[3,valid] - (nir-red)/(nir+red))))
                    str_error = float(np.max(np.abs(values[4,valid] - (1-swir)**2/(2*swir))))
                if stats['valid_pixels'] != int(count):
                    print(json.dumps({'sensor': sensor, 'server_count': count, 'tile_count': stats['valid_pixels'],
                        'dimensions': [tile.width,tile.height], 'transform': tile.transform}), flush=True)
                    raise ValueError('Server count differs from downloaded pixels')
                saved = {'scene_id': scene_id, 'source_bands': list(optical.CATALOG[sensor][1]),
                    'source_projection': source.select(optical.CATALOG[sensor][1][0]).projection().getInfo(),
                    'output_resolution_m': 30, 'dimensions': [tile.width,tile.height],
                    'server_valid_pixels': int(count), **stats, 'local_ndvi_max_abs_error': ndvi_error,
                    'local_str_max_abs_error': str_error, 'stored_bytes': path.stat().st_size,
                    'sha256': optical.file_hash(path), 'reused': reused, 'recipe': recipe}
                break
            if saved is None:
                raise ValueError('No clear-land probe found for '+sensor+' within bounded candidates')
            report['sensors'][sensor] = saved
            optical.write_json(args.out/'real_sample_report.json', report, guard)
            print(sensor+': real tile passed QA, grid, index math and server/local pixel-count checks', flush=True)
    finally:
        session.close()
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        import traceback
        locations = [(Path(frame.filename).name, frame.lineno, frame.name) for frame in traceback.extract_tb(error.__traceback__)]
        print('Probe failed: '+type(error).__name__+' at '+str(locations)+'; bulk download was not started', file=sys.stderr)
        raise SystemExit(1)
