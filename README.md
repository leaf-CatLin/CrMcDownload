# CrMCDownload

自建镜像 + 离线账户的 Minecraft 启动器。客户端（PyQt6，PCL 风格）从你的镜像服务器下载原版版本、模组、整合包、资源包与光影，
服务端代理 Mojang / Fabric / Modrinth 文件并做磁盘缓存。

> 背景：2026 年 10 月起，PCL、HMCL 等第三方启动器陆续停止提供游戏下载。本项目把“下载源”做成可自建的镜像服务，
> 客户端默认只与你的域名通信。

## 组成

```
┌──────────────── 客户端 (user, PyQt6) ────────────┐   ┌──────────── 服务端 (server) ────────────┐
│ 启动 · 下载中心 · 设置 · 实例 · 离线包 · 日志     │──▶│ 镜像代理 + 磁盘缓存 + 令牌 + metrics      │──▶ Mojang
│ Modrinth 内容层（模组/整合包/资源包/光影）        │   │ /meta /data /libs /assets /fabric         │──▶ Fabric
└────────────────────────────────────────────┘HTTPS└─ /fabricmaven /modrinth /modcdn ────────┘──▶ Modrinth
```

## 分支

| 分支 | 内容 |
| --- | --- |
| `main` | 文档（本分支） |
| `user` | 客户端集成：入口、配置、实例、内容层、打包、集成测试 |
| `user-downloader` | 镜像重写、代理、下载器、元数据、安装、离线包、内容层（`offcraft/content/`） |
| `user-launcher` | 版本解析、rules、classpath、natives、Java、参数、进程 |
| `user-ui` | PyQt6 界面（主题、控件、内容页、主窗口） |
| `server` | 服务端集成：`main.py` 入口、配置、测试 |
| `server-mirror` | 镜像核心：路由、缓存、Range、metrics |
| `server-deploy` | systemd / Docker / Caddy / nginx 部署物 |

每个分支在本地是一个独立 clone，目录名与分支名一致；合并方向 `user-* → user`、`server-* → server`。
详见 [docs/BRANCHES.md](docs/BRANCHES.md)。

## 客户端

顶部导航栏（启动 / 联机 / 下载 / 设置 / 插件）+ 下方内容区，窗口固定 3:2（默认 1200×800，最小 1080×720）：

- **启动游戏**：账号行、版本大图卡、内存滑块、启动主按钮；右侧版本列表（右键：启动 / 版本设置 /
  **安装加载器…** / 打开游戏目录 / 删除实例）与 6 个快捷磁贴
- **联机大厅**：热门榜（服务端 SQLite 热度）+ 我的收藏 + 最近访问，每卡显示 favicon / MOTD / 版本 /
  在线人数 / 延迟，支持刷新、复制地址、详情、加入游戏（≥1.20.2 直连，否则复制地址提示手动添加）
- **下载中心**：游戏版本（正式版 / 快照 / 远古版 / 已安装，支持搜索与安装最新版）、
  模组 / 整合包 / 资源包 / 光影（Modrinth 搜索 → 详情 → 版本 → 一键安装）、Java（Mojang 官方 java-runtime）
- **插件**：仅官方插件（capability 白名单，不加载第三方代码），示例插件「连通性自检」；
  「离线联机」为特权官方插件，基础包不内置、按需下载后在插件页卡片启用进入（见下）；其余分区保留框架
- **设置**：游戏目录、版本隔离、Java（自动检测 / 手动添加）、内存、JVM 参数、
  下载源（只读，按 F12 才能改），不再提供日志查看入口
- 底部任务面板与状态栏（连接状态、下载源、实例统计）

装版本时进入独立的「安装版本」子页选择加载器（原版 / Fabric / Forge / NeoForge）：原版只能装资源包，
模组与光影只能装到带加载器的实例。内容下载默认走镜像（`/modrinth`、`/modcdn`），
镜像不可用时自动回落到 Modrinth 官方源，徽章显示当前通道。

### 下载源择优（0.5.0）

镜像与上游直连两条路都在时，不再固定走镜像：取文件前先探测两边时延
（新建连接 + `Range: bytes=0-2047` 的首字节时间，2 个样本取最快），择快者使用，决策缓存 10 分钟。

