#!/usr/bin/env python3
"""Download native Landsat 8/9 or Sentinel-2 SR clear-land OPTRAM inputs.

No OAuth is started by this program. Authenticate Earth Engine beforehand.
Dates/paths are supplied by the user; the Cloud project can be read from local config.
"""
from __future__ import annotations

import argparse
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

VERSION = "optical-native-1.0.0"
RESOLUTION = 30
NODATA = -9999.0
CATALOG = {
    "L8": ("LANDSAT/LC08/C02/T1_L2", ("SR_B4", "SR_B5", "SR_B7")),
    "L9": ("LANDSAT/LC09/C02/T1_L2", ("SR_B4", "SR_B5", "SR_B7")),
    "S2": ("COPERNICUS/S2_SR_HARMONIZED", ("B4", "B8A", "B12")),
}
SENSORS = dict(CATALOG)
ACTIVE_SENSOR = "L8"
MAX_AOT = None
MAX_TRANSFER_BPS = 0.0
BANDS = ("red", "nir", "swir2", "NDVI", "STR", "QA_PIXEL", "SR_QA_AEROSOL", "QA_RADSAT")


def configure(sensor, resolution=30, max_aot=None):
    """One sensor per process: signing transport and raster schema are process-local."""
    global ACTIVE_SENSOR, SENSORS, BANDS, RESOLUTION, MAX_AOT
    if sensor not in CATALOG or resolution not in (10, 20, 30) or (sensor != "S2" and resolution != 30):
        raise DownloadError("Landsat仅支持30米；Sentinel-2支持10、20或30米输出网格。")
    if max_aot is not None and (not math.isfinite(max_aot) or max_aot < 0 or sensor != "S2"):
        raise DownloadError("--s2-max-aot 只适用于S2，必须是非负有限数。")
    ACTIVE_SENSOR, RESOLUTION, MAX_AOT = sensor, resolution, max_aot
    SENSORS = {sensor: CATALOG[sensor]}
    quality = ("SCL", "AOT", "reserved_zero") if sensor == "S2" else ("QA_PIXEL", "SR_QA_AEROSOL", "QA_RADSAT")
    BANDS = ("red", "nir", "swir2", "NDVI", "STR", *quality)


def quality_recipe(sensor, aerosol):
    if sensor == "S2":
        return {"SCL_keep": [4, 5], "AOT_scale": 0.001, "max_aot": MAX_AOT,
                "reflectance_scale": 0.0001, "reflectance_offset": 0,
                "nir": "B8A", "cross_sensor_harmonization": False}
    return {"QA_PIXEL_reject_bits": [0, 1, 2, 3, 4, 5, 7],
            "QA_RADSAT_reject_bits": [3, 4, 6, 11],
            "SR_QA_AEROSOL_reject_bits": [0, 2], "aerosol": aerosol,
            "reflectance_scale": 0.0000275, "reflectance_offset": -0.2,
            "cross_sensor_harmonization": False}


class DownloadError(RuntimeError):
    pass


class TransientDownloadError(DownloadError):
    """A bounded transport retry was exhausted; a controller may resume later."""


class DownloadCancelled(DownloadError):
    """A peer failed or the owner cancelled; no further work may be submitted."""


TRANSFER_RETRY_DELAYS = (5, 15, 30, 60, 120)


def check_download_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("当前景下载已取消，已校验文件保留。")


def pace_transfer(transferred, started, cancel_event=None):
    """Optional per-worker bandwidth cap; never changes exported pixels."""
    if MAX_TRANSFER_BPS <= 0:
        return
    delay = transferred / MAX_TRANSFER_BPS - (time.monotonic() - started)
    if delay > 0:
        if cancel_event is None:
            time.sleep(delay)
        elif cancel_event.wait(delay):
            raise DownloadCancelled("限速等待期间当前景已取消，已校验文件保留。")


def get_download_url(image, parameters, url_lock=None, cancel_event=None):
    """Serialize the entire EE call, including serialization/token refresh/HTTP.

    EE's initialized transport is process-global. No other EE calls or state
    changes are made by the owner until this scene's workers have drained.
    """
    check_download_cancelled(cancel_event)
    if url_lock is None:
        return image.getDownloadURL(parameters)
    while not url_lock.acquire(timeout=0.2):
        check_download_cancelled(cancel_event)
    try:
        check_download_cancelled(cancel_event)
        url = image.getDownloadURL(parameters)
        check_download_cancelled(cancel_event)
        return url
    finally:
        url_lock.release()


def is_transient_error(error):
    """Classify transport errors without reading/logging credential-bearing text.

    EE wraps HttpError in EEException; walk a bounded exception chain to retain
    its numeric HTTP status. Ordinary filesystem OSError is deliberately absent.
    """
    import requests
    import httplib2
    import ssl
    from google.auth.exceptions import DefaultCredentialsError, RefreshError, TransportError
    from googleapiclient.errors import HttpError
    if isinstance(error, TransientDownloadError):
        return True
    if isinstance(error, DownloadError):
        return False
    network_types = (requests.exceptions.ConnectionError,
                     requests.exceptions.Timeout,
                     requests.exceptions.ChunkedEncodingError,
                     requests.exceptions.ContentDecodingError,
                     httplib2.socks.ProxyError,
                     httplib2.ServerNotFoundError,
                     ssl.SSLEOFError,
                     TransportError)
    # A failed flush/close/unlink during a network exception has that exception
    # as __context__. The top-level local I/O failure must remain terminal.
    if isinstance(error, OSError) and not isinstance(error, network_types + (requests.exceptions.HTTPError,)):
        return False
    pending, seen = [error], set()
    transient = False
    while pending and len(seen) < 12:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        # Credential refresh uses a Google-auth transport wrapper. Retry its
        # HTTP transport failures, but never hide a rejected/expired grant or
        # other credential error behind a preceding network exception.
        if isinstance(current, (RefreshError, DefaultCredentialsError, ssl.SSLCertVerificationError)):
            return False
        status = None
        if isinstance(current, requests.exceptions.HTTPError):
            response = current.response
            if response is not None:
                status = response.status_code
        elif isinstance(current, HttpError):
            status = getattr(current.resp, "status", None)
        if status is not None:
            if status not in (408, 425, 429, 500, 502, 503, 504):
                return False
            transient = True
        elif isinstance(current, network_types):
            transient = True
        for inner in (getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if inner is not None:
                pending.append(inner)
    return transient


def is_opaque_ee_error(error):
    """Classify EE failures that hide the underlying request status.

    Explicit authorization/HTTP failures still use the normal fatal path.
    Never inspect or log exception messages, which can contain signed URLs.
    """
    from ee.ee_exception import EEException
    import requests
    from google.auth.exceptions import DefaultCredentialsError, RefreshError
    from googleapiclient.errors import HttpError

    if not isinstance(error, EEException):
        return False
    pending, seen = [error], set()
    while pending and len(seen) < 12:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (RefreshError, DefaultCredentialsError, HttpError)):
            return False
        if isinstance(current, requests.exceptions.HTTPError) and current.response is not None:
            return False
        for inner in (getattr(current, "__cause__", None), getattr(current, "__context__", None)):
            if inner is not None:
                pending.append(inner)
    return True


