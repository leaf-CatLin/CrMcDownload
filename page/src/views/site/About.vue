<script setup lang="ts">
import { latestVersion } from "@/api/client";
</script>

<template>
  <div>
    <section class="page-head">
      <div class="wrap">
        <span class="eyebrow">ABOUT · v{{ latestVersion }}</span>
        <h1>关于 CrMcDownload</h1>
        <p>
          它最初只是一个「不想再等 Mojang 下载」的小工具，后来慢慢长成了启动器 + 镜像服务 +
          联机小站。名字里的 Cr 是 Creeper，MC 是 Minecraft，剩下的全是猫猫的私心。
        </p>
      </div>
    </section>

    <section class="section" style="padding-top: 8px">
      <div class="wrap">
        <div class="about-grid">
          <div class="prose">
            <h2>这个小项目在做什么</h2>
            <p>
              客户端负责把 Minecraft 装好、把 Java 找齐、把模组和整合包摆平；
              服务端负责把公开的元数据与文件缓存下来，让同一份资源只需要从外网拉一次。
            </p>
            <p>
              它不收集账号信息，不内嵌广告，也不会偷偷改你的游戏文件。所有代码以 MIT 协议开源，
              你可以在 GitHub 上看到每一次提交。
            </p>
            <p>
              0.7.0 开始，服务端可以配置「最低支持版本」：旧客户端请求资源时会收到 426，
              并附带最新版本与发布页地址，避免版本过旧导致资源不兼容喵。
            </p>
          </div>

          <div class="credits">
            <h2>致谢与许可</h2>
            <ul>
              <li>
                <a href="https://github.com/guillermolg00/morphicons" target="_blank" rel="noreferrer">Morphicons</a>
                —— SVG 图标形变动效，MIT License。导航菜单的开合动画就来自它。
              </li>
              <li>
                <a href="https://lucide.dev" target="_blank" rel="noreferrer">Lucide</a>
                —— 图标风格参考，ISC License。
              </li>
              <li>
                <a href="https://modrinth.com" target="_blank" rel="noreferrer">Modrinth</a>
                —— 模组与整合包元数据来源。
              </li>
              <li>本项目与 Mojang / Microsoft 无关，Minecraft 是 Mojang 的商标。</li>
            </ul>
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="wrap">
        <div class="section-head">
          <div>
            <div class="section-tag">ENDPOINTS</div>
            <h2>服务端主要端点</h2>
            <p>镜像地址可以在客户端设置里换成你自己的部署。</p>
          </div>
        </div>
        <table class="endpoint-table">
          <thead>
            <tr><th>端点</th><th>说明</th></tr>
          </thead>
          <tbody>
            <tr><td><code>/meta/</code></td><td>Mojang 版本清单与版本 JSON</td></tr>
            <tr><td><code>/fabric/</code> · <code>/forgemaven/</code> · <code>/neoforgemaven/</code></td><td>加载器元数据与 jar</td></tr>
            <tr><td><code>/modrinth/</code> · <code>/modcdn/</code></td><td>模组搜索、项目、版本与文件</td></tr>
            <tr><td><code>/github/</code></td><td>GitHub Releases 资产代理（支持 Range 断点续传）</td></tr>
            <tr><td><code>/api/update</code></td><td>最新 / 最低版本、强制更新标记、下载地址</td></tr>
            <tr><td><code>/update</code></td><td>301 重定向到 GitHub Releases 页面</td></tr>
            <tr><td><code>/status</code></td><td>运维看板：缓存、下载热度、联机、皮肤</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="section" style="padding-top: 0">
      <div class="wrap">
        <div class="band">
          <div>
            <h2>想来一起写代码吗？</h2>
            <p>Issue、PR、想法、甚至一句「喵」都欢迎。</p>
          </div>
          <a class="btn" href="https://github.com/leaf-CatLin/CrMcDownload" target="_blank" rel="noreferrer">GitHub 仓库</a>
        </div>
      </div>
    </section>
  </div>
</template>
