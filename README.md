# 草埠湖近实时农业遥感监测系统

本项目是可独立复制运行的 Windows Web 系统。复制整个 `Caobuhu_Agri_Monitor` 文件夹到另一台电脑后，可以一键安装环境、一键启动系统。

## 1. 当前状态

- 数据下载/检索：done
- Sentinel-2 L2A 下载：done
- Sentinel-2 L1C 下载入口：done
- Sentinel-1 GRD 下载：done，已改为通过 ASF 下载，支持勾选产品下载和单景进度显示
- 本地 Sentinel-2 L2A SAFE 处理：done
- Sentinel-1 SAR 预处理和 Sentinel-1/Sentinel-2 融合分析：待开发
- AOI 检索范围：支持 GeoJSON/JSON/SHP，SHP 需要同名 `.shx/.dbf/.prj`

## 2. 运行前准备

另一台电脑需要先安装 Python，建议版本：

```text
Python 3.10 / 3.11 / 3.12
```

安装 Python 时必须勾选：

```text
Add Python to PATH
```

不建议使用过新的 Python 3.14，因为部分地理空间依赖可能不稳定。

## 3. 文件夹复制方式

直接复制整个文件夹：

```text
Caobuhu_Agri_Monitor
```

可以放到任意路径，例如：

```text
D:\Caobuhu_Agri_Monitor
E:\Caobuhu_Agri_Monitor
C:\Users\你的用户名\Desktop\Caobuhu_Agri_Monitor
```

系统路径已经改为相对路径，不要求必须放在原来的开发目录。

## 4. 一键安装环境

进入 `Caobuhu_Agri_Monitor` 文件夹，双击：

```text
一键安装环境.bat
```

安装脚本会自动完成：

- 创建 `.venv` 虚拟环境
- 升级 `pip`
- 安装 `requirements.txt` 中的依赖
- 创建本地数据目录
- 创建 `.env` 账号配置模板

安装完成后会生成这些目录：

```text
data\S2\DANGYANG\raw_zip
data\S2\DANGYANG\raw_safe
data\S2\DANGYANG\processed
data\S1\DANGYANG\raw_zip
data\S1\DANGYANG\raw_safe
data\ROI
```

## 5. 账号配置

如果只处理已有本地 SAFE 数据，可以跳过账号配置。

如果需要在线检索或下载数据，需要配置对应账号：

- Sentinel-2：使用 Copernicus Data Space 账号
- Sentinel-1：使用 NASA Earthdata / ASF 账号

配置文件位置：

```text
Caobuhu_Agri_Monitor\.env
```

### 5.1 Sentinel-2 账号

Sentinel-2 数据从 Copernicus Data Space 下载。

注册地址：

```text
https://dataspace.copernicus.eu/
```

注册并登录后，在 `.env` 中填写：

```env
CDSE_USERNAME=你的Copernicus账号邮箱
CDSE_PASSWORD=你的Copernicus密码
CDSE_MAX_WORKERS=2
```

### 5.2 Sentinel-1 账号申请方式

Sentinel-1 数据已改为从 ASF 下载。ASF 下载需要 NASA Earthdata Login 账号。

申请步骤：

1. 打开 Earthdata 注册页面：

```text
https://urs.earthdata.nasa.gov/users/new
```

2. 按页面要求填写用户名、密码、邮箱和基本信息，完成注册。
3. 注册完成后登录一次 Earthdata：

```text
https://urs.earthdata.nasa.gov/
```

4. 打开 ASF 页面，并用同一个 Earthdata 账号登录或完成授权：

```text
https://search.asf.alaska.edu/
https://sentinel1.asf.alaska.edu/
```

5. 如果页面提示授权 ASF 应用访问 Earthdata，选择同意授权。

完成以上步骤后，在 `.env` 中填写 Earthdata 账号密码：

```env
EARTHDATA_USERNAME=你的Earthdata用户名
EARTHDATA_PASSWORD=你的Earthdata密码
EARTHDATA_TOKEN=
```

也可以使用 Earthdata token：

```env
EARTHDATA_USERNAME=
EARTHDATA_PASSWORD=
EARTHDATA_TOKEN=你的EarthdataToken
```

注意：

- 如果同时填写 token 和账号密码，脚本优先使用 token。
- token 会过期，过期后需要重新生成并更新 `.env`。
- 浏览器已经登录 Earthdata，不代表 Python 脚本已经认证成功；脚本仍然需要 `.env` 中的账号密码或 token。
- 第一次使用账号密码下载时，如果 ASF/Earthdata 需要应用授权，请先在浏览器完成授权后再重新下载。

## 6. 一键启动系统

双击：

```text
一键启动.bat
```