@dataclass(frozen=True)
class Tile:
    row: int
    col: int
    left: float
    top: float
    width: int
    height: int

    @property
    def name(self):
        return f"r{self.row:06d}_c{self.col:06d}.tif"

    @property
    def transform(self):
        return [RESOLUTION, 0, self.left, 0, -RESOLUTION, self.top]

    @property
    def bounds(self):
        return (self.left, self.top - self.height * RESOLUTION,
                self.left + self.width * RESOLUTION, self.top)


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hash_object(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_project(explicit=None, config_path=None):
    """Read project IDs only; never inspect credentials or authenticate here."""
    for value, source in [(explicit, "--project"),
            *[(os.environ.get(key), f"环境变量 {key}") for key in
              ("EE_PROJECT", "EASYGEE_PROJECT", "EARTHENGINE_PROJECT")]]:
        if isinstance(value, str) and value.strip():
            return value.strip(), source
    local_config = Path(config_path) if config_path else Path(__file__).resolve().with_name("optical_config.local.json")
    def read_config(path, source):
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise DownloadError(f"项目配置无法读取：{path}，请检查 JSON 格式。") from None
        if not isinstance(payload, dict):
            raise DownloadError(f"项目配置需为 JSON 对象：{path}")
        project = payload.get("earthEngineProject") or payload.get("project")
        if isinstance(project, str) and project.strip():
            return project.strip(), source
        return None
    resolved = read_config(local_config, "optical_config.local.json")
    if resolved:
        return resolved
    if os.environ.get("EASYGEE_SETTINGS"):
        settings = Path(os.environ["EASYGEE_SETTINGS"]).expanduser()
    else:
        if os.environ.get("EASYGEE_USER_CONFIG_DIR"):
            settings_root = Path(os.environ["EASYGEE_USER_CONFIG_DIR"]).expanduser()
        elif os.name == "nt":
            settings_root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "EasyGEE"
        else:
            settings_root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "easygee"
        settings = settings_root / "settings.json"
    return read_config(settings, "EasyGEE 用户设置") or (None, None)


class MountGuard:
    """Fail before writing when an explicitly required Linux mount is absent."""

    def __init__(self, output, mount=None, source=None):
        self.output = Path(output).expanduser().resolve()
        self.mount = Path(mount).expanduser().resolve() if mount else None
        self.source = source
        if self.mount is None and sys.platform.startswith("linux"):
            if self.output.is_relative_to(Path("/nasdata")):
                self.mount = Path("/nasdata")
        if source and self.mount is None:
            raise DownloadError("--mount-source 必须配合 --require-mount 使用。")
        if self.mount and not self.output.is_relative_to(self.mount):
            raise DownloadError("输出目录必须在 --require-mount 指定的挂载目录内。")
        self.min_free_gib = 0.0
        if self.mount is not None:
            try:
                self.min_free_gib = float(os.environ.get("OPTICAL_MIN_FREE_GIB", "0"))
            except ValueError:
                raise DownloadError("OPTICAL_MIN_FREE_GIB 必须是有限的非负 GiB 数值。") from None
            if not math.isfinite(self.min_free_gib) or self.min_free_gib < 0:
                raise DownloadError("OPTICAL_MIN_FREE_GIB 必须是有限的非负 GiB 数值。")

    def check(self):
        if self.mount is None:
            return
        if not sys.platform.startswith("linux"):
            raise DownloadError("--require-mount 使用 Linux /proc/self/mountinfo；Windows 请去掉该参数。")
        def decode(value):
            return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)
        records = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            before, after = line.split(" - ", 1)
            fields, extra = before.split(), after.split()
            if decode(fields[4]) == str(self.mount):
                records.append((decode(extra[1]), fields[5].split(","), fields[0]))
        if not records:
            raise DownloadError(f"NAS 未挂载到 {self.mount}；已停止，避免写入本地磁盘。")
        actual_source, options, mount_id = records[-1]
        if "ro" in options:
            raise DownloadError(f"{self.mount} 当前为只读挂载。")
        if self.source and actual_source != self.source:
            raise DownloadError("NAS 挂载来源与 --mount-source 不一致。")
        if not self.output.resolve().is_relative_to(self.mount):
            raise DownloadError("输出路径经符号链接解析后已离开 NAS。")
        return mount_id

    @contextmanager
    def writable_parent(self, path):
        """Pin a verified Linux mount during writes; never follow output symlinks."""
        path = Path(path).absolute()
        if not path.is_relative_to(self.output):
            raise DownloadError("写入目标不在指定输出目录内。")
        mount_id = self.check()
        if self.mount is None:
            for item in (path, *path.parents):
                if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
                    raise DownloadError("输出路径含符号链接/目录联接，请使用普通专用目录。")
            path.parent.mkdir(parents=True, exist_ok=True)
            yield None, path.name
            return
        descriptors = []
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            descriptor = os.open(self.mount, flags)
            descriptors.append(descriptor)
            def verify_descriptor(fd):
                info = Path(f"/proc/self/fdinfo/{fd}").read_text()
                actual = re.search(r"^mnt_id:\s*(\d+)$", info, re.MULTILINE)
                if not actual or actual.group(1) != mount_id:
                    raise DownloadError("NAS 挂载在检查期间发生变化/出现其他子挂载，已停止写入。")
            verify_descriptor(descriptor)
            # Query the pinned NAS itself before creating even output directories.
            # f_bavail excludes blocks unavailable to this user; a pathname query
            # could accidentally inspect local storage after the mount disappears.
            if self.min_free_gib > 0:
                try:
                    space = os.fstatvfs(descriptor)
                except OSError:
                    raise DownloadError("无法检查 NAS 剩余空间，已停止创建输出。") from None
                available_gib = space.f_bavail * space.f_frsize / (1024 ** 3)
                if available_gib < self.min_free_gib:
                    raise DownloadError(
                        f"NAS 剩余空间 {available_gib:.2f} GiB，低于保留阈值 "
                        f"{self.min_free_gib:g} GiB；已停止，未创建新的输出目录或文件。")
            relative = path.relative_to(self.mount)
            for component in relative.parts[:-1]:
                try:
                    os.mkdir(component, dir_fd=descriptor)
                except FileExistsError:
                    pass
                descriptor = os.open(component, flags, dir_fd=descriptor)
                descriptors.append(descriptor)
                verify_descriptor(descriptor)
            # All subsequent I/O uses this pinned parent, including rasterio.
            try:
                info = os.stat(relative.name, dir_fd=descriptor, follow_symlinks=False)
                import stat
                if not stat.S_ISREG(info.st_mode):
                    raise DownloadError("输出文件已存在且不是普通文件。")
            except FileNotFoundError:
                pass
            yield descriptor, relative.name
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def temporary_file(self, path, descriptor):
        """Create a partial file without following an existing symlink."""
        name = path.name + ".part"
        if descriptor is None:
            temporary = path.with_name(name)
            if temporary.is_symlink():
                raise DownloadError("临时输出文件是符号链接，已停止。")
            return temporary.open("wb"), temporary
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                     0o600, dir_fd=descriptor)
        return os.fdopen(fd, "wb"), Path(f"/proc/self/fd/{descriptor}/{name}")

    def finalize(self, path, temporary, descriptor):
        self.check()
        if descriptor is None:
            os.replace(temporary, path)
        else:
            os.replace(path.name + ".part", path.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)


