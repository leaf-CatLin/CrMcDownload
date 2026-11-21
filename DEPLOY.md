# 部署指南

目标：在 `yjlt.top`（Ubuntu 24.04）上运行镜像服务，由 nginx 反向代理到 `https://mc.yjlt.top`。

> 现有 80/443 已被主站占用，因此使用**子域名**最干净；如果暂时没有 DNS 记录，也可以先监听独立端口（见文末）。

## 0. 前置条件

- 子域名 `mc.yjlt.top` 的 A 记录指向 `154.219.110.63`
- 服务器可直连 Mojang（本项目已在 `yjlt.top` 实测 200）
- 磁盘 ≥ 20 GB（全量缓存会随使用增长）

## 1. 拉取代码

```bash
cd ~
git clone -b server https://github.com/crazying-dev/CrMcDownload.git CrMCDownload
cd CrMCDownload
# 安装 uv（若未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv sync --frozen
~/.local/bin/uv run --frozen main.py   # 前台验证，Ctrl+C 退出
```

## 2. 配置

复制示例并修改：

```bash
sudo mkdir -p /var/lib/offcraft-mirror
sudo chown mimi:mimi /var/lib/offcraft-mirror
cp .env.example .env
# 编辑 .env：设置 MIRROR_TOKEN、MIRROR_CACHE_DIR=/var/lib/offcraft-mirror
```

生成令牌：

```bash
openssl rand -hex 24
```

## 3. systemd

```bash
sudo cp systemd/offcraft-mirror.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now offcraft-mirror
curl -s http://127.0.0.1:8081/health
```

## 4. nginx + 证书

```bash
sudo cp nginx/offcraft.conf /etc/nginx/sites-available/offcraft.conf
sudo ln -s /etc/nginx/sites-available/offcraft.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d mc.yjlt.top
```

验证：

```bash
curl -I https://mc.yjlt.top/meta/mc/game/version_manifest_v2.json
```

## 5. 客户端

客户端配置：

```json
{
  "download_base": "https://mc.yjlt.top",
  "download_token": "<与 MIRROR_TOKEN 相同>"
}
```

或命令行：

```bash
CrMCDownload.exe --base https://mc.yjlt.top --token <token> install 1.14
```

## 5.1 内容路由（模组 / 整合包 / 资源包 / 光影 / 加载器）

镜像支持以下新前缀，客户端内容下载优先走它们，镜像不可用时才回落到官方源：

| 前缀 | 上游 | 备注 |
| --- | --- | --- |
| `/fabricmaven/` | `maven.fabricmc.net` | Fabric loader 的 jar 依赖 |
| `/forgemaven/` | `maven.minecraftforge.net` | Forge 版本索引（`maven-metadata.xml`）与安装器依赖 |
| `/neoforgemaven/` | `maven.neoforged.net` | NeoForge 版本索引与 jar |
| `/modrinth/` | `api.modrinth.com` | 搜索 / 项目 / 版本，保留查询串 |
| `/modcdn/` | `cdn.modrinth.com` | `.jar` / `.mrpack` / 图标 |

Forge / NeoForge 的**安装器自身**会直连各自 maven 拉依赖，这部分不经过镜像（客户端只能保证版本索引走镜像）。

部署后自检：

```bash
curl -s -o /dev/null -w '%{http_code}\n' "https://mc.yjlt.top/modrinth/v2/search?query=sodium&limit=1"   # 200
curl -s -o /dev/null -w '%{http_code}\n' "https://mc.yjlt.top/modrinth/v2/search?query=sodium&limit=1"   # 二次请求应为 200 且 /metrics 上游计数不变
curl -s -o /dev/null -w '%{http_code}\n' "https://mc.yjlt.top/forgemaven/net/minecraftforge/forge/maven-metadata.xml"  # 200
```

如需改写上游（例如换镜像加速），在 `.env` 里补 `UPSTREAM_FABRICMAVEN` / `UPSTREAM_FORGEMAVEN` / `UPSTREAM_NEOFORGEMAVEN` / `UPSTREAM_MODRINTH` / `UPSTREAM_MODCDN`。

## 5.2 官网、更新路由与版本门槛（0.7.0）

官网与看板是同一份构建产物，`git pull` 之后就在 `page/dist`，服务端会自动从 `MIRROR_PAGE_DIR` 读取：

| 地址 | 内容 |
| --- | --- |
| `https://mc.yjlt.top/page/` | 官网（项目介绍 / 下载 / 更新日志 / 关于） |
| `https://mc.yjlt.top/page/status` | 运维看板（概览 / 缓存 / 下载热度 / 联机 / 皮肤） |

更新相关的入口：

| 地址 | 行为 |
| --- | --- |
| `GET /update` | **301** → GitHub Releases 页面（tag 来自自动检测，发版即跟随） |
| `GET /api/update` | 返回 `latest_version` / `min_version` / `force_update` / 直连与镜像下载地址 / 全部资产 |
| `GET /api/releases` | 更新历史（GitHub Releases 自动检测 + 缓存），官网「更新日志」页读它 |
| `GET /github/<owner>/<repo>/releases/download/<tag>/<file>` | 安装包镜像代理，透传 `Range`，支持断点续传 |

### 最新版本自动检测（0.8.0 起）

