"""Read-only monthly scene throughput and ETA; never starts a download."""
import argparse
from collections import deque
from datetime import datetime, timedelta
import json
from pathlib import Path
import time
import archive


def snapshot(config, jobs):
    result = {region["code"]: {"total": 0, "done": 0, "months": 0, "finished": 0, "tiles": 0, "bytes": 0} for region in config["regions"]}
    for job in jobs:
        row = result[job["region"]]
        row["total"] += job["scene_count"]
        row["months"] += 1
        directory = Path(job["out"])
        if job["scene_count"] == 0:
            row["finished"] += 1
            continue
        try:
            summary = json.loads((directory / "download_selection_summary.json").read_text())
            count = summary["processed_scenes"]
            if type(count) is int and 0 <= count <= job["scene_count"]:
                row["done"] += count
                row["finished"] += count == job["scene_count"]
                row["tiles"] += summary.get("stored_tiles", 0)
                row["bytes"] += summary.get("stored_bytes", 0)
        except (OSError, ValueError, KeyError):
            pass
    return result


def estimate(samples, now, done, total):
    samples.append((now, done))
    while len(samples) > 1 and now - samples[0][0] > 900:
        samples.popleft()
    elapsed, delta = now - samples[0][0], done - samples[0][1]
    if elapsed < 120 or delta < 3:
        return None, None
    speed = delta / elapsed
    return speed * 60, max(0, total-done) / speed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, default=Path("runs/inventory.json"))
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 5:
        parser.error("刷新间隔至少5秒")
    config = archive.read_config(args.config)
    inventory = archive.load_inventory(config, args.inventory)
    jobs = archive.tasks(config, inventory["counts"])
    samples, byte_samples = deque(), deque()
    try:
        while True:
            rows = snapshot(config, jobs)
            done, total = sum(r["done"] for r in rows.values()), sum(r["total"] for r in rows.values())
            now = time.monotonic()
            speed, eta = estimate(samples, now, done, total)
            stored = sum(row["bytes"] for row in rows.values())
            byte_samples.append((now, stored))
            while len(byte_samples) > 1 and now - byte_samples[0][0] > 900:
                byte_samples.popleft()
            elapsed = now-byte_samples[0][0]
            mbps = max(0, stored-byte_samples[0][1])/elapsed/1024**2 if elapsed > 0 else None
            print("\n" + datetime.now().isoformat(timespec="seconds") + " 原生SR归档", flush=True)
            for code, row in rows.items():
                marker = Path(config["output_root"]) / code / "region_complete.json"
                validated = False
                try:
                    record = json.loads(marker.read_text())
                    validated = record.get("config_hash") == archive.config_hash(config) and record.get("status") == "all_months_validated"
                except (OSError, ValueError):
                    pass
                print(f"{code}: 候选{row['done']}/{row['total']}；月份{row['finished']}/{row['months']}；" + ("已完成校验，可进行后续处理" if validated else "等待/处理中"))
            print(f"候选处理 {done}/{total}；速度 " + (f"{speed:.2f}景/分钟" if speed else "采样中/暂无新增候选"))
            print("已完成景中的文件增长速度 " + (f"{mbps:.2f} MiB/s" if mbps is not None else "采样中") + "（不是网卡瞬时带宽）")
            if eta is not None:
                print(f"全档案滚动估算：{eta/3600:.1f}小时；预计完成{(datetime.now()+timedelta(seconds=eta)).isoformat(timespec='minutes')}（跨区域/传感器时可能变化）")
            else:
                print("剩余时间：需至少120秒与3个新增候选样本")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