- 设置页开关「下载时比较镜像与直连的延迟，自动用更快的一边」（`config.json` 的 `prefer_fastest`，默认开）；
- 关闭后回到恒走镜像的旧行为；命令行可用 `--no-fastest`；
- 结论按「下载源 + 代理 + 择优开关」缓存在内存里，改下载源或重启后自动重测；
- 自建镜像被限速而官源直连更快时自动改走直连，反之当官源被阻断时仍由镜像中转照顾。

### 安装版本页（0.5.3）

下载页里的独立子页（不是弹窗），四行布局：

- 首行较高：版本图标 + 版本目录名输入框（同时作为实例显示名）；
- 下面三行为 Fabric / NeoForge / Forge，点行选中，底部按钮按选择安装；
- 加载器静态兼容性在此判定（Fabric ≥ 1.14、NeoForge ≥ 1.20.2、快照不可用）；
- Forge 与 NeoForge 互斥：同一 MC 版本已装其一，另一行变灰不可选；
- 安装完成后按目录名重命名实例；被其他版本 `inheritsFrom` 的版本拒绝改名（Forge 依赖原版）。

### 内容安装目标（0.5.3）

模组 / 资源包 / 光影的安装目标改为 `Target` 抽象：

- 下拉框列出已装版本（自动换算成隔离后的游戏目录），另有「选目录」可挑任意游戏文件夹；
- 选中文件夹会读取版本 JSON 判断 MC 版本与加载器，并把筛选条件对齐到它；
- 能否安装不再只看加载器，而是看目标下是否存在 `mods/` 等子目录：版本目录缺子目录时自动创建，
  普通文件夹没有则禁止安装。

### 安全与容量加固（0.5.3，Issue #1）

- **令牌不再外泄**：`X-OffCraft-Token` 只发给镜像自身 origin（`scheme://host:port`），
  直连 Modrinth / GitHub / Mojang 时一律不带；
- **未知来源默认拒绝**：整合包上游回退先过域名白名单，未在白名单的地址默认跳过并在界面提示，
  白名单域名命中时提示「将直连下载」；需要放开时用配置 `allow_untrusted_sources`；
- **离线包 / 整合包导入加固**：路径穿越、成员数、单文件与总解压体积、重名冲突全部预校验，
  写入走临时名 + `os.replace`，失败回滚；冲突策略 `error` / `skip` / `overwrite`（CLI `--force`、`--skip-existing`）；
- **镜像服务端**：缓存容量上限（LRU 淘汰）、临时文件回收、过期项定期清扫、同一文件单飞回源、
  上游并发上限（超出返回 503），`/health` 暴露 `cache.stats()`。

### 加载器安装修复与界面精修（0.5.4）

**Forge / NeoForge 装完启动报「缺少依赖」的根因与修法**（用户实测：`26.2-forge-65.1.5` 缺 24 项依赖，原版 `26.2` 正常）：

1. **`jar_id` 取错**：`MergedVersion.jar_id` 原本在版本 JSON 缺 `jar` 时回落到加载器自己的 id，
   而加载器实例根本没有独立 jar → 改为回落到父版本（`inheritsFrom` / `chain[0]`）；
2. **安装器拉依赖时卡死**：Forge 安装器自己去官方 maven 取库（`maven.creeperhost.net` 等，
   `HttpURLConnection` 无读超时），旧实现只在进程退出后 `read()` 管道，900s 超时被 terminate，
   结果版本 JSON 写完了、依赖全缺。现在先按 `install_profile.json` + 安装器内 `version.json` 的库表
   从镜像预下载全部依赖，再以 `--offline` 跑安装器（失败才回退联网），且边跑边读输出，
   卡住时日志能看到停在哪个库；
3. **补丁 jar 不存在于上游**：`net.minecraftforge:forge:<ver>:client` 的 `artifact.url` 是空串，
   该文件由安装器的 processor 用 `data/client.lzma` 本地打补丁生成 → 新增
   `plan.is_installer_provided()` 把「有 path 但没有 url」的库识别为安装器产物并跳过下载；
4. **装完复核**：用 `build_classpath` 检查缺失，仍缺就报「安装不完整，还缺 N 个依赖」，不再静默放行；
5. **老实例自愈**：启动时若因缺库失败且带 `inheritsFrom`，自动 `install_local` 补齐后重试一次。