**发布新客户端不需要再改 `.env`、也不需要重启服务**：服务端直接读
`https://api.github.com/repos/<MIRROR_UPDATE_REPO>/releases`，取最新的非预发布、非 draft 版本，
连 tag、发布页、资产名与更新日志正文一起缓存。

```bash
MIRROR_UPDATE_REPO=leaf-CatLin/CrMcDownload
MIRROR_UPDATE_AUTO=on             # off = 完全不联网，只用快照文件与兜底值
MIRROR_UPDATE_CACHE_TTL=600       # 快照新鲜期（秒）
# MIRROR_UPDATE_CACHE_FILE=../db/CrMcDownload/releases.json
# MIRROR_UPDATE_TIMEOUT=8
# MIRROR_UPDATE_GITHUB_TOKEN=ghp_xxx   # 可选，只提高 API 额度（60 → 5000 次/小时）
MIRROR_UPDATE_MIN_VERSION=auto    # 空 = 不设门槛；auto = 当前次版本的上一档（0.9.1 → 0.8.0）
MIRROR_UPDATE_ENFORCE=on
# 兜底版本号：仅 GitHub 不可达且没有快照文件时使用，正常不用维护
MIRROR_UPDATE_LATEST_VERSION=0.9.1
```

缓存行为（都不用运维干预）：

- 快照在 `MIRROR_UPDATE_CACHE_TTL` 内直接命中内存；过期后由第一个请求（或后台线程）
  带 `ETag` 条件请求回源，304 也算一次成功刷新，**其余请求继续用旧快照，不阻塞**；
- GitHub 不可达时按 30s → 60s → … → 30min 指数退避重试，旧快照照常对外服务；
- 快照落在 `<MIRROR_DATA_DIR>/releases.json`，重启后立刻可用，不用等首次检测；
- 想立刻验证刚发的版本，删掉快照文件再打一次 `/api/update` 即可（或等 TTL 到期）。

自检：

```bash
curl -sI https://mc.yjlt.top/update | head -n 5           # 301 + Location: .../releases/tag/v0.9.1（自动跟随最新 tag）
curl -s https://mc.yjlt.top/api/update | python -m json.tool        # latest_version / version_source / version_checked_at
curl -s 'https://mc.yjlt.top/api/releases?limit=5' | python -m json.tool   # 更新历史 + check.source / check.stale
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-OffCraft-Version: 0.8.0' https://mc.yjlt.top/api/stats/overview   # 426（门槛 0.9.1）
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-OffCraft-Version: 0.9.1' https://mc.yjlt.top/api/stats/overview   # 200
curl -s -o /dev/null -w '%{http_code}\n' -r 0-1023 https://mc.yjlt.top/github/leaf-CatLin/CrMcDownload/releases/download/v0.9.1/CrMCDownload-0.9.1-win64-setup.exe   # 206
```

> 建议先把 `MIRROR_UPDATE_MIN_VERSION` 留空跑一轮，确认 `/api/update` 与镜像下载正常后再设为 `auto`，
> 避免发布资产尚未上传时把所有人都挡在 426 上。

## 5.3 发布新版本的上线步骤

1. 客户端打包：`pyinstaller CrMCDownload.spec --noconfirm --clean` → `packaging\build_installer.ps1`；
2. GitHub 建 tag（如 `v0.9.1`），上传资产（`CrMCDownload-0.9.1-win64-setup.exe` 等）；
3. **服务端不需要改配置也不需要重启**：自动检测会在 TTL（默认 10 分钟）内跟上，
   想立刻生效就删掉 `<MIRROR_DATA_DIR>/releases.json` 再打一次 `/api/update`；
4. 按 5.2 的 curl 自检，确认 `/api/update` 的 `latest_version` 已经是新版本，
   镜像 `Range` 请求返回 206，再打开 `/page/` 与 `/page/status` 各点一遍；
5. 只有「想强制旧客户端升级」时才需要动配置：`.env` 写 `MIRROR_UPDATE_MIN_VERSION=auto`
   （或具体版本号）后 `sudo systemctl restart offcraft-mirror`。


## 备选：无子域名（独立端口）

若暂时不能加 DNS，可让 Caddy 监听 `:8443` 并自签/使用已有证书：

```bash
caddy run --config caddy/Caddyfile --adapter caddyfile
```

客户端把 `download_base` 设为 `https://yjlt.top:8443`，并确保防火墙放行 8443。

## 运维

| 操作 | 命令 |
| --- | --- |
| 查看状态 | `systemctl status offcraft-mirror` |
| 查看日志 | `journalctl -u offcraft-mirror -f` |
| 缓存命中 | `curl -s http://127.0.0.1:8081/metrics` |
| 清理缓存 | `sudo rm -rf /var/lib/offcraft-mirror/*`（服务无需重启） |
| 强刷版本快照 | `rm -f ../db/CrMcDownload/releases.json && curl -s https://mc.yjlt.top/api/update` |
| 看版本检测状态 | `curl -s 'https://mc.yjlt.top/api/releases?limit=1' \| python -m json.tool`（关注 `check.source` / `check.stale` / `check.error`） |
| 升级 | `git pull && uv sync --frozen && sudo systemctl restart offcraft-mirror` |

## 安全

- 必须设置 `MIRROR_TOKEN`，避免成为公开下载源；
- nginx 层可再加 IP 白名单或 basic auth；
- 不要把 `.env` 提交到仓库。
