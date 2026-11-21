# CrMCDownload 客户端（user 分支）

Minecraft 启动器客户端：从自建镜像下载游戏版本、模组、整合包、资源包与光影，离线账户启动，PyQt6 界面（默认浅色 / 可选 PCL 风格）。

## 界面

顶部固定导航栏（**启动 / 下载 / 联机 / 插件 / 设置**）+ 下方内容区；窗口长宽比 3:2（默认 1200×800，最小 1080×720）。
导航顺序在三处（界面、README、冒烟测试）保持一致，`devtools/ui_smoke.py` 会直接断言。

- **启动游戏**：左侧账户卡（头像、账户名、「每日一言」、版本信息、启动按钮、版本设置 / 皮肤入口），
  右侧资源面板横向页签 **已装模组 / 我的存档 / 已装资源 / 已装光影**，右上角「去下载更多」直达对应下载分类。
  页签命名刻意与下载页的「模组 / 整合包 / 资源包 / 光影」区分：这里是**已装**内容，下载页是**可下载**内容。
  存档列表直接读存档目录里的 `icon.png` / `icon.ico` 当图标（没有才用占位图）；
  **点击存档进入「存档详情页」**：只读解析 Anvil 存档，把世界元数据（种子 / 版本 / 难度 /
  世界边界 / 天气 / 数据包 / 维度时钟）、玩家（坐标、背包、末影箱、经验、进度时间戳、
  游玩统计）、区块（数量、完整区块、方块排行、生物群系、地表高度）、容器与物品
  （含 1.20.5 组件与旧版 tag 两套附魔解析）、实体、兴趣点（床 / 工作站 / 传送门）、
  结构、地图（`map_*.dat` 颜色字节还原成图片，可点开放大）、游戏规则、**历史日志**
  （进度时间戳 + 区块写入日期 + 游戏日志）一次性排出来；扫描在后台线程，几万区块的
  存档也不会卡界面。解析器在 `offcraft/saves/`（NBT / 区域文件 / 位打包 / 报告四层，
  零第三方依赖），不会读写玩家存档的任何字节；
  **资源包/光影包列表用包内自带的 `pack.png` 当图标**（没有才回退占位图）；
  双击资源包/光影包会打开**内容浏览页**：左边是默认展开 3 层的目录树（可一键全展开/全折叠），
  右边预览包内图片——透明区域铺经典灰白棋盘格，16×16 / 32×32 的小贴图按整数倍放大
  （最近邻插值，像素画边缘不糊），左右各一张背景卡片；
  **没有 Iris / OptiFine / Oculus 前置时「已装光影」页签自动隐藏**（光影单独放着不生效）。
- **下载中心**：游戏版本（正式版 / 快照 / 远古版 / 已安装 四个分段，支持搜索与「安装最新版」）、
  模组、整合包、资源包、光影（Modrinth 搜索 → 详情 → 版本 → 一键安装）、**Java**
  （Mojang 官方 java-runtime 列表，可下载并设为启动 Java）、**服务端**。
  目标目录下拉末尾是「选择文件夹…」，可直接把任意文件夹（例如已有的 `.minecraft`）当作安装目标，
  取消选择会回到上一次的有效目录。内容列表用 `QStyledItemDelegate` 自绘（不再每行一个 QWidget）。
  分页有三条路径：首屏一次 60 条；之后每 5 秒自动往下补 30 条（底栏显示「正在加载更多……」）；
  滚动到已加载内容一半时再预取一页。若用户在当前 5 秒窗口内自己触发过加载，这一轮自动追加让位，
  下一轮恢复；列表到底后定时器自动停表。
  **安装模组/光影前会先查前置**：目标目录里已装的前置不重复报，缺必需前置时弹窗列出名称并询问
  是否一并下载（选「否」只装本体）。
  **窗口最小化或超过 30 秒无操作时，内容列表与联机的自动加载会暂停**（用户操作后自动恢复；
  手动发起的下载任务不受影响）。
