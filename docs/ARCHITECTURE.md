# 架构

## 数据流

```
客户端                          镜像服务端                      上游
------                          ----------                     ----
版本清单  ──/meta/...────────▶  白名单校验 ──▶ 磁盘缓存 ──▶ piston-meta.mojang.com
版本 JSON ──/meta/v1/...─────▶                 │
client jar──/data/v1/...─────▶                 ├──▶ piston-data / launcher.mojang.com
库文件    ──/libs/...────────▶                 ├──▶ libraries.minecraft.net
资源对象  ──/assets/xx/...───▶                 ├──▶ resources.download.minecraft.net
Fabric    ──/fabric/...──────▶                 ├──▶ meta.fabricmc.net
Fabric jar──/fabricmaven/...─▶                 ├──▶ maven.fabricmc.net
Forge jar ──/forgemaven/...──▶                 ├──▶ maven.minecraftforge.net
NeoForge  ──/neoforgemaven/...▶                ├──▶ maven.neoforged.net
Java 运行时─/meta/v1/products/...▶              ├──▶ launchermeta（java-runtime all.json）
搜索 / 版本──/modrinth/...?...▶                ├──▶ api.modrinth.com（保留查询串）
内容文件  ──/modcdn/...──────▶                 └──▶ cdn.modrinth.com
```

## URL 重写

客户端只认 `download_base`，所有上游 URL 按主机映射：

| 上游主机 | 客户端路径 |
| --- | --- |
| `piston-meta.mojang.com`、`launchermeta.mojang.com` | `{base}/meta/<path>` |
| `piston-data.mojang.com`、`launcher.mojang.com` | `{base}/data/<path>` |
| `libraries.minecraft.net` | `{base}/libs/<path>` |
| `resources.download.minecraft.net` | `{base}/assets/<path>` |
| `meta.fabricmc.net` | `{base}/fabric/<path>` |
| `maven.fabricmc.net` | `{base}/fabricmaven/<path>` |
| `maven.minecraftforge.net` | `{base}/forgemaven/<path>` |
| `maven.neoforged.net` | `{base}/neoforgemaven/<path>` |
| `api.modrinth.com` | `{base}/modrinth/<path>?<query>` |
| `cdn.modrinth.com` | `{base}/modcdn/<path>` |

maven 路由下 `*.xml`（`maven-metadata.xml`）按元数据缓存（TTL 600s），其余 jar 视为不可变。

未识别主机直接报错，绝不回落到直连。唯一的例外是**内容层**（模组 / 整合包 / 资源包 / 光影）：
当镜像缺少新路由或返回非 2xx 时会主动回落到 Modrinth 官方 API，以免自建镜像未升级时功能不可用。

> 上述「绝不回落直连」只约束**上游下载的 URL 改写**。0.6.0 新增的 `/api`、`/skin`、`/page` 是镜像
> 自身提供的端点（客户端用 `api_url()` 直接拼 `{base}` 访问，不经 URL 改写）；而「联机」功能的
> Server List Ping 是客户端**主动连接任意 Minecraft 服务器**的裸 TCP —— 这是对「只与自己域名通信」
> 原则的有意例外，由 SSRF 护栏（默认拒绝私网 / 回环）兜底，详见下文「联机状态探测」。

## 下载源选择（镜像 vs 直连）

0.5.0 起，镜像不再是唯一出口：每次取文件前先比较「镜像中转」和「上游直连」的时延，择快者使用。

```
SourceRouter（offcraft/net/latency.py）
  decide(key, mirror_url, direct_url)
    ├─ 探测：新建连接 + `Range: bytes=0-2047`，量首字节时间（2 个样本取最快，单次 5s 超时，首次失败即放弃）
    ├─ 主机无镜像映射（UnknownUpstreamError）→ 直连
    ├─ 两者都不可达 → 直连
    ├─ 只剩一方可达 → 用可达的一方
    └─ 都可达 → 谁快用谁
  决策按 key（route:meta / route:forgemaven / route:modcdn / modrinth-api …）缓存 600s
```