def write_json(path, value, guard):
    path = Path(path)
    with guard.writable_parent(path) as (descriptor, _):
        stream, temporary = guard.temporary_file(path, descriptor)
        with stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))
        guard.finalize(path, temporary, descriptor)


def load_roi(path, target_crs=None, buffer_m=0, repair=False):
    """Read geometry only; do not write/reproject beside the original Shapefile."""
    import geopandas as gpd
    from pyproj import CRS
    from shapely.geometry import mapping
    from shapely import make_valid, union_all

    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise DownloadError(f"ROI 文件不存在：{path}")
    if path.suffix.lower() == ".shp":
        names = {item.name.lower() for item in path.parent.iterdir()}
        missing = [extension for extension in (".shx", ".dbf", ".prj")
                   if (path.stem + extension).lower() not in names]
        if missing:
            raise DownloadError(f"Shapefile 缺少配套文件：{', '.join(missing)}；请放在同一目录。")
    elif path.suffix.lower() != ".zip":
        raise DownloadError("--roi 需要 .shp 或包含完整 Shapefile 的 .zip。")
    frame = gpd.read_file(str(path))
    if frame.crs is None:
        raise DownloadError("ROI 没有 CRS。请先在 GIS 中正确设置/补齐 .prj，不能猜测坐标系。")
    frame = frame.loc[frame.geometry.notna() & ~frame.geometry.is_empty, ["geometry"]].copy()
    if frame.empty:
        raise DownloadError("ROI 没有有效几何。")
    if not frame.geometry.is_valid.all():
        if not repair:
            raise DownloadError("ROI 含无效几何。请修复，或明确使用 --repair-geometries。")
        frame.geometry = frame.geometry.map(make_valid)
    geographic = frame.to_crs("EPSG:4326")
    xmin, ymin, xmax, ymax = geographic.total_bounds
    if not all(math.isfinite(v) for v in (xmin, ymin, xmax, ymax)):
        raise DownloadError("ROI 坐标范围无效。")
    if xmin < -180 or xmax > 180 or ymin < -90 or ymax > 90 or xmax - xmin > 180:
        raise DownloadError("ROI 超出经纬度范围或跨越日期变更线，请先分区处理。")
    if target_crs is None:
        if xmax - xmin > 6 or ymin < -80 or ymax > 84:
            raise DownloadError("ROI 范围过宽或位于极区，请用 --crs 指定适用的米制投影。")
        target_crs = geographic.estimate_utm_crs()
        if target_crs is None:
            raise DownloadError("无法估计 UTM，请用 --crs 指定米制投影。")
    crs = CRS.from_user_input(target_crs)
    if not crs.is_projected or not crs.axis_info or any(
            abs(axis.unit_conversion_factor - 1) > 1e-10 for axis in crs.axis_info[:2]):
        raise DownloadError("--crs 必须是单位为米的投影坐标系，不能用 EPSG:4326。")
    projected = frame.to_crs(crs)
    if buffer_m < 0:
        raise DownloadError("--buffer-m 不能为负。")
    if buffer_m:
        projected.geometry = projected.geometry.buffer(buffer_m)
    if not projected.geom_type.isin(["Polygon", "MultiPolygon"]).all():
        raise DownloadError("ROI 必须是面。点/线研究区请用 --buffer-m 明确给出缓冲距离（米）。")
    roi = union_all(projected.geometry.to_numpy()).normalize()
    if roi.is_empty or not roi.is_valid:
        raise DownloadError("处理后的 ROI 为空或无效。")
    ee_frame = gpd.GeoDataFrame(geometry=[roi], crs=crs).to_crs("EPSG:4326")
    payload = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": mapping(ee_frame.geometry.iloc[0])}]}
    if len(canonical_json(payload).encode("utf-8")) > 8 * 1024 * 1024:
        raise DownloadError("ROI 几何超过 8 MiB；请先合理简化/分区，或改用预先上传的 EE 资产。")
    return roi, crs, payload


def make_tiles(roi, edge):
    from shapely.geometry import box
    if not 64 <= edge <= 768:
        raise DownloadError("--tile-size 需为64–768。八个float32波段每块保持在下载限制以内。")
    xmin, ymin, xmax, ymax = roi.bounds
    left = math.floor(xmin / RESOLUTION) * RESOLUTION
    top = math.ceil(ymax / RESOLUTION) * RESOLUTION
    width = math.ceil(xmax / RESOLUTION) - math.floor(xmin / RESOLUTION)
    height = math.ceil(ymax / RESOLUTION) - math.floor(ymin / RESOLUTION)
    tile_count = math.ceil(width / edge) * math.ceil(height / edge)
    if tile_count > 200000:
        raise DownloadError("ROI 包围盒会产生超过 20 万个块，请先分区处理。")
    tiles = []
    for row in range(0, height, edge):
        for col in range(0, width, edge):
            tile = Tile(row, col, left + col * RESOLUTION, top - row * RESOLUTION,
                        min(edge, width - col), min(edge, height - row))
            if roi.intersection(box(*tile.bounds)).area > 0:
                tiles.append(tile)
    if not tiles:
        raise DownloadError("ROI 未产生任何下载块。")
    return tiles


def strict_clear_land(qa_pixel, aerosol="low", aerosol_qa=None, saturation=None, sensor=None):
    """Numpy counterpart of the server-side mask, used for offline validation."""
    import numpy as np
    sensor = sensor or ACTIVE_SENSOR
    if sensor == "S2":
        ok = np.isin(qa_pixel, [4, 5])
        if MAX_AOT is not None:
            ok &= np.asarray(aerosol_qa) <= MAX_AOT
        return ok
    qa = np.asarray(qa_pixel, dtype=np.uint16)
    aq = np.asarray(aerosol_qa, dtype=np.uint16)
    sat = np.asarray(saturation, dtype=np.uint16)
    level = (aq >> 6) & 3
    acceptable = (level == 1) if aerosol == "low" else (level <= 1)
    return ((qa & 191) == 0) & ((aq & 5) == 0) & acceptable & ((sat & 2136) == 0)


def parse_proxy(url):
    if not url or url.lower() == "none":
        return None
    parsed = urlsplit(url)
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username or
            parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
        raise DownloadError("--proxy 使用无账号的 HTTP CONNECT 代理，如 http://127.0.0.1:8118；不能填 SOCKS。")
    try:
        port = parsed.port or 80
    except ValueError as error:
        raise DownloadError("代理端口无效。") from error
    return parsed.hostname, port