- **联机大厅**：热门榜（服务端热度）/ 我的收藏 / 我的发布 / 最近访问 / 已屏蔽五个分区，每卡显示 favicon / MOTD /
  版本徽章 / 在线人数 / 延迟三档徽章；支持刷新、复制地址、详情、加入游戏（≥1.20.2 直连，否则复制地址）。
  切换分区时按服务器地址增量复用卡片，不再整表重建。
- **插件**：只渲染官方插件（capability 白名单、不动态加载第三方代码）；离线联机以官方 bundle
  按需下载，其余分区用统一空态组件说明现状。
- **设置**：左侧 7 个分区（外观 / 游戏 / Java 与内存 / 下载 / 皮肤 / 更新 / 关于）均带图标，
  左侧底部常驻「打开游戏目录 / 打开数据目录」；所有修改即时保存，界面上不再有「保存设置」按钮。
  下载源地址不在界面上展示，只能按 **F12** 打开「下载源设置」。
- 底部任务面板（标题 / 阶段 / 详情 / 进度条 / 取消）与状态栏（连接状态灯、实例与文件计数）。

### 视觉与性能基线

界面由 `offcraft/ui/tokens.py` 的一张 token 表驱动（颜色 / 字号 / 圆角 / 间距 / 层级），
`theme.py` 只负责把 token 展开成 QSS；切换「PCL 风格」只是换另一份 token，不再维护两套重复选择器。

- 字号收敛到 8 档（caption 9 / label 9 / body 10 / bodyStrong 10 / h3 12 / h2 14 / h1 19 / hero 22），
  圆角收敛到 3 档（control 8 / card 12 / pill 999）。
- 卡片与背景对比 ≥ 1.15:1、描边与背景 ≥ 1.15:1、次要文字 ≥ 4.5:1（WCAG AA），
  `tests/test_theme.py` 会直接断言这些数值。
- `devtools/ui_bench.py` 是性能回归门禁，当前基线见该文件顶部表格（例如「填充 100 行内容列表」从
  1616 ms 降到 < 5 ms、「列表滚动一帧」从 14.6 ms 降到 < 3 ms）。

### 快捷键

| 快捷键 | 作用 |
| --- | --- |
| `Ctrl+1` ~ `Ctrl+5` | 切换到 启动 / 下载 / 联机 / 插件 / 设置 |
| `Ctrl+L` | 启动 / 结束游戏 |
| `Ctrl+D` | 下载中心 |
| `Ctrl+,` | 设置 |
| `Ctrl+K` | 聚焦当前页面的搜索框 |
| `F5` | 刷新当前页面（实例 / 内容列表 / 联机 / 插件） |
| `F12` | 打开「下载源设置」 |

托盘菜单同样覆盖常用入口：显示主界面、启动游戏、下载中心、联机、设置、当前版本、更换皮肤、打开游戏目录、退出。

下载源与运行日志：界面任何位置都不显示下载源地址、令牌与代理，正常操作也改不了；
按 **F12** 才会打开「下载源设置」窗口。启动器不提供任何日志查看入口（日志仅写入数据目录，供排查用）。

### 下载源择优（镜像 vs 直连）

`offcraft/net/latency.py` 的 `SourceRouter` 代替原始 `Mirror` 作为下载源：同一个上游文件
先分别量一次「镜像中转」和「上游直连」的首字节时间（新建连接 + `Range: bytes=0-2047`，
2 个样本取最快，单次 5s 超时），谁快用谁，决策按路由缓存 600s。

- 设置页开关「下载时比较镜像与直连的延迟，自动用更快的一边」（`config.json` 的 `prefer_fastest`，默认开）；
- 关闭或 `--no-fastest` 时回到恒走镜像的旧行为（`Mirror`）；
- 镜像无对应路由 / 双方都不可达 / 只剩一方可达都有明确分支，结论记录在 `Decision.detail`；
- 设置页「测试连接」会清空缓存后重新探测，并追加每个路由的时延结论；
- Modrinth 多个候选域名用 `SourceRouter.rank()` 按实测时延排序后取第一个。

### 自动更新（0.7.0）