| 场景 | 行为 |
| --- | --- |
| 设置 `prefer_fastest = true`（默认） | 探测并择优，决策缓存 10 分钟 |
| 设置 `prefer_fastest = false` | 恒走镜像（旧行为） |
| 设置页「测试连接」 | 清空缓存并重新探测，输出每个路由的时延结论 |
| CLI | `--no-fastest` 关闭择优 |

`SourceRouter` 与 `Mirror` 接口兼容（`base_url` / `rewrite` / `rewrite_optional` / `is_mirror_url`），
因此内容层、安装管线、加载器安装与 Java 运行时都无需改动；Modrinth 的多个候选域名则用 `rank()`
按实测时延排序后取第一个。

**只复用「选了哪个源」，不复用具体地址**（2026-11 修复）：`_rewrite_multi` 的路由缓存按 host 聚合
（所有 `piston-meta` / `launchermeta` 地址共用 `route:meta`），但 `Decision.url` 是当时探测的那**一个**
具体地址。早期实现直接返回 `Decision.url`，于是先取版本清单之后，同路由的其它文件
（`java-runtime/all.json`、各个版本 JSON）都会被改写成版本清单地址，表现为「Java 页永远转圈 /
清单解析失败」。现在按 `Decision.source` 为当前 url 重新挑候选，缓存只承载源的选择。

探测定理同样调整过：`decide_multi` 的网络采样**不再持锁**（旧实现把 2×5s 的采样放在 `with self._lock`
里，同一时刻其它线程的路由决策全部排队），改为「同 key 只探测一次」的事件门 + 锁内只做缓存读写。

## UI 设计 token 与渲染预算（2026-11）

界面视觉与性能的统一约定，客户端侧见 `user/offcraft/ui/tokens.py`：

- **单一 token 源**：颜色 / 字号 / 圆角 / 间距 / 层级只有 `TOKENS`（默认）与 `TOKENS_PCL`（PCL2 风格）
  两张表；`theme.app_qss(style)` 只做展开，`pcl_qss()` 仅保留结构差异（标题栏、启动按钮描边款），
  历史上那份 340 行的 PCL 重复选择器已删除。切风格 = 换 token 表，不再改选择器。
- **收敛尺度**：字号 8 档、圆角 3 档（control 8 / card 12 / pill 999）、间距 4/8/12/16/24/32/48；
  `tests/test_theme.py` 断言档数与 WCAG AA 对比度（正文 ≥ 7:1、次要文字 ≥ 4.5:1、卡片/描边与背景 ≥ 1.15:1）。
- **QSS 不写死颜色**：内联 `setStyleSheet` 从 74 处降到个位数，其余走 `QLabel[role="h2"]` 这类属性选择器
  （`theme.apply_role(widget, "h2")`），为后续暗色模式留出入口。
- **渲染预算**（`devtools/ui_bench.py` 断言）：首帧 < 600ms、打开页面 < 400ms、填充 100 行内容列表
  < 200ms、列表滚动一帧 < 8ms、整窗重绘 < 20ms。

关键实现选择，都是踩过坑之后定下来的：

