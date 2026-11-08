<script setup lang="ts">
import { latestVersion } from "@/api/client";

const logs = [
  {
    version: "0.8.5",
    date: "2026-11-08",
    items: [
      "新增 Microsoft 正版账号登录：启动器内嵌登录窗口自动截获授权回调，无需再手动粘贴跳转地址。",
      "另有设备码登录与手动粘贴两种备用方式；设备码会醒目显示在窗口中，并提供「复制代码」按钮，不再自动占用剪贴板。",
      "登录令牌只保存在本机并自动续期，退出登录立即删除。",
      "新增隐私政策（客户端「关于」页与官网 /page/privacy 同口径）。",
      "服务器收藏与新增只保存在本机，不再自动上报；上报需手动确认。",
      "新增服务器举报入口，举报内容进入管理端人工审核。",
    ],
  },
  {
    version: "0.8.0",
    date: "2026-11-08",
    items: [
      "新增 P2P 虚拟局域网：同一房间的玩家可像在同一个局域网里联机，无需公网端口。",
      "下载源支持多线路配置，自动探测延迟与实测速度后择优，过慢时自动重新探测并切源。",
      "探测前自动清理 DNS 缓存；普通权限下会请求管理员权限，而不是静默跳过。",
      "新增管理端 CrMC 项目：服务器清单与举报审核。",
    ],
  },
  {
    version: "0.7.0",
    date: "2026-11-06",
    items: [
      "新增完整官网：首页、下载、更新日志、关于，运维看板迁移到 /status。",
      "新增更新路由：最低 / 最新版本可配置，GET /update 会 301 跳到 GitHub Releases。",
      "客户端请求统一携带 X-OffCraft-Version；低于最低版本时服务端返回 426 并提示强制更新。",
      "自更新流程：先测 GitHub 直连延迟，过高时清理 DNS 缓存重测，仍然偏高才切换镜像。",
      "自更新支持断点续传，安装包下载中断后可从 .part 文件继续。",
      "设置页新增「检查更新」，会显示选源结果与下载进度。",
      "看板新增 GitHub Release 资产代理路由 /github/，用于镜像下载安装包。",
    ],
  },
  {
    version: "0.6.2",
    date: "2026-10-30",
    items: [
      "卡片加入毛玻璃质感，列表底色与控件统一为半透明。",
      "启动即强制刷新版本清单，缓存不再阻塞网络拉取。",
      "下载页详情卡片默认隐藏，点击对象后动画展开，可再次收起。",
      "接入 ldrs 风格环形加载动画，替换原先的纯文字等待。",
      "支持自定义背景：纯色、PNG、GIF，可叠加颜色蒙层并保留底层渐变兜底。",
    ],
  },
  {
    version: "0.6.0",
    date: "2026-10-18",
    items: [
      "服务端新增联机热度榜、皮肤托管、插件下发与 Web 看板。",
      "镜像支持 Fabric、Forge、NeoForge、Modrinth 等内容路由。",
      "客户端支持一键安装、依赖补全与断点续传。",
    ],
  },
];
</script>

<template>
  <div>
    <section class="page-head">
      <div class="wrap">
        <span class="eyebrow">CHANGELOG · 当前 v{{ latestVersion }}</span>
        <h1>更新日志</h1>
        <p>每一次改动都记在这里。修 bug 是日常，加功能是顺手喵。</p>
      </div>
    </section>

    <section class="section" style="padding-top: 8px">
      <div class="wrap">
        <div class="changelog">
          <article v-for="log in logs" :key="log.version" class="log-item">
            <div class="log-head">
              <span class="log-version">v{{ log.version }}</span>
              <span class="log-date">{{ log.date }}</span>
              <span v-if="log.version === latestVersion" class="pill green">当前版本</span>
            </div>
            <ul>
              <li v-for="item in log.items" :key="item">{{ item }}</li>
            </ul>
          </article>
        </div>
      </div>
    </section>

    <section class="section" style="padding-top: 0">
      <div class="wrap">
        <div class="band">
          <div>
            <h2>想追最新的开发进度？</h2>
            <p>仓库、提交记录、Release 都在 GitHub 上，欢迎来玩。</p>
          </div>
          <a class="btn" href="https://github.com/leaf-CatLin/CrMcDownload" target="_blank" rel="noreferrer">去 GitHub</a>
        </div>
      </div>
    </section>
  </div>
</template>