设置页的「更新」卡片会请求镜像的 `/api/update`（未配下载源时回落 GitHub Releases API），
拿到最新版本后按下面的顺序决定从哪下安装包（`offcraft/net/updater.py`）：

1. **先测直连**：对 GitHub 安装包地址发 `Range: bytes=0-2047` 的 GET，读首字节耗时；
2. **超过阈值（默认 1200 ms）就清 DNS**：Windows 走 `ipconfig /flushdns`（无窗口），
   macOS / Linux 走对应的刷新命令，然后重测一次；
3. **重测仍慢就用镜像**：改走 `{download_base}/github/<owner>/<repo>/releases/download/<tag>/<file>`，
   由服务端代理 GitHub 资产；
4. **断点续传**：安装包先写 `.part`，中断后再次下载会带 `Range` 续传（`Downloader.download(keep_part=True)`）。

请求同时带 `X-OffCraft-Version`，服务端若返回 **426**（版本低于最低要求），
界面会直接提示"客户端版本过低"并给出发布页 / 下载按钮，不会静默失败。
选源结论（直连多少毫秒、是否清过 DNS、最终走哪条线）会写在任务面板详情里。

## 加载器与 Java

| 加载器 | 安装方式 | 能否装模组 / 光影 |
| --- | --- | --- |
| 原版 `vanilla` | 只装 Mojang 文件 | 否（只能装资源包） |
| Fabric | 拉 loader profile JSON 并补齐依赖库 | 是 |
| Forge | 下载 installer jar，`--installClient` 安装 | 是 |
| NeoForge | 同上 | 是 |

- 安装版本时先弹「选择加载器」，默认原版；已有实例可在版本右键菜单「安装加载器…」补装。
- 模组 / 光影只能在带加载器的实例上安装：内容页会把原版实例标为「原版·不支持模组」并禁用安装按钮。
- Java 选项卡读取 `launchermeta` 的 `java-runtime/all.json`，展开到 `runtime/<component>/bin/java.exe`，
  与启动器自动选 Java 的扫描路径一致；Forge / NeoForge 安装器必须有可用 Java。
- **缺 Java 自动补全（0.9.0）**：启动时若找不到匹配的 Java，会弹窗询问是否自动安装；
  确认后按版本要求挑组件（精确同主版本优先，其次更高的最小版本，例如需要 19 就装 21），
  后台下载到 `runtime/` 并**自动重试启动**，不需要用户自己去设置页找 Java。
  判断只认「需要 Java / 没有找到可用的 Java / 未检测到 Java」三种文案，不会把缺依赖文件误判成缺 Java。

## 内容下载

统一由 `offcraft/content/` 提供：

| 类型 | 来源 | 安装位置 |
| --- | --- | --- |
| 模组 `mod` | Modrinth 搜索 / 版本 / 必需依赖 | `<实例游戏目录>/mods` |
| 资源包 `resourcepack` | 同上 | `<实例游戏目录>/resourcepacks` |
| 光影 `shader` | 同上 | `<实例游戏目录>/shaderpacks` |
| 整合包 `modpack` | `.mrpack` | `versions/<实例名>`（含 `overrides/`） |

- **镜像优先**：先请求 `{download_base}/modrinth/...` 与 `/modcdn/...`，镜像不可用或返回非 2xx 时自动回落到
  官方源（`api.modrinth.com` / `cdn.modrinth.com`），详情页徽章实时显示当前通道（镜像 / 官方源）。
- **依赖解析**：安装模组时对 `required` 依赖做广度优先安装（上限 24 个文件，避免异常数据导致无限扩张）。
- **整合包**：解析 `modrinth.index.json`，按 `dependencies` 安装基础版本（Fabric 自动拉取 loader profile），
  再解压 `overrides/`，最后写入 `offcraft-pack.json` 记录来源与文件数。
- **限制**：Forge / NeoForge 整合包需要先有对应加载器实例，安装器不会由整合包自动触发。

## 联机与皮肤（0.6.0）