| 问题 | 做法 |
| --- | --- |
| 每行一个 QWidget 的列表（100 行 1.6s、滚动 14ms/帧） | `ContentDelegate(QStyledItemDelegate)` 自绘；图标走 item data + `block_pixmap` 的 `lru_cache` |
| 每次刷新 `list.clear()` 重建 | `_sync_rows` 按 id 增量增删，顺序不变时只回写数据 |
| 淡入后 `QGraphicsOpacityEffect` 常驻 | `motion._finish` 里 `setGraphicsEffect(None)` 真正卸载；波纹改为每窗口一个复用层 |
| 背景渐变每次 `paintEvent` 重建 | `Backdrop` 预渲染成 QPixmap，尺寸变化才重建；GIF 缩放改 `FastTransformation` |
| 隐藏页面的 `Spinner` 仍 16ms 跑定时器 | `hideEvent` 停表，`showEvent` 按需启（且要求 `isVisible()`） |
| 版本搜索每敲一个字整表重排 | 180ms 防抖 + `block_pixmap` 缓存（400 个图标 89ms → 2.4ms） |
| 30 秒定时器在 GUI 线程跑资源审计 | 磁盘审计移到工作线程，结果 `post` 回主线程做差异更新 |
| 全盘扫描其他 `.minecraft`（冷扫 162s） | `scan_minecraft_dirs` 改为已知候选根 + 深度 3 + 4s 截止 |
| 设置页每次显示都探测 Java | `list_java_cached`（5 分钟，`refresh=True` 绕过） |
| 首帧前做 Java 探测 / 清单读盘 / 扫实例 | `QTimer.singleShot(0, _start_background_init)` 推到首帧之后 |
| 每张内容图标都 new 一个 Downloader（每个建一次 SSL context，枚举系统证书库 21.8ms） | `net/downloader.shared_ssl_context()`：进程级共享一份 SSL 上下文 |
| 60 张图标 = 60 个线程，GIL 下把主线程饿死（实测 34.3s「未响应」） | 有界 4 线程池 `_icon_pool` + 按线程复用 Downloader |
| 图标解析触发 `SourceRouter` 时延探测（首次可达 40s） | 图标是装饰性资源，直接 `Mirror.rewrite` 纯字符串改写，不做择优 |
| 下载页 7 个页签一次性构造 130ms | 先放占位，`_fill_download_tabs()` 逐页签补造，每步让出事件循环 |
| 页面首次点击才现场构造 | 启动后 `warm_pages()` 依次预热四个一级页面 |
| 图标等用户翻到才加载 | 启动即按分类（模组/整合包/资源包/光影）各起一条预取线程，按批入池 |

### 响应性契约

界面线程的停顿必须小于 60ms（Windows 的「未响应」判定阈值远高于此，
但 60ms 是肉眼可感知卡顿的下限）。所有会读盘、联网、起子进程的工作都必须
满足下面任一条：

1. 在 `warm_pages()` / `_fill_download_tabs()` 这类**分片**流程里执行，
   每片之间 `QTimer.singleShot(0, ...)` 让出事件循环；或
2. 放进工作线程，结果通过 `window.post()` 回主线程。

`devtools/ui_smoke.py` 断言「刷新不阻塞主线程」，`devtools/ui_bench.py` 断言
`show_page` / `apply_result` / 滚动 / 重绘的耗时上限；新增页面或加载路径时
应同步补一条断言。

### 内容列表的分页节奏

`ContentPage` 有三条加载路径，共用同一个请求互斥（`_requesting` / `_loading_more`），
同一时刻只会有一个请求在飞：

| 路径 | 触发 | 条数 |
| --- | --- | --- |
| 首屏 | 进入页签 / 换筛选 | `PAGE_SIZE` = 60 |
| 定时自动追加 | 首屏完成后每 `AUTO_LOAD_SECONDS` = 5s | `AUTO_APPEND_SIZE` = 30 |
| 滚动预取 | 滚动条过已加载内容 50% | 60 |

定时追加是**让位式**的：用户在本轮 5 秒窗口内自己加载过（点「重试」或触发滚动预取），
`_auto_skip_round` 置位，这一轮自动追加直接跳过，下一轮恢复——避免自动加载和用户
操作抢同一个请求槽。到底（`offset >= total`）后定时器自动停表；页面 `hideEvent`
时暂停、`showEvent` 时恢复，不在看不见的页面上空跑网络。

`tests/test_content_autoload.py` 用离屏 Qt + 桩客户端覆盖了这些契约（首屏 60、
每 5s 补 30、用户加载后跳过一轮、到底停表、暂停恢复），不依赖网络也能在 CI 里跑。

### 空闲门控：30 秒无操作就停止自动加载

后台自动加载（内容列表定时追加、联机页 5 秒刷新）只在「用户真的在用」时跑：

```
_ActivityFilter(QObject)  装到 QApplication，捕获鼠标/键盘/滚轮/触摸
        → note_user_activity()  刷新 _last_user_activity（鼠标移动 300ms 节流）
_sync_idle_gate()         由 5s 巡检定时器 + changeEvent(WindowStateChange) 驱动
        → loading_allowed() = 窗口可见且未最小化 且 距最后操作 < IDLE_STOP_SECONDS(30s)
        → 不允许则停 _content_timer / 联机自动刷新 / 内容页定时追加
```