同一版本还顺手修了 Fabric 分支（改用 `install_local`，原来误用 `prepare_install` 导致加载器 id 不在官方清单里）。

**界面精修 9 项**：

- 启动页 hero 去掉「原版 · 不能装模组」一类说教文案；
- 全局不再展示下载源（地址、令牌、代理、徽章、设置页「下载源」卡片与「测试连接」全部移除），
  `F12` 仍可打开「下载源设置」窗口；启动器也不提供日志查看入口；
- 下载中心去掉「选择目录」按钮行，改为目标目录下拉末尾的「选择文件夹…」哨兵项，
  取消选择回到上一次有效目录；
- 修复下拉框弹层黑边（`polish_combo`：无边框 + 透明背景 + 零边距 + 最多 14 项）；
- 下载中心改为首次切换时才拉清单（`ensure_loaded()`，再次进入只同步筛选）；
- 安装版本页的非 high 行加复选框（用该加载器安装），high 行隐藏标题/副标题、高度收窄、
  「可选」徽章不再显示；
- 配套 `devtools/ui_smoke.py` 断言更新（新增「界面不出现 `mc.yjlt.top`」全局扫描等）。

### 联机、皮肤与看板（0.6.0）

- **联机大厅**：客户端用标准库 socket 实现 Server List Ping（延迟 / MOTD / 版本 / 在线人数 / 玩家列表 / favicon），
  新增「联机」导航页（左榜单右卡片），首屏先渲染缓存再后台刷新，主线程不阻塞；收藏与最近访问存本地，
  热度榜由服务端 SQLite 维护（客户端上报 + 后台 `asyncio` 批量探测 + 时间衰减）。
- **SSRF 护栏**：这是本项目首次允许客户端主动连任意主机，默认拒绝回环 / 私网 / 链路本地 / IPv6 ULA，
  只有 `allow_local_servers` 打开才允许内网联机；服务端探测走同一套规则。
- **自定义皮肤**：CSL（CustomSkinLoader）mod 经 Modrinth 镜像自动安装并写好指向 `{base}/skin/` 的配置；
  服务端 `/skin` 托管皮肤 PNG（Pillow 校验尺寸 64×64 / 64×32、64 KiB 上限、原子落盘）；客户端上传前用
  Pillow 判 `classic/slim` 并裁 8×8 头像替换启动页头像。预留 `AccountProvider` 抽象与 `-javaagent` 注入位，
  供后续接外置登录（LittleSkin / authlib-injector）。
- **启动提速**：Java 探测磁盘缓存、natives 与 classpath 并行、版本解析 LRU、页面懒构建（首屏只造启动页）、
  分段启动埋点；已安装实例的启动路径保持零网络。
- **插件接口雏形**：仅官方插件 + capability 白名单，禁止动态加载第三方代码；示例官方插件 `diagnostics`
  在设置页输出「连通性自检」卡片，插件页渲染已加载的官方插件。
- **离线联机（插件，按需下载）**：跨网直连联机改造为特权官方插件 `offline-multiplayer`，声明新能力
  `net.tunnel`（引擎用标准库 `socket` 直连中继，数据面端到端加密，服务端只见密文、不解密、不持明文）。
  基础安装包 `excludes` 掉纯 Python 引擎，启用时从服务端 `GET /plugin/offline-multiplayer.{manifest.json,zip}`
  按需下载、逐文件 sha256 校验后加载；**入口只在「插件」页一张专用卡片**（下载 → 启用 → 进入），不加顶层导航，
  第三方动态加载始终禁止。房间语义由 `房间码+预共享密钥` 改为 **`房间名`（全局唯一、不区分大小写）+ `密码`**，
  生命周期 `创建`（服务端登记，每设备最多 10 个）/ `开放`（房主开实时会话）/ `加入`（仅连已开放会话，绝不自动建房）/
  `删除`（房主删、释放名额）；表单只填 **`房间名` + `密码`**（不再有世界/端口输入，转发端口默认 25565、不可用时从游戏
  `logs/latest.log` 自动探测）。**房主权限管理**：`开放` 时服务端随机下发 `host_secret`（仅房主持有、不进名册），房主可
  `roster` 查看在线成员、`kick` 踢出指定成员（服务端校验 `host_secret`+`host_sid`，并经中继即时拆桥 + 撤销在途配对令牌）；
  踢出为「断开当前会话」而非永久封禁——被踢者若仍持有密码可重新 `加入`（阻断重加入需改密码，为已知局限）。面板同时展示
  `已进入的房间`（本机加密存）与 `所在房间`（当前会话），房主可对本机已存记录 `查看密码`。设备身份 = 客户端首启稳定随机
  `p2p_device_id`（软约束）。加入成功后把房间名+密码经
  **Windows DPAPI**（per-user+per-machine）加密存本地，非 Windows/失败回落首启随机 `machine.key`；同账户同机可还原
  明文、重装会重置设备 id 为已知局限。UI 全程不暴露 `mc.yjlt.top`/中继地址，准入仍靠 `proof` HMAC。
