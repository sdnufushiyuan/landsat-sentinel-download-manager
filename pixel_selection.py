"""Exact-grid clear-pixel selection with an immutable, legacy-aware resume plan.

No time compositing: only equal (sensor, time_ms) observations share coverage.
Already committed legacy tiles take priority, including tiles of later scenes.
The plan is separate from the spectral recipe so existing files remain usable.
The caller owns ee.Initialize. Fixed reproject/crsTransform are necessary here
to deduplicate the exact exported pixels; scale would be mutually exclusive.
Each bounded RPC returns at most 32 pairs of counts, never raster arrays.
"""
from collections import defaultdict
import json
import math

POLICY = "native-sr-clear-land-exact-time-unique-pixels-v1"
BATCH_SIZE = 32


def observation_key(scene):
    return scene["sensor"], scene["time_ms"]


def completed_record(api, root, scene, recipe_hash, tiles_by_name):
    """Verify every committed tile, not just the presence of completion markers."""
    directory = root / scene["sensor"] / scene["index"]
    path = directory / "scene.json"
    if not path.exists():
        return None
    if directory.is_symlink() or path.is_symlink():
        raise api.DownloadError("景完成记录是符号链接，已停止。")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if (record.get("recipe_hash") != recipe_hash or
                any(record.get(k) != scene[k] for k in ("id", "sensor", "index")) or
                not isinstance(record.get("tiles"), list)):
            raise ValueError()
        seen = set()
        for item in record["tiles"]:
            name = item["file"]
            if name not in tiles_by_name or name in seen:
                raise ValueError()
            seen.add(name)
            tile_path = directory / name
            sidecar = tile_path.with_suffix(".json")
            if tile_path.is_symlink() or sidecar.is_symlink():
                raise ValueError()
            if not tile_path.is_file() or not sidecar.is_file():
                raise ValueError()
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            if (data.get("recipe_hash") != recipe_hash or data.get("scene_id") != scene["id"] or
                    not isinstance(data.get("sha256"), str) or len(data["sha256"]) != 64):
                raise ValueError()
            if api.cached_tile(tile_path, tiles_by_name[name], record["crs"], recipe_hash,
                    record.get("aerosol", "low"), scene["id"],
                    selection_hash=data.get("selection_hash")) is None:
                raise ValueError()
        return record
    except (KeyError, TypeError, ValueError):
        raise api.DownloadError("已有景完成记录/块记录不完整；为保护旧数据，停止而不覆盖。") from None


