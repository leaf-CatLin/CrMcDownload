# 路线图

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M0 | 仓库与 8 个分支、uv 工程骨架、文档 | 已完成 |
| M1 | 下载核心：镜像重写、代理、下载器、元数据、安装器 + 单测 | 已完成 |
| M2 | 启动核心：classpath、natives、Java、参数、进程；真实安装 1.14 | 已完成 |
| M3 | 镜像服务端 + 部署物；`mc.yjlt.top` 上线 | 已完成 |
| M4 | PyQt6 界面：启动页、下载中心（游戏/模组/整合包/资源包/光影/Java）、插件页框架、设置 | 已完成 |
| M5 | 离线能力：断网启动、离线包导出/导入、Java 本地化 | 已完成 |
| M6 | 部署到生产服务器并切换域名实测 | 已完成 |
| M7 | 内容生态：Modrinth 搜索/安装、`fabricmaven`/`modrinth`/`modcdn` 路由、整合包安装 | 已完成 |
| M8 | 加载器：安装时选原版/ Fabric/ NeoForge/ Forge，已有实例可补装；模组/光影只能装到带加载器的实例 | 已完成 |
| M9 | PyInstaller 打包、CI、发布（`v0.5.0` / `v0.5.3` / `v0.5.4` Release） | 已完成 |
| M11 | 下载源择优：镜像 vs 直连时延探测（`SourceRouter`） | 已完成 |
| M12 | 安装版本页与内容安装目标（`Target`）、下拉框美化；Issue #1 安全与容量加固（`v0.5.3`） | 已完成 |
| M13 | Forge/NeoForge 依赖预下载 + `--offline` 安装 + 启动自愈；界面精修 9 项（`v0.5.4`） | 已完成 |
| M14 | 联机状态探测 + 热度榜、自定义皮肤（CSL + `/skin`）、Web 看板（`/page`）、启动提速、插件雏形（仅官方）（`v0.6.0`） | 已完成 |
| M10 | 插件市场化（第三方来源 + 动态加载）与外置登录账户服务（LittleSkin / authlib-injector）—— **0.6.x** | 待开始 |

## 验收基线

1. 空目录经镜像安装 1.14，全部文件 SHA-1 校验通过；
2. 安装 26.1.2（Java 25）并启动到主菜单；
3. 断网后已安装版本仍可启动，启动阶段零网络请求；
4. 离线包导入后可无网启动；
5. 客户端请求只发往 `download_base`（内容层在镜像不可用时才回落官方 Modrinth）；
6. 镜像请求命中缓存，`/metrics` 上游计数为 0；
7. `CrMCDownload.exe` 免 Python 完成安装与启动；
8. 模组/资源包/光影一键安装到当前实例，整合包自动建实例并可启动。

## 当前测试基线

| 仓库 | 命令 | 数量 |
| --- | --- | --- |
| `user` | `python -m unittest discover -s tests` | ~420（`test_updater` / `test_microsoft_auth` / `test_theme` 需装 dev 依赖 pytest 与 PyQt6） |
| `server` | `python -m pytest tests -q`（或 `unittest discover`） | 206 |
| `user-downloader` / `user-launcher` / `user-ui` | 同上 | 快照落后于 `user`，未随 0.8.x 同步 |

另有离屏界面自检 `devtools/ui_smoke.py`（断言式）与截图 `devtools/ui_shots.py`。