暂停只在「自动」加载上生效：用户手动点出来的下载任务照常跑完。
恢复是自动的——任何一次操作都会解除暂停并重启当前页的定时器。

> 注意：`_ActivityFilter` 必须由窗口持有引用（`self._activity_filter`），且在
> `closeEvent` 里 `removeEventFilter`。否则 Qt 析构时可能回调已释放的窗口，
> 在进程退出阶段触发访问冲突（0xC0000005，表现为「指标全过但退出码异常」）。

### 资源包内容浏览

双击启动页「已装资源 / 已装光影」里的条目 → `offcraft/ui/pack_view.py:PackBrowser`：
左侧按路径分层的 `QTreeWidget`，右侧图片预览（等比缩放、可滚动）。`.zip` 用
`zipfile` 按需读、文件夹直接读，**不解压到磁盘**；单包最多列 `MAX_ENTRIES`(4000) 项、
单图超过 `MAX_IMAGE_BYTES`(16MiB) 不加载。左右两个面板各套一张 `Card`，
目录默认展开 `DEFAULT_EXPAND_DEPTH`(3) 层，另有「全部展开 / 全部折叠」。

> 树不继承 QSS 的 `color`，只靠调色板会显得发灰（在 0.62 透明度的卡片上尤其明显）。
> 因此 `PackBrowser` 对目录名 / 图片名显式设 `TEXT`，非图片文件与「大小」列设 `MUTED`，
> 并给 `QTreeWidget` / `::item` / `QHeaderView::section` 写局部 QSS 兜底。
> 不要用 `palette().darker()`——调色板前景本就接近黑，再 darker 反而更难读。

预览控件是自绘的 `_CheckerCanvas`（不是 QLabel），两个要点：

1. **透明区域铺经典灰白棋盘格**（`CELL`=8px，`#FFFFFF` / `#D6D6D6`，QPainter 自绘不用图片资源）。
   棋盘格只画在图片实际占用的矩形内并裁剪——图片比可视区小时，四周留白不应该也是棋盘格，
   否则整屏很花。无图/读失败时走 `set_message()`，只显示文字、不画格子。
2. **小贴图整数倍放大**：资源包常见 16×16 / 32×32，原样显示只有指甲盖大。按可视区做
   整数倍放大（上限 16 倍）并用**最近邻插值**（`FastTransformation`）保住像素画硬边；
   平滑插值会把像素画糊成一团。大图仍走等比缩小 + `SmoothTransformation`。

缩放每帧按画布自身 `rect()` 计算（不在加载时算一次），窗口拉伸时图片自适应。

> 写相关测试时注意：`QPixmap.fill()` 不带参数默认填**白色**而非透明，
> 会盖住棋盘格导致断言失败；构造透明图必须 `fill(Qt.GlobalColor.transparent)`。

### 资源包内部图标

`launch/resources.py:pack_icon_bytes()` 读取资源包根目录的 `pack.png`（游戏内资源包
列表显示的就是它），`ui/app.py:_resource_icon()` 拿它当启动页列表图标。支持
zip / 文件夹 / `*.zip.disabled` 三种形态（`.disabled` 只是改了后缀，`zipfile` 按内容
判断格式，直接打开即可），任何异常静默回退到按文件名生成的占位图。

### 前置依赖检测与光影页签

| 检查 | 位置 | 规则 |
| --- | --- | --- |
| 下载前查前置 | `content/install.missing_dependencies` | 只认 `required`；有 `file_name` 精确比对，否则 `project_id` → slug 前缀匹配已装文件 |
| 有无光影前置 | `launch/resources.has_shader_loader` | `mods/` 里有 `iris-*` / `optifine*` / `oculus-*` 的 `.jar`（`.disabled` 不算） |

缺前置时弹窗列名询问是否一并下载；没有光影前置时「已装光影」页签直接隐藏——
光影包单独放进 `shaderpacks/` 不会生效，留着是死按钮。

## 安装管线

```
版本清单 → 版本 JSON → client jar → 库（规则过滤 + natives 分类器）
        → 资源索引 → 资源对象 → logging 配置 → versions/<id>/<id>.json → 实例注册
```