def initialize_ee(project, proxy_url):
    import ee
    import httplib2
    proxy = parse_proxy(proxy_url)
    transport = httplib2.Http(timeout=180, proxy_info=(
        httplib2.ProxyInfo(httplib2.socks.PROXY_TYPE_HTTP, proxy[0], proxy[1])
        if proxy else None))
    # Persistent-credential validation refreshes a token with requests BEFORE
    # Initialize attaches our httplib2 transport. Cover that refresh explicitly.
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        if proxy:
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                os.environ[key] = proxy_url
            os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1,::1"
        ee.Initialize(project=project, http_transport=transport)
    except Exception as error:
        if is_transient_error(error):
            raise TransientDownloadError("Earth Engine 初始化时网络/代理暂不可用；等待网络恢复后可续跑。") from None
        raise DownloadError("Earth Engine 初始化失败：检查预先认证、Cloud 项目权限及 HTTP 代理。"
                            "本脚本不会自动启动 OAuth。") from None
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return ee


def build_image(ee, source, sensor, roi_fc, factor, aerosol):
    scale, offset = (0.0001, 0) if sensor == "S2" else (0.0000275, -0.2)
    reflectance = source.select(list(CATALOG[sensor][1]), ["red", "nir", "swir2"]).multiply(scale).add(offset)
    red, nir, swir = (reflectance.select(name) for name in ("red", "nir", "swir2"))
    if sensor == "S2":
        qa = source.select("SCL")
        aq = source.select("AOT").multiply(0.001)
        sat = qa.multiply(0).rename("reserved_zero")
        quality_ok = qa.eq(4).Or(qa.eq(5))
        if MAX_AOT is not None:
            quality_ok = quality_ok.And(aq.lte(MAX_AOT))
    else:
        qa, aq, sat = (source.select(name) for name in ("QA_PIXEL", "SR_QA_AEROSOL", "QA_RADSAT"))
        aerosol_level = aq.rightShift(6).bitwiseAnd(3)
        aerosol_ok = aerosol_level.eq(1) if aerosol == "low" else aerosol_level.lte(1)
        quality_ok = qa.bitwiseAnd(191).eq(0).And(aq.bitwiseAnd(5).eq(0)).And(aerosol_ok).And(sat.bitwiseAnd(2136).eq(0))
    mask = (reflectance.mask().reduce(ee.Reducer.min()).gt(0)
            .And(qa.mask().gt(0)).And(aq.mask().gt(0)).And(sat.mask().gt(0)).And(quality_ok)
            .And(swir.gt(0)).And(nir.add(red).abs().gt(1e-6)))
    # Calculate only from usable denominators; no implicit NDVI negative-value mask.
    red, nir, swir = (band.updateMask(mask) for band in (red, nir, swir))
    ndvi = nir.subtract(red).divide(nir.add(red)).rename("NDVI")
    str_image = swir.multiply(-1).add(1).pow(2).divide(swir.multiply(2)).rename("STR")
    result = reflectance.addBands([ndvi, str_image, qa.rename(BANDS[5]), aq.rename(BANDS[6]), sat.rename(BANDS[7])]).updateMask(mask)
    return (result.select(list(BANDS)).clipToCollection(roi_fc).toFloat()
            .unmask(NODATA, sameFootprint=False))


def inspect_tile(path, tile, crs, annotate=False, tags=None):
    """Read every small tile fully, validate its grid and catch a gross scale error."""
    import numpy as np
    import rasterio
    from rasterio.transform import Affine
    from rasterio.crs import CRS
    with rasterio.open(path, "r+" if annotate else "r") as dataset:
        expected = Affine(*tile.transform)
        if (dataset.count != len(BANDS) or dataset.width != tile.width or
                dataset.height != tile.height or dataset.crs != CRS.from_user_input(crs) or
                not dataset.transform.almost_equals(expected, precision=1e-6) or
                any(dtype != "float32" for dtype in dataset.dtypes)):
            raise DownloadError("下载块的波段数、类型或网格与请求不一致。")
        values = dataset.read()
        if not np.isfinite(values).all():
            raise DownloadError("下载块含 NaN/Inf，请检查反射率与 STR 分母。")
        missing = values == NODATA
        if np.any(np.any(missing, axis=0) != np.all(missing, axis=0)):
            raise DownloadError("下载块各波段 NoData 掩膜不一致。")
        valid = ~np.any(missing, axis=0)
        count = int(valid.sum())
        if count:
            p95 = np.percentile(values[:3, valid], 95, axis=1)
            if np.any(p95 > 2):
                raise DownloadError("反射率第95百分位大于2，疑似比例因子不正确。"
                                    "本程序固定使用官方地表反射率比例与偏移，请核实数据集。")
            qa_values = values[5, valid]
            aq, sat = values[6, valid], values[7, valid]
            qa_max = 11 if ACTIVE_SENSOR == "S2" else 65535
            if np.any((qa_values < 0) | (qa_values > qa_max) | (qa_values != np.floor(qa_values))):
                raise DownloadError("原生QA/SCL不是合法整数，疑似经过不合适的重采样。")
            if ACTIVE_SENSOR == "S2":
                if np.any((aq < 0) | (aq > 65.5351) | (sat != 0)):
                    raise DownloadError("S2 AOT或保留波段值无效。")
            elif np.any((aq < 0) | (aq > 255) | (aq != np.floor(aq)) | (sat < 0) | (sat > 65535) | (sat != np.floor(sat))):
                raise DownloadError("Landsat气溶胶/饱和度QA值无效。")
            if not np.all(strict_clear_land(qa_values, (tags or {}).get("aerosol", "low"), aq, sat)):
                raise DownloadError("下载块包含不符合本传感器晴空陆地条件的像元。")
            red, nir, swir = values[:3, valid]
            if np.any(swir <= 0) or np.any(np.abs(nir + red) <= 1e-6):
                raise DownloadError("OPTRAM指数分母无效。")
            if not np.allclose(values[3, valid], (nir-red)/(nir+red), rtol=2e-5, atol=2e-6) or not np.allclose(values[4, valid], (1-swir)**2/(2*swir), rtol=2e-5, atol=2e-6):
                raise DownloadError("NDVI/STR与反射率不一致。")
        if annotate:
            dataset.nodata = NODATA
            for index, name in enumerate(BANDS, 1):
                dataset.set_band_description(index, name)
            if tags:
                dataset.update_tags(**{key: str(value) for key, value in tags.items()})
        elif dataset.nodata != NODATA or tuple(dataset.descriptions) != BANDS:
            raise DownloadError("已有块缺少预期的 NoData/波段描述。")
    return {"valid_pixels": count, "reflectance_p95": p95.tolist() if count else None}


def cached_tile(path, tile, crs, recipe_hash, aerosol, scene_id, *, selection_hash=None):
    record_path = path.with_suffix(".json")
    if not path.is_file() or not record_path.is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        if (record.get("recipe_hash") != recipe_hash or record.get("scene_id") != scene_id or
                record.get("sha256") != file_hash(path)):
            return None
        if selection_hash is not None and record.get("selection_hash") != selection_hash:
            return None
        import rasterio
        with rasterio.open(path) as dataset:
            tags = dataset.tags()
            if tags.get("recipe_hash") != recipe_hash or tags.get("scene_id") != scene_id:
                return None
            if selection_hash is not None and tags.get("selection_hash") != selection_hash:
                return None
        stats = inspect_tile(path, tile, crs, tags={"aerosol": aerosol})
        return stats
    except Exception:
        return None


