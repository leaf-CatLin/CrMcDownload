# CrMCDownload 服务端（server 分支）

Minecraft 文件镜像服务：为客户端代理 Mojang / Fabric / Modrinth 元数据与文件，带磁盘缓存、路径白名单与访问令牌。

## 当前版本

服务端 `0.7.0`，对外通告的客户端最新版本为 **`0.9.1`**（`MIRROR_UPDATE_LATEST_VERSION`，
`/update` 与 `/api/update` 都读它）。线上 `.env` 建议同时设 `MIRROR_UPDATE_MIN_VERSION=0.8.5`，
让 0.8.5 之前的客户端收到 426 强制更新。

发布新客户端时这里要改三处：`server_mirror/config.py` 的 `update_latest` 默认值、
`.env.example` 的 `MIRROR_UPDATE_LATEST_VERSION`、以及本段说明。

## 分支模型

| 分支 | 内容 |
| --- | --- |
| `main` | 文档、架构、部署说明 |
| `user` / `user-*` | 客户端（PyQt6） |
| `server` | 服务端集成（本分支） |
| `server-mirror` | 镜像核心：路由、缓存、代理 |
| `server-deploy` | systemd / Docker / Caddy / nginx 部署物 |

## 环境要求

- Python 3.12
- [uv](https://docs.astral.sh/uv/) 0.12+
- Linux（生产）/ Windows（开发）均可

## 启动

```bash
uv sync --frozen
uv run --frozen main.py
curl http://127.0.0.1:8081/health
```

也可以 `uv run --frozen python -m server_mirror`，两者等价。

## 路由

| 前缀 | 上游 | 说明 |
| --- | --- | --- |
| `/meta/` | `piston-meta.mojang.com`、`launchermeta.mojang.com` | 版本清单 / 版本 JSON |
| `/data/` | `piston-data.mojang.com`、`launcher.mojang.com` | client jar 等 |
| `/libs/` | `libraries.minecraft.net` | 库文件 |
| `/assets/` | `resources.download.minecraft.net` | 资源对象 |
| `/fabric/` | `meta.fabricmc.net` | Fabric loader 元数据 |
| `/fabricmaven/` | `maven.fabricmc.net` | Fabric/Intermediary jar |
| `/forgemaven/` | `maven.minecraftforge.net` | Forge 版本索引与安装器依赖 |
| `/neoforgemaven/` | `maven.neoforged.net` | NeoForge 版本索引与 jar |
| `/modrinth/` | `api.modrinth.com` | 搜索 / 项目 / 版本（**保留查询串**） |
| `/modcdn/` | `cdn.modrinth.com` | `.jar` / `.mrpack` / 图标 |
| `/github/` | `github.com` | Releases 资产（客户端安装包），支持 `Range` 断点续传 |

`/github/` 只允许 `github.com`、`api.github.com`、`*.githubusercontent.com` 三种跳转目标，
GitHub 资产 302 到 `objects.githubusercontent.com` 时由服务端跟随（最多 3 跳），
因此客户端可以只用镜像地址拿到安装包，并在断线后用 `Range` 续传。

另有 `/health`、`/metrics`，以及 0.6.0 起**由应用自身提供**（不走上游代理、不做 URL 改写）的端点：

| 端点 | 说明 |
| --- | --- |
| `/api/servers/list\|detail\|status` | 联机热度榜与状态（读接口免令牌） |
| `/api/servers/report`（POST） | 客户端上报新服务器，需 `X-OffCraft-Token`，按 host 限流 |
| `/api/stats/overview\|cache\|download\|servers` | Web 看板数据 |
| `/api/skins/status` | 已存皮肤数与最近更新 |
| `/api/skins/list` | 皮肤库列表（`?q=&limit=`，按更新时间倒序，免令牌） |
| `/skin/<name>.png`（GET/POST/DELETE） | 皮肤托管（Pillow 校验；POST/DELETE 需令牌）；`/skin/default.png` 返回内置默认皮肤，`?fallback=1` 缺图时回落默认 |
| `/update` | **301** 跳到 GitHub Releases 页面（`Location` 由 `MIRROR_UPDATE_*` 决定） |
| `/api/update` | 版本与下载地址 JSON（`latest_version` / `min_version` / `force_update` / `mirror_download_url`），永不被版本门槛拦截 |
| `/page`、`/page/<asset>` | 静态产物（`MIRROR_PAGE_DIR`，缺 `dist` 时 404）：`/page/` 是官网，`/page/status` 是运维看板 |

- 查询串上限 2048 字节，拒绝反斜杠、NUL 等非法字符；
- 带查询串的请求缓存键为 `<path>.q<sha1(查询串)[:16]>`，避免不同搜索命中同一缓存；
- `meta` / `fabric` / `modrinth` 前缀按 `MIRROR_METADATA_TTL` 过期；maven 路由下 `*.xml`
  （`maven-metadata.xml`）也算元数据，其余 jar 长期缓存；
- 未识别前缀 404，绝不回落到直连。

### 版本门槛（0.7.0）

客户端请求带 `X-OffCraft-Version`（兼容 `?v=`）时，若低于 `MIRROR_UPDATE_MIN_VERSION`：

- `/api/*`、代理路由与 `/github/*` 返回 **426 Upgrade Required**，JSON 体与 `/api/update` 同构，
  并附 `X-OffCraft-Min-Version`、`X-OffCraft-Latest-Version`、`X-OffCraft-Force-Update: 1`；
- 未携带版本号、或版本号无法解析的请求一律放行（curl / 看板 / 旧客户端不受影响）；
- `/update`、`/api/update`、`/health`、`/metrics`、`/page/*` 始终放行，保证旧客户端还能拿到升级信息。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MIRROR_LISTEN` | `127.0.0.1:8081` | 监听地址 |
| `MIRROR_CACHE_DIR` | `./cache` | 磁盘缓存目录 |
| `MIRROR_TOKEN` | 空 | 非空时校验 `X-OffCraft-Token` |
| `MIRROR_METADATA_TTL` | `600` | 元数据 / Modrinth 响应缓存秒数 |
| `MIRROR_MAX_CACHE_BYTES` | `0` | 缓存上限，0 = 不限 |
| `MIRROR_DATA_DIR` | `../db/CrMcDownload` | 持久数据根目录（启动时自动创建） |
| `MIRROR_DB_PATH` | `<DATA_DIR>/mirror.db` | 联机 SQLite 路径 |
| `MIRROR_POPULAR_DATA` | `<DATA_DIR>/popular_servers.json` | 榜单为空时导入的种子 |
| `MIRROR_SKINS_DIR` | `<DATA_DIR>/skins` | 皮肤文件根目录 |
| `MIRROR_PLUGIN_DIR` | `<DATA_DIR>/plugins` | 官方特权插件包下发目录 |
| `MIRROR_PAGE_DIR` | `./page/dist` | 官网 + 看板构建产物目录 |
| `MIRROR_SERVER_PROBE_INTERVAL` | `300` | 后台探测间隔（秒） |
| `MIRROR_SERVER_PROBE_ENABLE` | `on` | 关闭则不启动探测线程 |
| `MIRROR_UPDATE_REPO` | `leaf-CatLin/CrMcDownload` | 发布仓库（`owner/repo`） |
| `MIRROR_UPDATE_LATEST_VERSION` | `0.9.1` | 最新版本号，`/update` 与 `/api/update` 都读它 |
| `MIRROR_UPDATE_MIN_VERSION` | 空 | 最低可用版本；空 = 不做版本门槛（线上 `.env` 建议设为 `0.8.5`） |
| `MIRROR_UPDATE_ENFORCE` | `on` | 关掉后即使低于最低版本也只提示不拒绝 |
| `MIRROR_UPDATE_RELEASE_URL` | 空 | 自定义发布页地址；空 = `https://github.com/<repo>/releases/tag/v<latest>` |
| `MIRROR_UPDATE_ASSET` | 空 | 安装包文件名，可用 `{version}` 占位；空 = `CrMCDownload-<latest>-win64-setup.exe` |
| `UPSTREAM_META` / `_DATA` / `_LIBS` / `_ASSETS` / `_FABRIC` / `_FABRICMAVEN` / `_FORGEMAVEN` / `_NEOFORGEMAVEN` / `_MODRINTH` / `_MODCDN` / `_GITHUB` | 官方地址 | 上游，可替换 |
| `UPSTREAM_PROXY` | 空 | 上游代理（如 `socks5h://127.0.0.1:1080`） |

## 官网与看板（`page/`）

`page/` 是 Vue 3 + Vite 工程，一次构建同时产出两份内容：官网（`/page/`、`/page/download`、
`/page/changelog`、`/page/about`）与运维看板（`/page/status`）。构建设物直接提交到仓库，
部署时不需要在服务器上装 Node：

```bash
cd page
pnpm install            # 首次（pnpm 11，esbuild 构建白名单在 pnpm-workspace.yaml 的 allowBuilds）
pnpm build              # 产出 page/dist，随提交一起推送
```

页面所有请求都会带 `X-OffCraft-Version`（取自 `package.json` 的 version，经 vite `define` 注入），
因此最低版本策略对官网自身同样生效。

## 测试

```bash
uv run --group dev python -m pytest tests -q        # 164 项（db/api/skins/settings/p2p/probe/servers/static/update）
uv run python -m unittest discover -s tests          # 既有 unittest 用例仍可直接跑
```

服务端新依赖（`pyproject.toml`）：运行期 `pillow`（皮肤校验），开发组 `pytest`；探测用标准库 `asyncio`，未引入 aiohttp。

## 部署

见 `server-deploy` 分支的 `DEPLOY.md`。

## 许可证

MIT，见 `LICENSE`。