- **Web 可视化看板**：`server/page`（Vue 3 + Vite + vue-router，仅这两个运行时依赖、图表自绘 SVG）的构建产物
  由镜像服务在 `/page` 直出，含概览 / 缓存 / 下载热度 / 联机 / 皮肤五个页面；`dist` 缺失时 `/page` 返回 404，
  不影响其它路由。

```bash
cd user
python main.py                     # 需要 PyQt6
# 或打包
pyinstaller CrMCDownload.spec --noconfirm --clean   # dist/CrMCDownload/CrMCDownload.exe
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_installer.ps1   # 再打安装包（自带 csc，无需 Inno/NSIS）
# 安装包：dist/installer/CrMCDownload-<版本>-win64-setup.exe（发布者 Cr，可直接覆盖旧版；
#         负载以 zip 资源内嵌，解包不经过 %TEMP%）
#         注意：双击前先把安装包复制到下载文件夹——从 E:\root\WorkSpaces\ 这类受限目录
#         启动的进程写不了 C 盘，安装器会直接弹框说明而不是静默失败
```

当前版本：`0.6.0`（`user`、`user-*` 分支）。

配置默认在 `%APPDATA%\CrMCDownload\config.json`。

## 服务端

```bash
cd server
uv sync --frozen
uv run --frozen main.py
curl http://127.0.0.1:8081/health
```

路由：`/meta` `/data` `/libs` `/assets` `/fabric` `/fabricmaven` `/forgemaven` `/neoforgemaven`
`/modrinth` `/modcdn`，另带 `/health` 与 `/metrics`；0.6.0 新增 `/api/servers/*`、`/api/stats/*`、`/api/skins/status`
（联机榜单与看板数据，读接口免令牌）、`/skin/<name>.png`（皮肤托管）、`/page`（Web 看板静态资源），
以及 `/plugin/offline-multiplayer.{manifest.json,zip}`（离线联机特权插件按需下载，免令牌、只读、防路径穿越，
文件由 `devtools/build_mp_plugin.py` 产出到 `MIRROR_PLUGIN_DIR`）。`/p2p/*` 为中继信令控制面（数据面走独立 TCP 中继端口），
含 `create/list/delete/open/join/leave/announce/relay` 及房主控制面 `roster`（查在线成员）与 `kick`（凭 `host_secret` 踢人）。
带查询串的请求（如 Modrinth 搜索）会完整转发并以 `.q<sha1>` 区分缓存；
maven 路由下的 `*.xml` 索引按元数据 TTL 缓存，jar 长期缓存。

生产部署见 `server-deploy` 分支的 `DEPLOY.md`（systemd / Docker / nginx / Caddy），
当前线上：`https://mc.yjlt.top`。

## 技术栈

- Python 3.12 + uv
- 客户端 GUI：PyQt6；皮肤校验 / 头像：Pillow（第三方运行期依赖）
- 服务端：标准库 `ThreadingHTTPServer`，可选 nginx/Caddy 反代
- 打包：PyInstaller（onedir 文件夹分发，免运行期解压）

## 文档

- [架构](docs/ARCHITECTURE.md)
- [分支与协作](docs/BRANCHES.md)
- [路线图](docs/ROADMAP.md)

## 许可证

MIT，见 [LICENSE](LICENSE)。