def compress_tile_losslessly(temporary, tile, crs, guard, descriptor, aerosol):
    """Re-encode one annotated tile in its pinned parent, then verify before replacing.

    Storage encoding is deliberately outside the scientific recipe: existing
    verified, uncompressed tiles remain valid resume candidates.
    """
    import numpy as np
    import rasterio
    from rasterio.shutil import copy as copy_raster

    def metadata(dataset):
        # IMAGE_STRUCTURE describes encoding and necessarily changes here.
        # Other dataset/band tag domains belong to the source and must survive.
        def tags(index=0):
            domains = (None, *dataset.tag_namespaces(index))
            return {domain: dataset.tags(index, ns=domain) for domain in domains
                    if domain not in ("IMAGE_STRUCTURE", "DERIVED_SUBDATASETS")}
        return {"width": dataset.width, "height": dataset.height,
                "count": dataset.count, "dtypes": dataset.dtypes,
                "crs": dataset.crs, "transform": dataset.transform,
                "nodata": dataset.nodatavals, "descriptions": dataset.descriptions,
                "scales": dataset.scales, "offsets": dataset.offsets,
                "units": dataset.units, "colorinterp": dataset.colorinterp,
                "dataset_tags": tags(),
                "band_tags": tuple(tags(index) for index in dataset.indexes)}

    guard.check()
    # Use the already pinned NAS directory, never a system temp directory or a
    # newly resolved NAS path. A unique name also avoids stale/parallel partials.
    target = temporary.with_name(temporary.name + ".deflate-" + uuid.uuid4().hex)
    stream, compressed = guard.temporary_file(target, descriptor)
    stream.close()
    try:
        # No quantization/DISCARD_LSB, sparse TIFFs, or implicit worker pool.
        with rasterio.Env(GDAL_NUM_THREADS="1", GDAL_PAM_ENABLED="NO"):
            with rasterio.open(temporary) as source:
                original_metadata = metadata(source)
                original_values = source.read()
                original_masks = source.read_masks()
                copy_raster(source, compressed, driver="GTiff", strict=True,
                            COMPRESS="DEFLATE", PREDICTOR=3, ZLEVEL=6,
                            NUM_THREADS="1", TILED="YES", BLOCKXSIZE=256,
                            BLOCKYSIZE=256, INTERLEAVE="BAND")
                # CreateCopy does not promise arbitrary custom tag domains.
                # Explicitly preserve them inside the TIFF, without sidecars.
                with rasterio.open(compressed, "r+") as destination:
                    for domain, tags in original_metadata["dataset_tags"].items():
                        if tags:
                            destination.update_tags(ns=domain, **tags)
                    for index, domains in enumerate(original_metadata["band_tags"], 1):
                        for domain, tags in domains.items():
                            if tags:
                                destination.update_tags(index, ns=domain, **tags)
            guard.check()
            with rasterio.open(compressed) as destination:
                if (metadata(destination) != original_metadata or
                        destination.compression != rasterio.enums.Compression.deflate or
                        destination.tags(ns="IMAGE_STRUCTURE").get("PREDICTOR") != "3"):
                    raise DownloadError("无损压缩后的网格、波段元数据或压缩参数不一致。")
                # Compare IEEE-754 bits as well as masks, including signed zero.
                if (not np.array_equal(destination.read().view(np.uint32),
                                       original_values.view(np.uint32)) or
                        not np.array_equal(destination.read_masks(), original_masks)):
                    raise DownloadError("无损压缩后的像元值或掩膜不一致，未提交该块。")
            inspect_tile(compressed, tile, crs, tags={"aerosol": aerosol})
        guard.check()
        if descriptor is None:
            os.replace(compressed, temporary)
        else:
            os.replace(compressed.name, temporary.name,
                       src_dir_fd=descriptor, dst_dir_fd=descriptor)
        return {"encoding": "DEFLATE", "predictor": 3, "level": 6,
                "threads": 1, "stored_bytes": temporary.stat().st_size}
    finally:
        # A failed copy/validation never replaces the input; clean only our
        # unique compressed partial, through the same pinned parent on Linux.
        try:
            if descriptor is None:
                compressed.unlink()
            else:
                os.unlink(compressed.name, dir_fd=descriptor)
        except FileNotFoundError:
            pass


def download_tile(image, tile, path, crs, session, guard, recipe_hash, aerosol, scene_id, *,
                  url_lock=None, cancel_event=None, selection_hash=None):
    check_download_cancelled(cancel_event)
    guard.check()
    if path.is_symlink() or path.with_suffix(".json").is_symlink():
        raise DownloadError("输出块或记录是符号链接，已停止。")
    existing = cached_tile(path, tile, crs, recipe_hash, aerosol, scene_id,
                           selection_hash=selection_hash)
    if existing is not None:
        check_download_cancelled(cancel_event)
        guard.check()
        return existing, True
    if selection_hash is not None and (path.exists() or path.with_suffix(".json").exists()):
        raise DownloadError("已有优化块未通过校验，停止而不覆盖或重新下载。")
    guard.check()
    parameters = {"crs": crs, "crs_transform": tile.transform,
                  "dimensions": [tile.width, tile.height], "format": "GEO_TIFF"}
    attempts = len(TRANSFER_RETRY_DELAYS) + 1
    opaque_ee_retry_used = False
    for attempt in range(attempts):
        check_download_cancelled(cancel_event)
        remote_phase = True
        try:
            # Refresh expiring signed URLs on retries. Never persist or log those URLs.
            url = get_download_url(image, parameters, url_lock, cancel_event)
            with session.get(url, stream=True, timeout=(30, 180)) as response:
                response.raise_for_status()
                remote_phase = False
                check_download_cancelled(cancel_event)
                with guard.writable_parent(path) as (descriptor, _):
                    check_download_cancelled(cancel_event)
                    stream, temporary = guard.temporary_file(path, descriptor)
                    committed = False
                    try:
                        with stream:
                            started, transferred = time.monotonic(), 0
                            chunks = iter(response.iter_content(65536 if MAX_TRANSFER_BPS > 0 else 1024 * 1024))
                            while True:
                                check_download_cancelled(cancel_event)
                                remote_phase = True
                                try:
                                    chunk = next(chunks)
                                except StopIteration:
                                    remote_phase = False
                                    break
                                remote_phase = False
                                check_download_cancelled(cancel_event)
                                if chunk:
                                    guard.check()
                                    stream.write(chunk)
                                    transferred += len(chunk)
                                    pace_transfer(transferred, started, cancel_event)
                        check_download_cancelled(cancel_event)
                        guard.check()
                        selection_tags = {"selection_hash": selection_hash} if selection_hash else {}
                        stats = inspect_tile(temporary, tile, crs, annotate=True,
                                             tags={"recipe_hash": recipe_hash, "aerosol": aerosol,
                                                   "scene_id": scene_id, "resampling": "nearest", **selection_tags})
                        check_download_cancelled(cancel_event)
                        storage = compress_tile_losslessly(temporary, tile, crs, guard,
                                                           descriptor, aerosol)
                        check_download_cancelled(cancel_event)
                        digest = file_hash(temporary)
                        check_download_cancelled(cancel_event)
                        guard.finalize(path, temporary, descriptor)
                        committed = True
                    finally:
                        if not committed:
                            try:
                                if descriptor is None:
                                    temporary.unlink()
                                else:
                                    os.unlink(temporary.name, dir_fd=descriptor)
                            except FileNotFoundError:
                                pass
            remote_phase = False
            check_download_cancelled(cancel_event)
            write_json(path.with_suffix(".json"), {"recipe_hash": recipe_hash, "sha256": digest,
                       "scene_id": scene_id,
                       **({"selection_hash": selection_hash} if selection_hash else {}),
                       "transform": tile.transform, "dimensions": [tile.width, tile.height],
                       "storage": storage,
                       **stats}, guard)
            return stats, False
        except DownloadError:
            raise
        except Exception as error:
            # Raw errors may contain credential-bearing signed URLs; print only the class.
            transient = remote_phase and is_transient_error(error)
            opaque_ee = remote_phase and not transient and is_opaque_ee_error(error)
            if opaque_ee and not opaque_ee_retry_used and attempt < attempts - 1:
                opaque_ee_retry_used = True
                delay = TRANSFER_RETRY_DELAYS[0]
                print(f"  Earth Engine 请求异常（{type(error).__name__}），等待 {delay} 秒后仅重试一次", flush=True)
                if cancel_event is None:
                    time.sleep(delay)
                elif cancel_event.wait(delay):
                    raise DownloadCancelled("网络等待期间当前景已取消，已校验文件保留。") from None
                continue
            if opaque_ee:
                raise TransientDownloadError("Earth Engine 请求异常（EEException），暂按临时网络故障等待恢复。"
                                             "已校验的块保留；若持续出现，请检查服务日志。") from None
            if not transient:
                raise DownloadError(f"请求或文件处理失败（{type(error).__name__}），已停止。"
                                    "已校验的块会保留；请检查日志、NAS及请求参数。") from None
            if attempt == attempts - 1:
                raise TransientDownloadError(f"块传输暂时失败（{type(error).__name__}），已尝试{attempts}次。"
                                             "网络恢复后可继续已完成块。") from None
            delay = TRANSFER_RETRY_DELAYS[attempt]
            print(f"  网络暂时中断（{type(error).__name__}），等待 {delay} 秒后重试 {attempt + 1}/{attempts-1}", flush=True)
            if cancel_event is None:
                time.sleep(delay)
            elif cancel_event.wait(delay):
                raise DownloadCancelled("网络等待期间当前景已取消，已校验文件保留。") from None