- **联机探测**：`offcraft/servers/ping.py` 用标准库 socket 实现 Server List Ping（变长整数、Handshake →
  Login Start → JSON Response），解析延迟 / MOTD（含 `extra` 递归与颜色码剥离）/ 版本 / 在线人数 / 玩家列表 /
  favicon；探活走后台线程，主线程不阻塞。热度榜来自服务端 `/api/servers/*`。
  **页面每 5 秒自动刷新**在线状态（`AUTO_REFRESH_SECONDS`），切走页面或窗口不可用时自动停表。
- **SSRF 护栏**：`offcraft/servers/ssrf.py` 解析 `host[:port]` 并拒绝回环 / 私网 / 链路本地 / IPv6 ULA；
  默认 `allow_local_servers=false`，打开后才允许内网联机（客户端主动连任意服是有意例外，靠护栏兜底）。
- **自定义皮肤（0.9.0 起 PCL 做法 + CSL 并存）**：皮肤**只存本机**，且是**全局单槽**
  （`<数据目录>/skins/local.png`，纤细为 `local_slim.png`），不做多用户隔离——换账户/换实例
  都用同一张。上传/镜像皮肤库/远端删除全部移除。启动前双保险：
  1. `skins/pack.py` 写成 `<游戏目录>/resourcepacks/CrMCDownloadSkin/` 资源包覆盖原版玩家贴图，
     加入 `options.txt` 的 `resourcePacks`，离线可用、原版实例也生效；
  2. 带加载器的实例额外装 CustomSkinLoader，`loadlist` 里挂 `LocalSkin`（读本地
     `<用户名>.png`）与镜像源——CSL 本身也支持本地皮肤，所以保留。
  资源包同时写经典/纤细/1.19.2+ 新布局三处贴图，切模型不会退回默认皮肤；`pack_format`
  按 MC 大版本查表。`skins/default.py` 内置 64×64 默认皮肤，未设置时头像显示它。
  皮肤页提供三套原生布局：双栏卡片（默认）、工作台、极简单栏，可在设置页「皮肤页样式」切换
  （`config.json` 的 `skin_layout`），纯 PyQt 绘制棋盘格与圆角预览。
- **每日一言（0.6.2+）**：启动页读取 `https://dlystc.unknownmp.top/api/v2/sentence` 的 JSON（失败回退
  `/text?format=full`），按自然日缓存到 `<data>/daily/YYYY-MM-DD.json`，离线时显示内置文案
  （`offcraft/daily.py`）。
- **彩蛋（0.6.2+）**：设置-关于里的「千万别点」进入「点哪掉哪」模式：页面保持原样，鼠标点中哪个控件
  才把哪个控件摘下掉落（按钮/文本/输入框，圆角白底，不含背景块），可拖动甩飞；单击已掉落的方块
  会恢复页面并触发原控件，底部提示条也可点击恢复。没有 Esc/双击/静止自动恢复
  （`offcraft/ui/physics.py` + `falling.py`）。
- **外置登录预留**：`offcraft/account/` 定义 `AccountProvider` / `LaunchAccount` 与 `OfflineAccount`，启动命令
  支持 `-javaagent=` 注入；本次不实现账户服务，后续接 LittleSkin / authlib-injector 只需新增 provider。
- 配置新增字段：`skin_enabled`（默认 true）、`skin_model`（`auto|classic|slim`）、
  `skin_layout`（`v1|v2|v3`，默认 `v1`）、`account_provider`（默认 `offline`）、
  `allow_local_servers`（默认 false）。

## 版本隔离

`config.json` 的 `isolation` 默认 `true`：每个版本以 `versions/<id>/` 作为游戏目录，
`options.txt`、`saves`、`mods`、`screenshots` 相互隔离；整合包实例恒为隔离，并通过 `offcraft-pack.json` 定位目录。
设置页可关闭，关闭后回到原生布局（共享 `.minecraft`）。

## 环境要求