正常启动后，浏览器会打开：

```text
http://127.0.0.1:5078/
```

如果浏览器没有自动打开，可以手动复制上面的地址访问。

## 7. 数据下载

系统支持下载：

- Sentinel-2 L2A
- Sentinel-2 L1C
- Sentinel-1 GRD

下载页面可设置：

- AOI 区域文件或地图绘制范围，支持 GeoJSON/JSON/SHP
- 开始日期、结束日期
- 数据类型
- 产品名过滤
- 最大检索数量，默认留空；留空时使用脚本默认值
- 并发下载数量，默认 2，最大 10
- 检索结果勾选下载，可只下载选中的产品
- 单景下载进度显示，Sentinel-1 在 ASF 未返回文件总大小时会按已下载 MB 显示进度
- ZIP 保存目录
- SAFE 解压目录

下载脚本位于：

```text
download\S2_CDSE_download.py
download\S1_ASF_auto_download.py
```

下载来源：

- Sentinel-2 L2A/L1C：Copernicus Data Space
- Sentinel-1 GRD：ASF / NASA Earthdata

Sentinel-1 未手动指定目录时，默认保存到：

```text
data\S1\DANGYANG\raw_zip
data\S1\DANGYANG\raw_safe
```

Sentinel-2 未手动指定目录时，默认保存到：

```text
data\S2\DANGYANG\raw_zip
data\S2\DANGYANG\raw_safe
```

## 8. 数据处理

当前本地处理主流程使用 Sentinel-2 L2A SAFE。

处理输入：

```text
光学数据输入目录：data\S2\DANGYANG\raw_safe
雷达数据输入目录：预留给 Sentinel-1，当前不参与光学指标计算
处理输出目录：data\S2\DANGYANG\processed
```

处理范围支持：

- GeoJSON
- JSON
- SHP
- 地图绘制多边形或矩形

SHP 最好包含以下配套文件：

```text
xxx.shp
xxx.shx
xxx.dbf
xxx.prj
```

其中 `.prj` 用于识别坐标系，非常重要。

## 9. 当前处理项

- 长势等级
- 墒情等级
- 综合异常
- 巡田优先级
- NDVI 连续值
- 去年同期 NDVI 差值
- 疑似干旱
- 疑似积水
- 疑似撂荒
- 疑似非农化

## 10. 注意事项

- Sentinel-2 L2A 可直接进入当前本地处理流程。
- Sentinel-2 L1C 当前支持下载，但不建议直接进入 L2A 处理流程。
- Sentinel-1 当前支持下载、勾选产品下载、单景进度显示和目录预留，尚未完成 SAR 预处理和融合计算。
- 不建议处理正在下载的同一个 SAFE 目录，可能读到半成品数据。
- 如果下载失败并提示文件被占用，请关闭旧的 Python 进程、资源管理器预览、QGIS/ArcGIS 或其他占用程序。

## 11. 常见问题

### 启动提示找不到 Python

重新安装 Python，并勾选 `Add Python to PATH`。

### 依赖安装失败

重新双击：

```text
一键安装环境.bat
```

如果网络慢，可以更换 pip 镜像源后再安装。

### Sentinel-1 下载提示 401 或认证失败

检查：

- `.env` 中 `EARTHDATA_USERNAME` 和 `EARTHDATA_PASSWORD` 是否正确
- 是否已经完成 Earthdata 注册
- 是否已经在 ASF 页面同意应用授权
- 如果使用 `EARTHDATA_TOKEN`，确认 token 没有过期

### Sentinel-1 下载进度一直停在 0% 或 1%

处理方式：

- 停止当前下载任务，并重启系统后重新下载。
- 新版本已经为 Sentinel-1 增加单景进度输出。
- 如果 ASF 未返回文件总大小，进度会按已下载 MB 显示，例如 `下载中 16 MB`。
- 如果长时间没有 MB 数变化，优先检查 Earthdata 账号、网络连接和保存目录权限。

### SHP 无法定位或裁剪

检查是否同时有 `.shp/.shx/.dbf/.prj`。

如果缺少 `.prj`，系统会默认按 EPSG:4326 处理，可能导致位置错误。

### TIF 大面积为空

可能原因：

- AOI 外按设计写成 `nodata`
- 云、阴影被 SCL 过滤
- 影像没有覆盖处理范围
- 处理范围坐标系错误

## 12. 后续开发建议

- 增加下载目录与处理目录冲突保护。
- 增加 Sentinel-1 VV/VH 预处理和融合分析。
- 增加处理质量报告：云比例、有效像元比例、参与影像数量。
- 增加多景合成方式选择：中值、最大 NDVI、最少云、逐期输出。