class TileDownloadPool:
    """Bounded scene-local work with persistent, separate HTTP sessions per thread."""

    def __init__(self, workers, proxy):
        if type(workers) is not int or not 1 <= workers <= 8:
            raise DownloadError("--workers 必须是1–8的整数。")
        parse_proxy(proxy)
        self.workers, self.proxy = workers, proxy
        self.local = threading.local()
        self.sessions, self.session_lock = [], threading.Lock()
        self.url_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self.executor = None

    def __enter__(self):
        if self.workers > 1:
            self.executor = ThreadPoolExecutor(max_workers=self.workers,
                                               thread_name_prefix="optical-tile")
        return self

    def session(self):
        check_download_cancelled(self.cancel_event)
        if not hasattr(self.local, "session"):
            import requests
            session = requests.Session()
            try:
                session.trust_env = False
                if parse_proxy(self.proxy):
                    session.proxies.update({"http": self.proxy, "https": self.proxy})
            except BaseException:
                session.close()
                raise
            with self.session_lock:
                self.sessions.append(session)
            self.local.session = session
        return self.local.session

    def __exit__(self, _kind, _error, _traceback):
        self.cancel_event.set()
        try:
            if self.executor is not None:
                self.executor.shutdown(wait=True, cancel_futures=True)
        finally:
            # All workers are drained before their persistent connections close.
            for session in self.sessions:
                session.close()

    def download_scene(self, image, tiles, scene_dir, crs, guard, recipe_hash,
                       aerosol, scene, emit=None, *, selection_hash=None):
        if emit is None:
            emit = lambda value: print(value, flush=True)
        check_download_cancelled(self.cancel_event)
        tiles = list(tiles)
        completed = [None] * len(tiles)
        count = 0
        selection_options = {"selection_hash": selection_hash} if selection_hash else {}

        def record(index, result):
            nonlocal count
            stats, skipped = result
            tile = tiles[index]
            completed[index] = {"file": tile.name, **stats}
            count += 1
            emit(f"  {count}/{len(tiles)} {tile.name} {'已校验，跳过' if skipped else '已下载'}；"
                 f"有效像元 {stats['valid_pixels']}")

        def contextual_error(index, error):
            prefix = f"{scene['sensor']}/{scene['index']}/{tiles[index].name}："
            if isinstance(error, TransientDownloadError):
                return TransientDownloadError(prefix + str(error))
            if isinstance(error, DownloadError):
                return DownloadError(prefix + str(error))
            # Unexpected errors may include signed URLs; keep only the class.
            return DownloadError(prefix + f"块处理失败（{type(error).__name__}）。")

        if self.workers == 1:
            # Preserve the original main-thread/session/retry behavior by default.
            for index, tile in enumerate(tiles):
                check_download_cancelled(self.cancel_event)
                try:
                    result = download_tile(image, tile, scene_dir / tile.name, crs,
                                           self.session(), guard, recipe_hash, aerosol, scene["id"],
                                           **selection_options)
                except DownloadError as error:
                    raise contextual_error(index, error) from None
                record(index, result)
            return completed

        def work(index):
            check_download_cancelled(self.cancel_event)
            tile = tiles[index]
            return download_tile(image, tile, scene_dir / tile.name, crs,
                                 self.session(), guard, recipe_hash, aerosol, scene["id"],
                                 url_lock=self.url_lock, cancel_event=self.cancel_event, **selection_options)

        pending, errors = {}, []
        next_index, owner_error = 0, None

        def collect(future):
            index = pending.pop(future)
            try:
                result = future.result()
            except (CancelledError, DownloadCancelled):
                pass
            except BaseException as error:
                errors.append((index, error))
                self.cancel_event.set()
            else:
                try:
                    record(index, result)
                except BaseException as error:
                    errors.append((index, error))
                    self.cancel_event.set()

        try:
            while next_index < len(tiles) or pending:
                check_download_cancelled(self.cancel_event)
                while next_index < len(tiles) and len(pending) < self.workers:
                    check_download_cancelled(self.cancel_event)
                    future = self.executor.submit(work, next_index)
                    pending[future] = next_index
                    next_index += 1
                done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                # Inspect the whole returned batch before submitting any next tile.
                for future in sorted(done, key=pending.get):
                    collect(future)
                if errors:
                    break
        except BaseException as error:
            owner_error = error
            self.cancel_event.set()

        if errors or owner_error is not None or self.cancel_event.is_set():
            self.cancel_event.set()
            for future in pending:
                future.cancel()
            # Running workers may still report a fatal NAS/QA failure. Collect all
            # of them; a first-observed transient failure must not hide that cause.
            while pending:
                try:
                    done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in sorted(done, key=pending.get):
                        collect(future)
                except KeyboardInterrupt as error:
                    owner_error = error
                    self.cancel_event.set()
            if isinstance(owner_error, (KeyboardInterrupt, SystemExit)):
                raise owner_error
            permanent = [(index, error) for index, error in errors
                         if not isinstance(error, TransientDownloadError)]
            if permanent or errors:
                index, error = (permanent or errors)[0]
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    raise error
                raise contextual_error(index, error) from None
            if owner_error is not None:
                raise owner_error
            raise DownloadCancelled("当前景已取消，未写入景完成记录。")
        return completed


