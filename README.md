# Landsat Sentinel Download Manager

[![Offline tests](https://github.com/sdnufushiyuan/landsat-sentinel-download-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/sdnufushiyuan/landsat-sentinel-download-manager/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

English: resumable per-acquisition Landsat 8/9 and Sentinel-2 surface-reflectance archives for OPTRAM, with monthly scheduling, smaller-region-first completion, verified GeoTIFFs and live progress estimates. Documentation is currently in Chinese.

这是一套独立的 OPTRAM 输入归档程序，来源是 Landsat Collection 2 Tier 1 Level 2 与 Sentinel-2 L2A SR。代码基于 [HLS Download Manager](https://github.com/sdnufushiyuan/hls-download-manager) 的 MIT 下载引擎独立适配，保留原作者版权。本仓库无需安装HLS程序。

三类数据**默认统一30米网格**。同一研究区内按 L8 → L9 → S2 的顺序分月执行，月内最多1–8路块传输（默认2）；该区三类数据全部通过后再推进下一区。不同研究区按估算剩余工作量优先：剩余候选景数 × ROI面积 ÷ 输出像元面积。它是调度估算，不能当作实际下载字节。库存未查询时先按面积估算。

仓库仅包含代码、示例配置和文档，不含真实ROI、影像、下载进度、EE项目与认证、服务器地址、代理节点或私人运行命令。适用于Linux、Python3.11+；Windows可运行离线测试和计划检查，正式归档使用Linux文件锁。示例展示六区配置，归档器也支持用户自定义的唯一研究区代码。

## 数据与科学规则

| 类型 | Earth Engine集合 | red / nir / swir2 | 原始像元大小 | SR换算 |
|---|---|---|---|---|
| Landsat8 | `LANDSAT/LC08/C02/T1_L2` | SR_B4 / SR_B5 / SR_B7 | 30/30/30米 | DN × 0.0000275 − 0.2 |
| Landsat9 | `LANDSAT/LC09/C02/T1_L2` | SR_B4 / SR_B5 / SR_B7 | 30/30/30米 | DN × 0.0000275 − 0.2 |
| Sentinel2 | `COPERNICUS/S2_SR_HARMONIZED` | B4 / B8A / B12 | 10/20/20米 | DN × 0.0001 |

Sentinel-2并非所有波段都是10米：B8宽近红外是10米，B8A窄近红外与B12短波红外是20米。本程序为沿用HLS的近红外选择而使用B8A。S2可显式指定20米或10米**输出网格**，但10米输出会重采样B8A/B12，不代表获得10米的原生短波红外信息。默认最近邻重采样，QA类别不插值。没有额外执行HLS的BRDF、光谱响应或跨传感器一致化校正，不能把本输出当作HLS产品。

保留每次观测，不做月均值/中值合成。使用 `(nir-red)/(nir+red)` 计算NDVI，`(1-swir2)^2/(2*swir2)` 计算STR；排除swir2≤0或nir+red绝对值≤1e-6的像元，不额外截断反射率。精确相同传感器与毫秒观测时刻的重叠像元去重，不跨日期、L8/L9或数据集去重。

- Landsat：排除QA_PIXEL位0–5和7（填充、膨胀云、卷云、云、云影、雪、水）；排除所用波段饱和位3/4/6及地形遮挡位11。SR_QA_AEROSOL排除填充与水位0/2；默认只保留气溶胶等级1（low），可显式使用`climatology-low`保留0/1。没有额外要求气溶胶有效检索位1为1，也没有排除插值位5；规则完整记录在配方中。
- Sentinel-2：仅保留SCL=4/5（植被、裸土）。不依赖2022–2024有缺档的QA60。S2没有与HLS气溶胶分类完全对应的字段，因此默认**不设置AOT阈值**；如明确需要可在私有配置设置`s2_max_aot`物理阈值。AOT按DN×0.001保存，不能当成Landsat气溶胶分类。
- 输出8波段float32：前5为red/nir/swir2/NDVI/STR；Landsat后3为QA_PIXEL/SR_QA_AEROSOL/QA_RADSAT；S2后3为SCL/AOT/reserved_zero。QA保留原值，S2末波段恒0。所有波段共同掩膜，NoData=-9999。
- 每块SHA256、投影/网格/波段、QA与指数一致性检查；无损DEFLATE压缩。`.part`不计完成，断点校验失败时停止保护旧文件。月度配方、源景ID、ROI哈希、掩膜、比例/偏移、API版本和运行时间均记录为JSON。

可用起点：L8为2013-03-18；L9为2021-10-31；S2 SR为2017-03-28。2017–2018年S2 L2A在EE中不是全球完整覆盖，空月份记为库存0，不能宣称补齐了缺档。不自动混入TOA/L1C。结束日期不包含在范围内。

官方定义（核对日期2026-10-20）：[Landsat8](https://developers.google.com/earth-engine/datasets/catalog/LANDSAT_LC08_C02_T1_L2)、[Landsat9](https://developers.google.com/earth-engine/datasets/catalog/LANDSAT_LC09_C02_T1_L2)、[Sentinel2](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED)。EE支持原生档案筛选与有界块计算；本地保存每景数据及配方，以便重复分析。当前没有Drive/GCS批量导出后端，超限月份会报错，需拆分时间/ROI，不自动降低分辨率。

## Linux使用

需要Python3.11+、已认证的EE账号与注册Cloud项目。程序不会自动启动OAuth、修改计费或代理节点。先按Earth Engine官方流程完成账号和项目授权，再填写自己的项目ID。

```bash
git clone https://github.com/sdnufushiyuan/landsat-sentinel-download-manager.git
cd landsat-sentinel-download-manager
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.json config.local.json
# 编辑config.local.json：真实ROI、CRS、实际area_m2、独立NAS输出、EE项目和代理
# 示例area_m2=1仅为占位；请填入真实米制面积，否则优先顺序没有实际意义
# 可直连时将proxy改成"none"；不使用NAS时修改output_root并删除require_mount

# 默认仅输出本地计划，不连接EE、不创建下载目录
python archive.py --config config.local.json

# 明确查询原生数据集的月度候选计数，冻结为专用库存；不下载栅格
python archive.py --config config.local.json --inventory runs/inventory.json --query-inventory

# 正式下载：一个独立进程，单月内并发；不启动HLS的三节点队列
python -u archive.py --config config.local.json --inventory runs/inventory.json --run

# 另一个终端只读看进度，Ctrl+C仅退出面板
python watch_progress.py --config config.local.json --inventory runs/inventory.json
```

`config.local.json`里的默认2路是为了控制首次运行负载；可改为1–8，不影响科学配方或现有块。代理可指向Linux现有Mihomo的8118端口，沿用该代理已有选路；本程序不部署第二个选路器。当前实现单Linux进程管理，不宣称已复制HLS的三节点集群编排。若同时跑HLS，两套程序共享网络与EE配额。

需要让出网络带宽时，可在私有配置中设置`"workers": 1, "max_mib_s": 1.0`，将新归档的块传输限制为每路约1MiB/s；0或省略表示不限速。单ROI命令使用`--max-mib-s 1`。这是每个工作线程的平均传输限制，不限制EE后台计算或元数据查询；多线程时总速率上限相应倍增，不保证共享配额完全没有竞争。调整限速不改变科学配方或已有断点。

示例配置只提供占位ROI路径和演示时间窗口，用户需自行准备输入。**不能复用HLS库存或下载缓存**，只有本程序原生数据的库存才有效。修改日期、ROI、分辨率或掩膜后需新库存与新输出；并发、代理和项目ID可调整。NAS挂载缺失、剩余空间低于`OPTICAL_MIN_FREE_GIB`、永久权限/数据错误时停止；临时网络退出75，控制器最多追加4次重试、间隔300秒；仍失败时，仅在配置并启动systemd服务的情况下延迟重启。验证块文件后复用，不从头重下。

每区完成标志为`输出根/区域代码/region_complete.json`，表示L8/L9/S2所有非空月均已处理并验证；不是每月都有晴空像元，也不是目录内存在未经掩膜的原始全波段影像。后续使用每景目录里的GeoTIFF与scene.json，而非把三个传感器混在一起估计同一套OPTRAM参数。

面板候选进度包含下载、有效缓存复用及质量/重叠跳过；速度和ETA按最近最多15分钟计算，至少120秒及3个新候选才显示。文件速度按**已完成景**记录的文件字节增长估算，长景未结束前会暂时为0，不等于网卡瞬时速度。不同区域/传感器体量差异会影响全档案ETA。

```text
独立NAS根目录/
  SDR/
    Landsat8/2013-04-11__2013-05-01/L8/景ID/*.tif
    Landsat9/2021-10-31__2021-11-01/L9/景ID/*.tif
    Sentinel2/2017-03-28__2017-04-01/S2/景ID/*.tif
    region_complete.json
  REM/ …
```

后台服务示例见`native-optical-archive.service.example`，须替换路径后安装。示例不自动启用或启动。首次先运行每个传感器的小样本：

```bash
python download_optical.py --sensor L8 --roi /path/roi.shp --start 2023-06-01 --end 2023-07-01 --out /nasdata/native-test/L8 --crs EPSG:32650 --limit 1 --workers 1
# L9/S2分别改--sensor和新的--out。正式归档不使用--limit。
```

## 验证

```bash
python -m unittest discover -p 'test_*.py'
```

离线测试包括官方比例与偏移、所有QA/SCL拒绝类别、服务器表达式与本地掩膜一致性、实际GeoTIFF指数/网格校验、并发签名串行、故障续传、精确时刻去重、调度顺序与ETA边界。真实EE小样本验证结果另见`VALIDATION.md`，以实际记录为准。

`verify_real_sample.py`是显式联网的小样本工具，需私有配置包含SDR研究区及有效EE权限；测试2023年5–10月、每个传感器最多5个低云量候选和一个约1.92公里方形范围，不会启动正式归档。GitHub CI只运行离线测试，不使用认证或EE配额。

## 文件与许可

`download_optical.py`负责逐景下载及验证；`pixel_selection.py`负责晴空预检与精确时刻像元去重；`archive.py`冻结月度库存并调度；`watch_progress.py`只读显示速度与ETA。`native-optical-archive.service.example`为可编辑的后台服务示例，不会自动安装或启动。

程序使用[MIT许可](LICENSE)。影像数据分别遵循USGS和Copernicus数据使用条款，代码许可不改变数据许可。