- 每个文件：`.part` 临时文件 + `Range` 续传 + SHA-1/大小校验 + 原子 rename；
- 已存在且哈希正确即跳过，支持断网复用与离线包导入；
- 进度按字节聚合，支持取消与重试。

## 内容管线（模组 / 资源包 / 光影）

```
搜索（facets：project_type / categories:<加载器> / versions:<MC 版本>）
→ 版本列表（取 primary 文件 + required 依赖）
→ 下载（SHA-1 / SHA-512 / size 校验，.part + 原子 rename）
→ 安装到 <实例游戏目录>/{mods,resourcepacks,shaderpacks}
```

必需依赖按广度优先安装，单次安装上限 24 个文件，防止异常依赖图无限扩张。

## 加载器与 Java 运行时

```
安装版本 → 选择加载器对话框（原版 / Fabric / Forge / NeoForge）
  ├─ 原版      ：只装 Mojang 文件，不能装模组（资源包不受影响）
  ├─ Fabric    ：拉 loader profile JSON → versions/<fabric-...>/<id>.json → 补库
  └─ Forge/NeoForge：下载 installer jar → java -jar <installer> --installClient <mc_dir> → 识别新版本目录

Java：读取 Mojang java-runtime all.json → 按平台列出 jre-legacy / java-runtime-*
     → 展开清单 files（拒绝 ../ 越界）→ runtime/<component>/bin/java.exe
```

Forge / NeoForge 安装器会直接从自己的 maven 拉依赖（不受镜像控制），且必须先有可用的 Java。
游戏内“能不能装模组”由实例的加载器决定：`原版` 实例只能装资源包，模组 / 光影必须选带加载器的实例
（已有实例可在版本右键菜单「安装加载器…」补装）。

## 整合包管线

```
.mrpack → modrinth.index.json（dependencies → MC 版本 + loader）
→ 安装基础版本（Fabric：拉取 loader profile JSON 并展开为版本文件）
→ 逐个安装 index 文件（相对路径，拒绝 ../ 越界）
→ 解压 overrides/ → 写 offcraft-pack.json
```

Forge / NeoForge 加载器需要经由「安装加载器…」或安装时选择，整合包自身不会触发安装器。

## 启动管线

```
合并 inheritsFrom → rules 过滤 → classpath（客户端 jar 置尾）
→ natives 解压 → 占位符展开（arguments / minecraftArguments）
→ 选择 Java（runtime 组件 / JAVA_HOME / 常见 JDK / PATH）
→ 子进程启动（cwd = 游戏目录） + 日志流
```

离线账户：`--username` + v3 UUID（`MD5("OfflinePlayer:" + name)`）+ `accessToken=0`。

## 版本隔离

`isolation=true`（默认）时游戏目录 = `versions/<id>/`，`--gameDir` 与子进程工作目录均指向它，
`options.txt`、`saves`、`mods`、`screenshots` 互不干扰；整合包实例恒为隔离，并通过 `offcraft-pack.json` 定位。
关闭隔离后回到原生布局（共享 `.minecraft`）。

## 离线能力

- 已安装版本启动阶段零网络请求；
- 镜像不可达时回落到本地实例与缓存清单；
- 离线包：`versions/<id>` + 规则过滤后的 libraries + assets（可选 runtime）打包，导入端解压即可启动。

## 安全边界（0.5.3）

**镜像令牌**：`Downloader(token=...)` 必须同时给出 `token_origin`（默认取 `download_base`）。
每个请求先算 `origin_of(url)`，只有与 `token_origin` 完全一致（scheme + host + port）才附加
`X-OffCraft-Token`；未配置 `token_origin` 时一律不发（fail-closed），避免令牌随上游回落泄漏。

**信任名单**：`offcraft/net/trust.py` 维护 Mojang / Forge / NeoForge / Fabric / Modrinth /
GitHub 及其 CDN 主机；`check_url(url, allow_untrusted=False)` 返回中文原因或 `None`。
整合包仅在上游回落处使用：未知主机 + 默认策略 → 跳过该文件并提示，
`allow_untrusted_sources=true` → 提示后直连。主下载路径（镜像）不受影响。