- Windows 10/11 x64
- Python 3.12
- PyQt6（`pip install PyQt6`）
- Pillow（`pip install Pillow`，皮肤校验与头像裁切）
- [uv](https://docs.astral.sh/uv/) 0.12+（可选，用于环境与测试）

## 运行

```bash
cd user
python main.py            # 打开图形界面
python main.py list       # CLI：list / install / check / instances / launch / pack / gui
```

首次启动使用默认下载源 `https://mc.yjlt.top`，设置页可修改下载源、令牌、代理与并发数。

## 测试与打包

```bash
python -m unittest discover -s tests -v          # 单元测试（494 项）
python ../devtools/ui_smoke.py                   # 离屏界面自检（断言式，含入口/图标/空态检查）
python ../devtools/ui_bench.py                   # 界面性能基准（p50 对比阈值，回归门禁）
python ../devtools/ui_shots.py .testbed/shots    # 输出界面截图（含 PCL 风格对照）
python ../devtools/save_shots.py                 # 存档详情页截图（自造合成存档，不需要游戏本体）
pyinstaller CrMCDownload.spec --noconfirm --clean
# 产物：dist/CrMCDownload/CrMCDownload.exe（文件夹分发）

powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_installer.ps1   # 再打安装包
# 产物：dist/installer/CrMCDownload-<版本>-win64-setup.exe
```

> `ui_smoke.py` / `ui_bench.py` 需要装有 PyQt6 的解释器。仓库里的 `user/.venv` 只装了
> `cffi / nacl / PIL`（PyQt6 在 dev 依赖组里），所以这两个脚本请用系统 Python 运行。

安装包口径（自 0.5.3 起）：

- 安装器是**自包含的 C# 程序**，源码 [`packaging/installer/Setup.cs`](packaging/installer/Setup.cs)，
  用 Windows 自带的 .NET Framework 编译器 `csc.exe` 编译，不需要 Inno Setup / NSIS / WiX；
- 应用负载以 zip 资源（`payload.zip`）内嵌在安装器中，**直接解包到目标目录**，
  全程不往 `%TEMP%` 写任何东西（规避「安装器无法创建 %TEMP%\is-XXXX.tmp，Error 5」一类环境问题）；
- 发布者统一为 **Cr**（`AssemblyCompany` / 回收站「应用和功能」`Publisher`）；
- 安装包本体带四段式文件信息（`FileVersion` / `ProductVersion` / `ProductName` / `CompanyName` / `Copyright`）；
- 覆盖升级：默认目录固定 `%LOCALAPPDATA%\Programs\CrMCDownload`，重跑新版安装包直接覆盖即可，
  无需先卸载；旧版正在运行时会先尝试关窗口，关不掉则提示用户手动退出；
- 卸载：安装目录内 `uninstall.exe`（与安装器同一份源码编译，约 33 KB，等价于 `setup.exe /UNINSTALL`），
  同时登记 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\CrMCDownload`，
  在「应用和功能」里可见（发布者 Cr、版本号、图标、卸载命令）；
- 受限目录自检：若安装包从**受限目录**（例如 `E:\root\WorkSpaces\` 下的项目工作区）启动，
  Windows 会拒绝该进程写入 C 盘，安装器会在窗口出现时/静默运行时立即给出中文说明
  （`无法写入以下位置…请把安装包复制到「下载」文件夹或桌面`），并把日志落到安装包同目录；
  把安装包复制到下载文件夹再双击即可正常安装；
- 静默安装：`setup.exe /S [/DIR=<目录>] [/NODESKTOPICON] [/NOLAUNCH]`，卸载：`setup.exe /UNINSTALL /S`。

> 注意：布局/临时目录相关的环境故障会让**任何**安装器失败，排查时可先用
> `setup.exe /S /DIR=<某个目录>` 快速区分「安装器问题」与「系统权限问题」。

## 配置

默认 `%APPDATA%\CrMCDownload\config.json`，可用 `--data-dir` 或 `OFFCRAFT_DATA` 覆盖。

## 分支

`user-downloader`（下载/安装/内容层）、`user-launcher`（启动）、`user-ui`（界面）合并到本分支；
服务端见 `server` / `server-mirror` / `server-deploy`。

## 许可证

MIT。