def parser():
    result = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    result.add_argument("--sensor", required=True, choices=tuple(CATALOG))
    result.add_argument("--resolution", type=int, choices=(10, 20, 30), default=30)
    result.add_argument("--s2-max-aot", type=float, help="可选物理AOT上限；S2没有HLS的气溶胶分类，不默认强行等同")
    result.add_argument("--max-mib-s", type=float, default=0, help="每路块传输MiB/s上限，0为不限速；不改变科学配方")
    result.add_argument("--roi", required=True, help="执行此脚本的机器上的 .shp 或 .zip 路径")
    result.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD（包含）")
    result.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD（不包含）")
    result.add_argument("--project", help="可选，覆盖环境变量/同目录本地配置/EasyGEE设置中的项目 ID")
    result.add_argument("--out", required=True, help="本地/NAS 输出目录")
    result.add_argument("--proxy", default="http://127.0.0.1:8118", help="HTTP 代理 URL；直连用 none")
    result.add_argument("--require-mount", help="必须存在的 Linux 挂载点；/nasdata 输出会自动检查该点")
    result.add_argument("--mount-source", help="可选，要求挂载来源与此字符串完全一致")
    result.add_argument("--crs", help="米制目标投影，如 EPSG:32650；默认根据 ROI 估计 UTM")
    result.add_argument("--buffer-m", type=float, default=0, help="点/线转为面，或扩展面 ROI 的显式缓冲距离（米）")
    result.add_argument("--repair-geometries", action="store_true", help="明确允许 make_valid 修复 ROI 几何")
    result.add_argument("--aerosol", choices=("low", "climatology-low"), default="low",
                        help="low 仅保留低气溶胶；另一个选项额外保留气候值类别")
    result.add_argument("--tile-size", type=int, default=512, help="每个 GeoTIFF 的最大边长（像元），64–768")
    result.add_argument("--workers", type=int, choices=range(1, 9), default=1,
                        help="同时下载的块数，1–8；EE下载地址生成始终串行，不改变科学配方")
    result.add_argument("--max-scenes", type=int, default=1000, help="候选影像上限；超过后停止，请缩短日期")
    result.add_argument("--limit", type=int, help="只处理按时间排序的前 N 景；首次建议 1")
    result.add_argument("--dry-run", action="store_true", help="只在本地检查 ROI/网格，不认证、不联网、不写输出")
    result.add_argument("--list-only", action="store_true", help="只查询并打印 EE 景列表，不下载、不写输出")
    result.add_argument("--no-download-optimization", action="store_true",
                        help="仅供兼容诊断：关闭下载前晴空检查和同一观测像元去重")
    result.add_argument("--resume-complete-only", action="store_true",
                        help="仅离线核验已完成月任务；未完成返回3，不联网、不写入")
    return result