**离线包 / 整合包导入预算**：成员数、单文件、总解压体积、元数据 JSON 大小各有上限
（`pack/offline.py` 与 `content/modpack.py`）。全量成员先做纯内存校验（路径、数量、体积、
大小写不敏感的重名、目标冲突），全部通过后才写入，每个文件写 `<name>.importing` 再 `os.replace`，
中途异常回滚已创建的文件。

**镜像服务端容量**：`Cache(root, max_bytes, max_file_bytes)` 按 mtime 做 LRU 淘汰；
写入走唯一临时名，`commit()` 用全局锁 + 重试保证并发下的 `os.replace` 安全；
`SingleFlight` 让同一 URL 只回源一次，其余请求在 `flight_wait` 内等待；
上游并发由 `BoundedSemaphore(max_upstreams)` 控制，超限直接 503；
`/health` 返回 `cache` 统计（字节数、文件数、命中/淘汰计数）。

## 联机状态探测（0.6.0）

```
客户端 offcraft/servers/
  ping.py      标准库 socket 实现 Server List Ping（Handshake protocol=-1 → Login Start → JSON Response）
  ssrf.py      地址解析 + 公网判定（拒绝回环/私网/链路本地/CGNAT/IPv6 ULA），allow_local_servers 才放开
  store.py     收藏 + 最近访问（config.json / servers.json）+ 探测结果缓存（TTL 300s）
  directory.py 拉服务端热度榜（GET {base}/api/servers/list|detail），走 token-origin 规则
```

服务端 `server_mirror/servers.py` 用**标准库 asyncio**（非 aiohttp —— SLP 是裸 TCP，`asyncio.open_connection`
更贴切）在独立线程的事件循环里按 `SERVER_PROBE_INTERVAL` 批量探测 `enabled` 服务器，写 `ping_history` 与
`servers_state`；停止时 `call_soon_threadsafe(loop.stop)` + 线程 join 保证干净退出。探测目标同样过 SSRF 校验。

## 皮肤链路（PCL 资源包 + CSL 并存）

上传链路已从界面移除。皮肤**只存本机**，且是**全局单槽**（不做多用户隔离）。

```
客户端 offcraft/skins/
  upload.py   单槽：<数据目录>/skins/local.png（纤细 local_slim.png）
              save_slot_skin / load_slot_skin / remove_slot_skin
              Pillow 校验并自动判 classic|slim
  avatar.py   8×8 头像 NEAREST，供启动页头像
  pack.py     路径 ①：启动前生成 <游戏目录>/resourcepacks/CrMCDownloadSkin/
              ├─ pack.mcmeta（pack_format 按 MC 大版本查表）
              ├─ pack.png（手写最小 PNG，不依赖 Pillow）
              └─ assets/minecraft/textures/entity/player/{steve,alex,wide/steve}.png
              → 再写进 options.txt 的 resourcePacks
  csl.py      路径 ②：带加载器的实例装 CustomSkinLoader，
              loadlist 首位挂 LocalSkin（读 <数据目录>/skins/<用户名>.png）+ 镜像源
```

启动前 `_prepare_custom_skin` 依次跑两条路径，**互为兜底**：资源包失败时 CSL 仍能显示皮肤，
CSL 装不上（原版实例 / 无加载器 / 无下载源）时资源包仍生效。

要点：

- **原版实例也能显示**：资源包覆盖原版玩家贴图，不需要 mod；
- **CSL 支持本地皮肤**，所以保留：`sync_local_skin_file()` 把单槽复制成 CSL 认识的
  `<用户名>.png`，`LocalSkin` 类型完全离线；没有下载源时照样写配置，有源时再补装 jar；
- **三处贴图都写**（经典 / 纤细 / 1.19.2+ 的 `player/wide/`），否则玩家切成纤细模型会退回默认皮肤；
- `pack_format` 必须匹配游戏版本，否则资源包被标成「不兼容」而不启用，故按大版本查表
  （1.12→4、1.16→5、1.18→8、1.20→15、1.21→34，未知版本用宽松默认值）；