class SelectionPlanner:
    def __init__(self, api, ee, guard, scenes, tiles, crs, recipe_hash, roi_fc, factor, aerosol):
        self.api, self.ee, self.guard = api, ee, guard
        self.scenes, self.crs, self.recipe_hash = scenes, crs, recipe_hash
        self.roi_fc, self.factor, self.aerosol = roi_fc, factor, aerosol
        self.tiles = {tile.name: tile for tile in tiles}
        self.transform = tiles[0].transform
        self.groups = defaultdict(list)
        for scene in scenes:
            self.groups[observation_key(scene)].append(scene)
        self.group_images = {}
        self.loaded_group = None
        self.records = {}
        self.plan = self._load_or_create_plan()
        self.selection_hash = api.hash_object(self.plan)

    def _load_or_create_plan(self):
        api, guard = self.api, self.guard
        plan_path = guard.output / "download_selection_plan.json"
        expected = {"schema_version": 1, "policy": POLICY, "recipe_hash": self.recipe_hash,
                    "scene_ids": [s["id"] for s in self.scenes],
                    "grid": {"crs": self.crs, "transform": self.transform}}
        if plan_path.exists():
            if plan_path.is_symlink():
                raise api.DownloadError("下载筛选计划是符号链接，已停止。")
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            if any(plan.get(k) != v for k, v in expected.items()) or not isinstance(plan.get("preserved"), dict):
                raise api.DownloadError("已有下载筛选计划不兼容，不能自动改写旧数据。")
            known = {s["id"]: s for s in self.scenes}
            for scene_id, saved in plan["preserved"].items():
                if scene_id not in known or not isinstance(saved, dict):
                    raise api.DownloadError("下载筛选计划包含未知旧景。")
                scene = known[scene_id]
                for name, entry in saved.items():
                    if name not in self.tiles or not isinstance(entry, dict):
                        raise api.DownloadError("下载筛选计划包含未知旧块。")
                    path = guard.output / scene["sensor"] / scene["index"] / name
                    if path.is_symlink() or path.with_suffix(".json").is_symlink() or not path.is_file() or api.file_hash(path) != entry.get("sha256"):
                        raise api.DownloadError("筛选计划引用的旧块已丢失或改变，停止以保护观测覆盖。")
            return plan
        preserved = {}
        print("正在校验旧断点，并冻结下载筛选计划。", flush=True)
        # Capture ALL committed pre-upgrade tiles before downloading anything.
        # Future-scene partials thus own their valid pixels before new transfers.
        for position, scene in enumerate(self.scenes, 1):
            guard.check()
            directory = guard.output / scene["sensor"] / scene["index"]
            if directory.is_symlink():
                raise api.DownloadError("已有景目录是符号链接，已停止。")
            saved = {}
            if directory.exists():
                for path in sorted(directory.glob("*.tif")):
                    tile = self.tiles.get(path.name)
                    if tile is None or path.is_symlink() or path.with_suffix(".json").is_symlink():
                        raise api.DownloadError("已有块不属于当前网格，停止而不覆盖。")
                    data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                    if data.get("selection_hash"):
                        raise api.DownloadError("发现优化块但缺少其筛选计划，停止而不覆盖。")
                    stats = api.cached_tile(path, tile, self.crs, self.recipe_hash, self.aerosol, scene["id"])
                    if stats is None:
                        raise api.DownloadError("已有块未通过校验，停止而不重新下载/覆盖旧文件。")
                    saved[path.name] = {"sha256": data["sha256"], **stats}
            if saved:
                preserved[scene["id"]] = saved
            if position % 25 == 0:
                print(f"  旧断点校验 {position}/{len(self.scenes)} 景。", flush=True)
        plan = {**expected, "preserved": preserved,
                "grouping": "sensor + exact system:time_start; no date rounding",
                "priority": "preserved legacy pixels, then time_ms/id order"}
        api.write_json(plan_path, plan, guard)
        return plan

    def base_image(self, scene):
        key = observation_key(scene)
        if self.loaded_group != key:
            self.group_images.clear()
            self.loaded_group = key
        if scene["id"] not in self.group_images:
            self.group_images[scene["id"]] = self.api.build_image(
                self.ee, self.ee.Image(scene["id"]), scene["sensor"], self.roi_fc,
                self.factor, self.aerosol).reproject(crs=self.crs, crsTransform=self.transform)
        return self.group_images[scene["id"]]

    def valid(self, scene):
        return self.base_image(scene).select("red").neq(self.api.NODATA).rename("valid")

    def selected_image(self, scene):
        ee = self.ee
        covered = ee.Image.constant(0).reproject(crs=self.crs, crsTransform=self.transform)
        group = self.groups[observation_key(scene)]
        for previous in group:
            if previous["id"] == scene["id"]:
                break
            covered = covered.Or(self.valid(previous))
        # Future legacy partials must not be downloaded again by an earlier scene.
        for other in group:
            if other["id"] == scene["id"]:
                continue
            preserved = self.plan["preserved"].get(other["id"], {})
            if not preserved:
                continue
            regions = [ee.Feature(ee.Geometry.Rectangle(list(self.tiles[name].bounds),
                       proj=self.crs, geodesic=False).transform("EPSG:4326", 1)) for name in preserved]
            legacy = self.valid(other).clipToCollection(ee.FeatureCollection(regions)).unmask(0)
            covered = covered.Or(legacy)
        return self.base_image(scene).updateMask(covered.Not()).unmask(self.api.NODATA, sameFootprint=False)

    def cached(self, scene, relevant):
        api = self.api
        directory = self.guard.output / scene["sensor"] / scene["index"]
        preserved = self.plan["preserved"].get(scene["id"], {})
        result = {}
        for tile in relevant:
            path = directory / tile.name
            if not path.exists() and not path.with_suffix(".json").exists():
                if tile.name in preserved:
                    raise api.DownloadError("筛选计划引用的旧块已丢失，停止以避免遗漏像元。")
                continue
            expected_selection = None if tile.name in preserved else self.selection_hash
            stats = api.cached_tile(path, tile, self.crs, self.recipe_hash, self.aerosol,
                                    scene["id"], selection_hash=expected_selection)
            if stats is None:
                raise api.DownloadError("已有块未通过校验，停止而不重新下载/覆盖旧文件。")
            if tile.name in preserved and api.file_hash(path) != preserved[tile.name]["sha256"]:
                raise api.DownloadError("筛选计划引用的旧块已改变，停止以避免遗漏像元。")
            result[tile.name] = {"file": tile.name, **stats}
        return result

    def preflight(self, scene, image, relevant, cached):
        """Bounded server reductions on exactly the requested output grid."""
        api, ee = self.api, self.ee
        path = self.guard.output / scene["sensor"] / scene["index"] / "pixel_selection.json"
        if path.exists():
            if path.is_symlink():
                raise api.DownloadError("晴空筛选记录是符号链接，已停止。")
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("selection_hash") != self.selection_hash or record.get("scene_id") != scene["id"]:
                raise api.DownloadError("晴空筛选记录不兼容，不能自动覆盖。")
            counts = record["counts"]
        else:
            counts = {}
        missing = [tile for tile in relevant if tile.name not in cached and tile.name not in counts]
        if missing:
            stats_image = self.valid(scene).rename("clear").addBands(
                image.select("red").neq(api.NODATA).rename("novel"))
            for start in range(0, len(missing), BATCH_SIZE):
                self.guard.check()
                batch = missing[start:start + BATCH_SIZE]
                features = ee.FeatureCollection([ee.Feature(ee.Geometry.Rectangle(list(tile.bounds),
                    proj=self.crs, geodesic=False).transform("EPSG:4326", 1), {"file": tile.name}) for tile in batch])
                # A small metadata response, no raster transfer and no bestEffort.
                try:
                    reduced = stats_image.reduceRegions(collection=features, reducer=ee.Reducer.sum().unweighted(),
                        crs=self.crs, crsTransform=self.transform, tileScale=4,
                        # EE budgets pixels using the transformed region's bbox;
                        # that can exceed its actual rotated grid-cell footprint.
                        maxPixelsPerRegion=16 * max(t.width * t.height for t in batch) + 1024)
                    data = reduced.map(lambda f: ee.Feature(None, f.toDictionary(["file", "clear", "novel"]))).getInfo()
                except Exception as error:
                    if api.is_transient_error(error):
                        raise api.TransientDownloadError("晴空有效像元预检暂时网络失败，可按原筛选计划续跑。") from None
                    raise api.DownloadError(f"晴空有效像元预检失败（{type(error).__name__}），未下载该批影像。") from None
                expected = {t.name: t for t in batch}
                seen = set()
                for feature in data["features"]:
                    p = feature["properties"]
                    name = p["file"]
                    if name not in expected or name in seen:
                        raise api.DownloadError("晴空预检返回了不一致的网格。")
                    seen.add(name)
                    row = {}
                    for field in ("clear", "novel"):
                        value = p.get(field) or 0
                        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                                not math.isfinite(value) or value != int(value) or
                                not 0 <= value <= expected[name].width * expected[name].height):
                            raise api.DownloadError("晴空预检返回了不合法的像元数。")
                        row[field] = int(value)
                    if row["novel"] > row["clear"]:
                        raise api.DownloadError("去重后有效像元数大于原始晴空像元数。")
                    counts[name] = row
                if seen != set(expected):
                    raise api.DownloadError("晴空预检缺少网格结果，不能将缺项当作空块。")
                api.write_json(path, {"policy": POLICY, "selection_hash": self.selection_hash,
                    "scene_id": scene["id"], "counts": counts}, self.guard)
        for name, row in counts.items():
            if name not in self.tiles or not isinstance(row, dict) or any(type(row.get(k)) is not int or row[k] < 0 for k in ("clear", "novel")) or row["novel"] > row["clear"]:
                raise api.DownloadError("缓存的晴空预检结果无效，停止而不覆盖。")
        return counts

    def process(self, scene, relevant, pool):
        api = self.api
        old = completed_record(api, self.guard.output, scene, self.recipe_hash, self.tiles)
        if old is not None:
            print("  已有景完成记录，复用；不重新下载。", flush=True)
            self.records[scene["id"]] = old
            self.summary()
            return
        cached = self.cached(scene, relevant)
        image = self.selected_image(scene)
        counts = self.preflight(scene, image, relevant, cached)
        needed = [tile for tile in relevant if tile.name not in cached and counts[tile.name]["novel"] > 0]
        skipped = [{"file": tile.name, "reason": "no_clear_pixels" if counts[tile.name]["clear"] == 0 else "covered_same_observation",
                    "clear_pixels": counts[tile.name]["clear"], "valid_pixels": 0}
                   for tile in relevant if tile.name not in cached and counts[tile.name]["novel"] == 0]
        print(f"  下载前筛选：复用 {len(cached)} 块；需下载 {len(needed)} 块；跳过空/重复块 {len(skipped)}。", flush=True)
        directory = self.guard.output / scene["sensor"] / scene["index"]
        downloaded = pool.download_scene(image, needed, directory, self.crs, self.guard,
            self.recipe_hash, self.aerosol, scene, selection_hash=self.selection_hash)
        for item in downloaded:
            if item["valid_pixels"] != counts[item["file"]]["novel"]:
                raise api.DownloadError("实际落盘像元数与下载前预检不一致；未标记该景完成。")
        results = {**cached, **{x["file"]: x for x in downloaded}}
        completed = [results[t.name] for t in relevant if t.name in results]
        clear_sum = sum(x["clear"] for x in counts.values()) + sum(x["valid_pixels"] for x in cached.values())
        status = ("downloaded" if completed else "skipped_no_clear" if clear_sum == 0 else "skipped_overlap")
        record = {**scene, "recipe_hash": self.recipe_hash, "selection_policy": POLICY,
            "crs": self.crs, "aerosol": self.aerosol,
            "selection_hash": self.selection_hash, "status": status,
            "source_bands": list(api.SENSORS[scene["sensor"]][1]), "tiles": completed,
            "skipped_tiles": skipped, "valid_pixels": sum(x["valid_pixels"] for x in completed)}
        api.write_json(directory / "scene.json", record, self.guard)
        self.records[scene["id"]] = record
        self.summary()

    def summary(self):
        # A restart recovers existing markers once, then only writes a small table.
        if not getattr(self, "recovered_records", False):
            for scene in self.scenes:
                old = completed_record(self.api, self.guard.output, scene, self.recipe_hash, self.tiles)
                if old is not None:
                    self.records[scene["id"]] = old
            self.recovered_records = True
        counts = {"processed_scenes": len(self.records),
                  "stored_tiles": sum(len(r.get("tiles", [])) for r in self.records.values()),
                  "stored_bytes": sum((self.guard.output / r["sensor"] / r["index"] / tile["file"]).stat().st_size
                      for r in self.records.values() for tile in r.get("tiles", [])),
                  "skipped_no_clear_scenes": sum(r.get("status") == "skipped_no_clear" for r in self.records.values()),
                  "skipped_overlap_scenes": sum(r.get("status") == "skipped_overlap" for r in self.records.values()),
                  "skipped_empty_tiles": sum(len(r.get("skipped_tiles", [])) for r in self.records.values())}
        self.api.write_json(self.guard.output / "download_selection_summary.json", {
            "policy": POLICY, "selection_hash": self.selection_hash, "recipe_hash": self.recipe_hash, **counts}, self.guard)