def run(args):
    global MAX_TRANSFER_BPS
    limit = getattr(args, "max_mib_s", 0)
    if not math.isfinite(limit) or limit < 0:
        raise DownloadError("--max-mib-s 必须是非负有限数。")
    MAX_TRANSFER_BPS = limit * 1024 * 1024
    configure(args.sensor, args.resolution, args.s2_max_aot)
    # Fixed catalog factors are applied by build_image; never user-overridden.
    args.reflectance_scale = 1.0
    workers = getattr(args, "workers", 1)
    if type(workers) is not int or not 1 <= workers <= 8:
        raise DownloadError("--workers 必须是1–8的整数。")
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if start >= end:
        raise DownloadError("开始日期必须早于结束日期。")
    if not math.isfinite(args.buffer_m):
        raise DownloadError("--buffer-m 必须为有限数。")
    if args.max_scenes < 1 or (args.limit is not None and args.limit < 1):
        raise DownloadError("--max-scenes 和 --limit 必须为正整数。")
    parse_proxy(args.proxy)
    guard = MountGuard(args.out, args.require_mount, args.mount_source)
    guard.check()
    # A negative cache probe needs no ROI/grid construction or proxy setup.
    # This is only a hint that work remains; positive reuse is fully validated below.
    if getattr(args, "resume_complete_only", False):
        manifest_hint = guard.output / "run_manifest.json"
        if not manifest_hint.is_file():
            return 3
        if manifest_hint.is_symlink():
            raise DownloadError("输出 manifest 是符号链接，已停止。")
        hint = json.loads(manifest_hint.read_text(encoding="utf-8"))
        items = hint.get("scenes")
        if not isinstance(items, list) or len(items) > args.max_scenes:
            raise DownloadError("已有景目录记录不合法，已停止。")
        for scene in items:
            sensor, index = scene.get("sensor"), scene.get("index")
            if (sensor not in SENSORS or not isinstance(index, str) or
                    not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", index) or index in (".", "..")):
                raise DownloadError("已有景目录记录包含非法标识，已停止。")
            if not (guard.output / sensor / index / "scene.json").is_file():
                return 3
    roi, crs, payload = load_roi(args.roi, args.crs, args.buffer_m, args.repair_geometries)
    crs_string = crs.to_string()
    tiles = make_tiles(roi, args.tile_size)
    recipe = {"version": VERSION, "roi_sha256": hash_object(payload), "start": args.start,
              "end_exclusive": args.end, "crs": crs_string, "resolution_m": RESOLUTION,
              "tile_size": args.tile_size, "bands": list(BANDS), "collections": SENSORS,
              "aerosol": args.aerosol,
              "quality": quality_recipe(args.sensor, args.aerosol), "nodata": NODATA,
              "source_resolution_m": [10, 20, 20] if args.sensor == "S2" else [30, 30, 30],
              "NDVI": "(nir-red)/(nir+red)", "STR": "(1-swir2)^2/(2*swir2)",
              "resampling": "nearest", "roi_area_m2": roi.area}
    fingerprint = hash_object(recipe)
    print(f"ROI：{roi.area / 1e6:.3f} km²；投影：{crs_string}；{RESOLUTION}米；每景最多 {len(tiles)} 块。")
    print(f"{args.sensor} 原生SR规则：{canonical_json(recipe['quality'])}。")
    project, project_source = resolve_project(args.project)
    if project:
        print(f"EE 项目：{project}（{project_source}）。")
    if args.dry_run:
        print("本地检查通过。未连接 Earth Engine，未写入任何输出。")
        return
    if not project:
        raise DownloadError("未找到EE项目。请设置optical_config.local.json或--project/EE_PROJECT。")
    from pixel_selection import SelectionPlanner, completed_record
    optimized = not getattr(args, "no_download_optimization", False)
    manifest = guard.output / "run_manifest.json"
    previous = None
    if not args.list_only:
        if not optimized and (guard.output / "download_selection_plan.json").exists():
            raise DownloadError("目录已经采用像元去重，不能切换回关闭优化模式。")
        if manifest.exists():
            if manifest.is_symlink():
                raise DownloadError("输出 manifest 是符号链接，已停止。")
            previous = json.loads(manifest.read_text(encoding="utf-8"))
            if previous.get("recipe_hash") != fingerprint:
                raise DownloadError("输出目录已有不同参数/ROI的下载记录。请改用新的 --out 目录。")
            saved_scenes = previous.get("scenes")
            if not isinstance(saved_scenes, list) or len(saved_scenes) > args.max_scenes:
                raise DownloadError("已有景目录记录不合法，已停止。")
            for scene in saved_scenes:
                if (scene.get("sensor") not in SENSORS or not isinstance(scene.get("index"), str) or
                        not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", scene["index"]) or
                        scene["index"] in (".", "..") or
                        scene.get("id") != SENSORS[scene["sensor"]][0] + "/" + scene["index"] or
                        type(scene.get("time_ms")) is not int):
                    raise DownloadError("已有景目录记录包含非法标识，已停止。")
            if optimized and saved_scenes and all(completed_record(sys.modules[__name__], guard.output,
                    scene, fingerprint, {t.name: t for t in tiles}) is not None for scene in saved_scenes):
                print(f"复用已完成月任务：{len(saved_scenes)} 景；无需重新查询或下载。", flush=True)
                return
        elif guard.output.exists() and any(guard.output.iterdir()):
            raise DownloadError("输出目录非空且没有本程序的 manifest，请换一个专用输出目录。")
    if getattr(args, "resume_complete_only", False):
        return 3
    ee = initialize_ee(project, args.proxy)
    # GeoJSON/Shapefile edges are planar, so set geodesic explicitly.
    roi_geometry = ee.Geometry(payload["features"][0]["geometry"], proj="EPSG:4326", geodesic=False)
    roi_fc = ee.FeatureCollection([ee.Feature(roi_geometry)])
    # Coarse metadata filter; the image is subsequently clipped to the exact polygons.
    from shapely.geometry import shape, box
    coordinates = payload["features"][0]["geometry"]
    geographic_bounds = shape(coordinates).bounds
    bounds = ee.Geometry.Rectangle(list(geographic_bounds), geodesic=False)
    # Aggregate bounded metadata, including projected footprints, in ONE RPC.
    # The full size is checked separately from the bounded metadata arrays.
    if previous is not None:
        scenes = previous["scenes"]
        print("复用已冻结的月度景目录，保持原断点和观测顺序。", flush=True)
    else:
        metadata_requests = {}
        for sensor, (collection_id, _) in SENSORS.items():
            collection = ee.ImageCollection(collection_id).filterBounds(bounds).filterDate(args.start, args.end)
            limited = collection.sort("system:time_start").limit(args.limit or (args.max_scenes + 1))
            with_metadata = limited.map(lambda image: image.set("_download_metadata", {
                "index": image.get("system:index"), "time_ms": image.get("system:time_start"),
                "projected_footprint": image.geometry().transform(ee.Projection(crs_string), 1)}))
            metadata_requests[sensor] = ee.Dictionary({"count": collection.size(),
                "items": with_metadata.aggregate_array("_download_metadata")})
        try:
            metadata = ee.Dictionary(metadata_requests).getInfo()
        except Exception as error:
            if is_transient_error(error):
                raise TransientDownloadError("影像目录查询时网络/代理暂不可用；网络恢复后可续跑。") from None
            raise DownloadError(f"影像目录查询失败（{type(error).__name__}）；请检查项目权限/配额及日期范围。") from None
        if sum(int(items["count"]) for items in metadata.values()) > args.max_scenes:
            raise DownloadError(f"候选景超过 --max-scenes={args.max_scenes}，请缩短日期/缩小 ROI。")
        scenes = []
        for sensor, items in metadata.items():
            for item in items["items"]:
                index = item["index"]
                scenes.append({"sensor": sensor, "id": SENSORS[sensor][0] + "/" + index, **item})
        scenes.sort(key=lambda item: (item["time_ms"], item["id"]))
        if args.limit:
            scenes = scenes[:args.limit]
    print(f"需要处理 {len(scenes)} 景（{args.sensor}，不进行时间合成）。")
    for scene in scenes:
        scene["time_utc"] = datetime.fromtimestamp(scene["time_ms"] / 1000, timezone.utc).isoformat()
        if args.list_only:
            print(f"{scene['time_utc']}  {scene['id']}")
    if args.list_only:
        return
    guard.check()
    if previous is None:
        write_json(manifest, {"recipe_hash": fingerprint, "recipe": recipe, "scenes": scenes,
                   "created_utc": datetime.now(timezone.utc).isoformat(), "ee_api_version": ee.__version__}, guard)
    selection = SelectionPlanner(sys.modules[__name__], ee, guard, scenes, tiles, crs_string,
        fingerprint, roi_fc, args.reflectance_scale, args.aerosol) if optimized else None
    if selection:
        print("启用下载前晴空检查和同一传感器/精确时间像元去重；旧块优先复用。", flush=True)
    with TileDownloadPool(workers, args.proxy) as pool:
        for position, scene in enumerate(scenes, 1):
            print(f"[{position}/{len(scenes)}] {scene['sensor']} {scene['index']}", flush=True)
            footprint = shape(scene["projected_footprint"])
            if footprint.is_empty or not footprint.is_valid:
                raise DownloadError("影像 footprint 为空或无效，请检查目标投影。")
            relevant = [tile for tile in tiles if roi.intersection(footprint).intersection(box(*tile.bounds)).area > 0]
            if selection:
                selection.process(scene, relevant, pool)
                continue
            source = ee.Image(scene["id"])
            image = build_image(ee, source, scene["sensor"], roi_fc, args.reflectance_scale, args.aerosol)
            scene_dir = guard.output / scene["sensor"] / scene["index"]
            completed = pool.download_scene(image, relevant, scene_dir, crs_string,
                                            guard, fingerprint, args.aerosol, scene)
            write_json(scene_dir / "scene.json", {**scene, "recipe_hash": fingerprint,
                       "source_bands": list(SENSORS[scene["sensor"]][1]), "tiles": completed,
                       "valid_pixels": sum(item["valid_pixels"] for item in completed)}, guard)
    print(f"完成：{guard.output}。候选处理包含复用旧景与跳过无晴空/已覆盖数据。")


def main():
    args = parser().parse_args()
    try:
        status = run(args)
    except KeyboardInterrupt:
        print("\n已中断。重新执行相同命令可继续；已完成的块将校验后跳过。", file=sys.stderr)
        return 130
    except TransientDownloadError as error:
        print(f"网络暂时失败：{error}", file=sys.stderr)
        return 75
    except (DownloadError, ValueError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Avoid echoing signed download URLs or authentication material in raw traces.
        print(f"错误：{type(error).__name__}。请检查依赖、ROI读取、EE权限及代理。", file=sys.stderr)
        return 1
    return status or 0


if __name__ == "__main__":
    import signal
    def stop_download(_signal, _frame):
        # Let pool __exit__ cancel/drain owned workers before the process exits.
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop_download)
    raise SystemExit(main())
