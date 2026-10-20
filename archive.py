"""Independent monthly native-SR archive; dry planning is the default."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import threading

import download_optical as optical

SENSOR_START = {"L8": "2013-03-18", "L9": "2021-10-31", "S2": "2017-03-28"}
FOLDERS = {"L8": "Landsat8", "L9": "Landsat9", "S2": "Sentinel2"}
CODE = re.compile(r"[A-Za-z0-9_-]{1,32}")


def months(start, end):
    cursor = date.fromisoformat(start)
    finish = date.fromisoformat(end)
    if cursor >= finish:
        raise ValueError("开始日期必须早于结束日期")
    result = []
    while cursor < finish:
        following = date(cursor.year+1, 1, 1) if cursor.month == 12 else date(cursor.year, cursor.month+1, 1)
        result.append((cursor.isoformat(), min(following, finish).isoformat()))
        cursor = following
    if len(result) > 600:
        raise ValueError("单次计划不得超过600个月")
    return result


def read_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    months(config["start"], config["end_exclusive"])
    regions = config["regions"]
    codes = [item["code"] for item in regions]
    if not regions or len(set(codes)) != len(codes) or not all(CODE.fullmatch(code) for code in codes):
        raise ValueError("研究区代码必须唯一，且仅含字母、数字、下划线或连字符")
    for item in regions:
        if not math.isfinite(item["area_m2"]) or item["area_m2"] <= 0:
            raise ValueError("研究区面积必须为正的有限数")
        if not (Path(item["roi"]).is_absolute() or PurePosixPath(item["roi"]).is_absolute()) or not item["crs"]:
            raise ValueError("ROI必须为绝对路径，并明确提供米制CRS")
    if not (Path(config["output_root"]).is_absolute() or PurePosixPath(config["output_root"]).is_absolute()):
        raise ValueError("输出根目录必须为绝对路径")
    if type(config.get("workers", 2)) is not int or not 1 <= config.get("workers", 2) <= 8:
        raise ValueError("workers必须为1–8")
    resolution = config.get("s2_resolution", 30)
    if resolution not in (10, 20, 30):
        raise ValueError("S2输出网格必须为10、20或30米")
    if config.get("aerosol", "low") not in ("low", "climatology-low"):
        raise ValueError("Landsat气溶胶策略无效")
    aot = config.get("s2_max_aot")
    if aot is not None and (type(aot) not in (float, int) or not math.isfinite(aot) or aot < 0):
        raise ValueError("S2 AOT上限必须是非负有限数")
    optical.parse_proxy(config.get("proxy", "http://127.0.0.1:8118"))
    return config


def config_hash(config):
    # Location, dates and scientific settings are immutable. Worker count,
    # proxy route and project may change without invalidating scientific data.
    keys = ("regions", "start", "end_exclusive", "output_root", "s2_resolution", "aerosol", "s2_max_aot")
    return optical.hash_object({key: config.get(key) for key in keys})


def tasks(config, counts=None):
    result = []
    root = Path(config["output_root"])
    for region in config["regions"]:
        for sensor in optical.CATALOG:
            start = max(config["start"], SENSOR_START[sensor])
            if start >= config["end_exclusive"]:
                continue
            for begin, end in months(start, config["end_exclusive"]):
                name = f"{region['code']}/{sensor}/{begin}__{end}"
                count = None if counts is None else counts[name]
                result.append({"key": name, "region": region["code"], "sensor": sensor,
                    "start": begin, "end": end, "scene_count": count,
                    "roi": region["roi"], "crs": region["crs"], "area_m2": region["area_m2"],
                    "resolution": config.get("s2_resolution", 30) if sensor == "S2" else 30,
                    "out": str(root / region["code"] / FOLDERS[sensor] / f"{begin}__{end}")})
    return result


def freeze_inventory(config, path):
    """Explicit metadata operation: at most 36 bounded counts per region/year RPC."""
    if path.exists():
        raise ValueError("库存已冻结；请复用，或使用新的库存文件名")
    project, _ = optical.resolve_project(config.get("project"))
    if not project:
        raise ValueError("请设置EE_PROJECT或私有配置中的project")
    ee = optical.initialize_ee(project, config.get("proxy", "http://127.0.0.1:8118"))
    ee.data.setDeadline(180000)
    planned, counts, geometry_hashes = tasks(config), {}, {}
    for region in config["regions"]:
        _, crs, payload = optical.load_roi(region["roi"], region["crs"])
        from shapely.geometry import shape
        bounds = ee.Geometry.Rectangle(list(shape(payload["features"][0]["geometry"]).bounds), geodesic=False)
        geometry_hashes[region["code"]] = optical.hash_object(payload)
        own = [job for job in planned if job["region"] == region["code"]]
        for year in sorted({job["start"][:4] for job in own}):
            batch = [job for job in own if job["start"].startswith(year)]
            queries = {str(index): ee.ImageCollection(optical.CATALOG[job["sensor"]][0]).filterBounds(bounds)
                .filterDate(job["start"], job["end"]).size() for index, job in enumerate(batch)}
            values = ee.Dictionary(queries).getInfo()
            for index, job in enumerate(batch):
                value = values[str(index)]
                if type(value) is not int or not 0 <= value <= 10000:
                    raise ValueError("月库存不合法或超过10000景，需进一步拆分ROI/时间")
                counts[job["key"]] = value
            print(f"库存 {region['code']} {year}: {len(batch)}个月/传感器计数", flush=True)
    payload = {"schema_version": 1, "config_hash": config_hash(config), "counts": counts,
        "roi_hashes": geometry_hashes, "collections": optical.CATALOG,
        "created_utc": datetime.now(timezone.utc).isoformat(), "ee_api_version": ee.__version__}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)


def load_inventory(config, path):
    inventory = json.loads(path.read_text(encoding="utf-8"))
    if inventory.get("schema_version") != 1 or inventory.get("config_hash") != config_hash(config):
        raise ValueError("库存与配置不一致，不能沿用其他ROI/时间/输出的库存")
    jobs = tasks(config)
    counts = inventory.get("counts", {})
    if set(counts) != {job["key"] for job in jobs} or any(type(v) is not int or not 0 <= v <= 10000 for v in counts.values()):
        raise ValueError("库存缺少月份，或候选计数不合法")
    return inventory


def command(config, job, verify=False):
    argv = [sys.executable, "-u", str(Path(__file__).with_name("download_optical.py")),
        "--sensor", job["sensor"], "--roi", job["roi"], "--crs", job["crs"],
        "--start", job["start"], "--end", job["end"], "--out", job["out"],
        "--resolution", str(job["resolution"]), "--workers", str(config.get("workers", 2)),
        "--max-scenes", "10000", "--proxy", config.get("proxy", "http://127.0.0.1:8118"),
        "--aerosol", config.get("aerosol", "low")]
    for key, flag in (("project", "--project"), ("require_mount", "--require-mount"), ("mount_source", "--mount-source")):
        if config.get(key):
            argv.extend([flag, str(config[key])])
    if job["sensor"] == "S2" and config.get("s2_max_aot") is not None:
        argv.extend(["--s2-max-aot", str(config["s2_max_aot"])])
    if verify:
        argv.append("--resume-complete-only")
    return argv


def ordered_regions(config, jobs):
    """Estimate remaining work for ordering only. Hints never skip validation."""
    cost = {region["code"]: 0 for region in config["regions"]}
    for job in jobs:
        # This marker is only a scheduling hint; each nonempty month is still
        # sent through the downloader's validation before the region completes.
        marker = Path(job["out"]) / "download_selection_summary.json"
        complete_hint = False
        try:
            if marker.is_file() and not marker.is_symlink() and marker.stat().st_size <= 4096:
                hint = json.loads(marker.read_text())
                complete_hint = hint.get("policy") == "native-sr-clear-land-exact-time-unique-pixels-v1" and type(hint.get("processed_scenes")) is int and hint["processed_scenes"] == job["scene_count"]
        except (OSError, ValueError):
            pass
        count = 1 if job["scene_count"] is None else job["scene_count"]
        if not complete_hint:
            cost[job["region"]] += count * job["area_m2"] / job["resolution"]**2
    return sorted(cost, key=lambda code: (cost[code], code))


@contextmanager
def output_lock(root):
    if sys.platform != "linux":
        raise ValueError("正式归档使用Linux文件锁；Windows可运行计划与离线测试")
    import fcntl
    root.mkdir(parents=True, exist_ok=True)
    lock = root / ".native-optical-archive.lock"
    with os.fdopen(os.open(lock, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600), "w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("同一输出已有归档运行，未重复启动") from None
        yield


def run_archive(config, inventory):
    root = Path(config["output_root"])
    guard = optical.MountGuard(root, config.get("require_mount"), config.get("mount_source"))
    guard.check()
    jobs = tasks(config, inventory["counts"])
    cancel = threading.Event()
    child = None
    def stop(_signal, _frame):
        cancel.set()
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with output_lock(root):
            # Recheck geometry before reusing a frozen inventory.
            for region in config["regions"]:
                _, _, payload = optical.load_roi(region["roi"], region["crs"])
                if optical.hash_object(payload) != inventory["roi_hashes"].get(region["code"]):
                    raise ValueError("ROI已改变，请使用新库存与新输出，已有文件未覆盖")
            order = ordered_regions(config, jobs)
            print("研究区顺序（估算剩余工作量）: " + " → ".join(order), flush=True)
            for code in order:
                print(f"开始研究区 {code}；该区三类数据完成后推进下一区", flush=True)
                for job in [item for item in jobs if item["region"] == code]:
                    if cancel.is_set():
                        return 130
                    if job["scene_count"] == 0:
                        print(f"空库存 {job['key']}", flush=True)
                        continue
                    for attempt in range(5):
                        guard.check()
                        print(f"开始月份 {job['key']} 候选{job['scene_count']}", flush=True)
                        # Own process group: stopping the controller drains its
                        # downloader, without affecting the existing HLS service.
                        child = subprocess.Popen(command(config, job), start_new_session=True)
                        if cancel.is_set() and child.poll() is None:
                            try:
                                os.killpg(child.pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                        status = child.wait()
                        child = None
                        if cancel.is_set():
                            return 130
                        if status == 0:
                            manifest = json.loads((Path(job["out"]) / "run_manifest.json").read_text())
                            if len(manifest["scenes"]) != job["scene_count"]:
                                raise ValueError("实际候选数与冻结库存不一致，该区未标记完成")
                            print(f"结束月份 {job['key']}", flush=True)
                            break
                        if status != 75 or attempt == 4:
                            return status if status > 0 else 1
                        print("临时网络失败，300秒后按原断点重试", flush=True)
                        if cancel.wait(300):
                            return 130
                optical.write_json(root / code / "region_complete.json", {
                    "config_hash": config_hash(config), "inventory_sha256": optical.hash_object(inventory),
                    "validated_utc": datetime.now(timezone.utc).isoformat(),
                    "collections": list(optical.CATALOG), "status": "all_months_validated"}, guard)
                print(f"研究区完成 {code}；可以开始该区后续处理", flush=True)
    finally:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, default=Path("runs/inventory.json"))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--query-inventory", action="store_true", help="明确联网冻结月度元数据，不下载栅格")
    mode.add_argument("--run", action="store_true", help="Linux正式下载；必须先冻结库存")
    args = parser.parse_args()
    try:
        config = read_config(args.config)
        if args.query_inventory:
            freeze_inventory(config, args.inventory)
            return 0
        inventory = load_inventory(config, args.inventory) if args.inventory.exists() else None
        if args.run:
            if inventory is None:
                raise ValueError("请先使用--query-inventory冻结本数据集库存，不能复用HLS库存")
            return run_archive(config, inventory)
        jobs = tasks(config, inventory["counts"] if inventory else None)
        print(json.dumps({"mode": "local_plan_only", "config_hash": config_hash(config),
            "order_basis": "remaining candidate count × ROI area ÷ output pixel area; estimate only",
            "region_order": ordered_regions(config, jobs), "monthly_tasks": len(jobs),
            "counts_frozen": inventory is not None, "collections": optical.CATALOG,
            "output_root": config["output_root"], "s2_resolution": config.get("s2_resolution", 30)}, ensure_ascii=False, indent=2))
        return 0
    except optical.TransientDownloadError as error:
        print(str(error), file=sys.stderr)
        return 75
    except (ValueError, KeyError, OSError, optical.DownloadError) as error:
        print(f"归档配置/运行失败：{error}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"归档失败：{type(error).__name__}（原始异常可能含临时URL，未输出）", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