- 资源包写到**实例自己**的游戏目录（尊重「版本隔离」），路径由 `instance_game_dir()` 决定；
- 任何失败只记 WARN，绝不影响启动。

服务端 `/skin` 与 `SkinStore`、以及 `skins/upload.py` 里的镜像上传函数仍保留（历史数据与既有测试在用），
但客户端界面已无上传入口。`offcraft/account/` 预留 `AccountProvider` 抽象（当前仅 `OfflineAccount`），
`command.py` 支持 `options.java_agent` 拼 `-javaagent=`，后续接 LittleSkin / authlib-injector 只需新增 provider 实现。

## 缺 Java 自愈（0.9.0）

```
launch_game → build_command
   └─ LaunchError 且文案命中「需要 Java / 没有找到可用的 Java / 未检测到 Java」
        → 投递 ("java_missing", (实例 id, 所需主版本, 原文))
        → QMessageBox 询问「是否现在自动下载并安装 Mojang 官方运行时」
        → 是：读 java-runtime/all.json → pick_runtime_for_major() 挑组件
             → install_java_runtime() 展开到 runtime/<component>/
             → 写 java_overrides["__global__"] → 自动重试启动同一实例
        → 否：提示「需要 Java N 才能启动」，不做任何改动
```

`pick_runtime_for_major` 的选择规则：精确同主版本优先 → 否则取**更高的最小**版本
（需要 19 就装 21，不要装 17 或 8）→ 清单里全都更低时兜底最高可用版本；
版本没声明 `javaVersion` 时优先 Java 8。识别条件只看那三种文案，避免把「缺依赖文件」
误判成缺 Java——两组断言分别在 `tests/test_java_autofix.py`。

## 服务端 API 与热度数据库（0.6.0）

`proxy.py:_dispatch` 在 `/health` 之后按前缀分流到 `api.py:ApiRouter`（`resolve()` 保持只做代理白名单，不改语义）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/servers/list?q&limit&offset` | 热度榜 + 最近状态（读免令牌，`Cache-Control: max-age=15`） |
| `GET /api/servers/detail?host&port` | 含玩家列表与最近 24 点延迟曲线 |
| `POST /api/servers/report` | 客户端上报新服务器，需 `X-OffCraft-Token`，按 host 令牌桶限流 |
| `GET /api/stats/overview\|cache\|download\|servers` | Web 看板数据 |

`db.py:Database` 用标准库 `sqlite3`（WAL + `RLock` 串行写，`check_same_thread=False`），三张表：
`servers`（成员 + reports/last_seen）、`servers_state`（最近一次探测快照）、`ping_history`（时间线）。
热度 = `reports + 近24h平均在线/50 + last_seen 半衰加分`，同分按在线降序再按延迟升序。

## 插件模型（0.6.0，仅官方）

```
offcraft/plugins/
  base.py      PluginManifest(frozen) + Plugin 协议；PLUGIN_API 主版本门控；能力白名单含
               net.tunnel（离线联机特权，validate() 强制「仅 official=True 可声明」）
  context.py   PluginContext 只暴露白名单能力：config 命名空间(plugins.<id>.)/notice/log/
               register_nav_item/register_task/instance_list/download_file(内部强制过 trust+镜像)
               以及 p2p_open_tunnel()（net.tunnel 声明位，返回「允许联网」标记，不给宿主网络对象）
  bundle.py    已下载官方插件包的落地与完整性：install_zip 先校 zip sha256（下载时）、
               verify_bundle 逐文件重算 sha256 比对 installed.json（加载时）；禁路径穿越
  registry.py  内置按 import 白名单发现 + 扫描 config_dir()/plugins/<id>/ 的第一方 bundle（校验
               通过才 spec_from_file_location 加载、把目录插入 sys.path）；启停持久化 plugins.json
  builtin/diagnostics.py  示例官方插件（capability mirror_probe），聚合连通性自检卡片
```

不给原始 `MainWindow`、不给任意文件写、不给裸网络接口；第三方目录扫描留 TODO 且不执行。

### 离线联机 = 特权官方插件 `offline-multiplayer`（按需下载）

- **能力例外**：`net.tunnel` 是**仅官方、经镜像 sha256 校验**的显式特权能力——离线联机引擎用标准库
  `socket` 直连中继（数据面端到端加密，服务端只见密文）；这是插件沙箱「禁裸网络」的唯一官方例外，
  第三方动态加载始终禁止。入口只在「插件」页一张专用卡片（下载 → 启用 → 进入），不加顶层导航。
- **按需下载**：基础安装包 `excludes` 掉纯 Python 引擎 `offcraft.p2p`（`CrMCDownload.spec` /
  `OffCraftDebug.spec`）；`devtools/build_mp_plugin.py` 把 `offcraft/p2p/*` 重排为顶层包 `offcraft_p2p` +
  插件入口 `plugin_main.py` + 包内 `manifest.json` 打成 zip，并产出 `sha256/size/abi` 描述符供服务端
  `GET /plugin/offline-multiplayer.{manifest.json,zip}` 下发。面板经 `_engine()` 解析（优先 `offcraft_p2p`，
  回落 `offcraft.p2p` 便于开发/测试），故基础包不含引擎也能按需加载。
- **nacl 后备**：Phase 0 冻结环境加载外部 `.pyd` 的权威判定留待打包验证；未确证前 `pynacl`/libsodium
  仍随基础包保留（hiddenimports 不动），只按需下载纯代码——满足「功能选择性下载」，暂不省体积。
- **房间语义**：`房间码+预共享密钥` → `房间名(全局唯一、不区分大小写)+密码`；生命周期
  `创建`(服务端登记，全局唯一名 + 每设备现存<10) / `开放`(房主开实时会话) / `加入`(仅连已开放会话，
  绝不自动建房) / `删除`(房主删，释放名额)。表单仅收房间名+密码；转发端口默认 25565、不可用时从
  游戏 `logs/latest.log` 探测(`p2p/port_probe.py`)。
- **房主控制面 `host_secret`**：端到端模型下 `open()` 会把 `host_sid` 下发给**所有加入者**，故 `host_sid`
  不足以单独授权踢人。`开放` 时服务端另随机生成 `host_secret=token_hex(32)`、**仅回给房主、不进名册**；
  `kick` 服务端以 `hmac.compare_digest` 校验 `host_secret` 且 `sid==host_sid`（否则 403），并调
  `RelayServer.evict()` 按 `(room,sid)` 登记跨线程关闭桥接两端 writer、撤销命中该 sid 的在途配对令牌，
  将被踢者加入 `room.blocked`（`join`/`relay` 一律拒绝其旧 sid），同时把该成员稳定的
  `p2p_device_id` 写入 `room.blocked_devices`——**同一设备换新 sid / 新公钥也无法重新加入**，
  封禁随 `删除` / 重开房间或房主 `rekey` 清空。`rekey` 由房主提交新的 `auth_key`（密码只在本地派生），
  服务端原子替换后 `evict` 所有非房主成员并清空封禁，旧成员不知道新密码即无法回流；`roster` / `kick`
  仍要求 `sid == host_sid` + `host_secret`（403 兜底）。`roster` 供房主/成员拉取在线名册。
- **机器绑定存储**：加入成功后把房间名+密码经 **Windows DPAPI**（per-user+per-machine，`CryptProtectData`）
  加密存本地，非 Windows/失败回落首启随机 `machine.key` + `SecretBox`（见 `p2p/vault.py`）。**局限**：
  同账户同机可还原明文；重装/清配置会重置 `p2p_device_id`（配额与设备身份均为软约束）。设备身份与
  密码/auth_key 都不入库、不上传明文，准入仍靠 `proof` HMAC。

## 启动耗时与可观测性（0.6.0）

`launch_game` worker 分段 `perf_counter`（load_version / classpath / natives / java / build / spawn）写日志文件
（界面不提供日志入口）。提速手段：Java 探测结果磁盘缓存（`config.java_cache`）、natives 解压与 classpath 校验
`ThreadPoolExecutor(2)` 并行、版本解析进程内 LRU（键 `id + mtime_ns + size`）、页面懒构建（`_page_factories` +
`_ensure_page`，首屏只造启动页）。`devtools/launch_probe.py` 在固定 fixture 上量各段耗时作回归基线。
