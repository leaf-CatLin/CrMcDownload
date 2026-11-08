<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, latestVersion, type UpdatePayload } from "@/api/client";

const info = ref<UpdatePayload | null>(null);
const online = ref(true);

onMounted(async () => {
  try {
    info.value = await api.update();
  } catch {
    online.value = false;
  }
});

const latest = computed(() => info.value?.latest_version || latestVersion.value);
const min = computed(() => info.value?.min_version || "未设置");
const repo = "https://github.com/leaf-CatLin/CrMcDownload";
const releaseUrl = computed(() => info.value?.release_url || `${repo}/releases/tag/v${latest.value}`);
const directUrl = computed(
  () =>
    info.value?.download_url ||
    `${repo}/releases/download/v${latest.value}/CrMCDownload-${latest.value}-win64-setup.exe`,
);
const mirrorUrl = computed(
  () =>
    info.value?.mirror_download_url ||
    `/github/leaf-CatLin/CrMcDownload/releases/download/v${latest.value}/CrMCDownload-${latest.value}-win64-setup.exe`,
);
</script>

<template>
  <div>
    <section class="page-head">
      <div class="wrap">
        <span class="eyebrow">DOWNLOAD · v{{ latest }}</span>
        <h1>下载 CrMcDownload</h1>
        <p>
          两个下载按钮，同一份安装包。Mirror 是自建镜像，适合 GitHub 直连抽风的时候；
          客户端内置测速与断点续传，下到一半断了也能接着来。
        </p>
      </div>
    </section>

    <section>
      <div class="wrap">
        <div class="release-card">
          <div class="release-top">
            <span class="release-version">v{{ latest }}</span>
            <span class="pill green">最新版本</span>
            <span class="pill">最低支持 {{ min }}</span>
            <span class="pill">{{ online ? "镜像在线" : "镜像接口暂不可达" }}</span>
          </div>
          <p class="muted" style="margin: 12px 0 0">
            Windows 10 / 11 · 64 位 · 安装包由 GitHub Releases 发布，镜像提供可断点续传的加速下载。
          </p>
          <div class="release-downloads">
            <a class="btn primary" :href="directUrl">GitHub 直连下载</a>
            <a class="btn" :href="mirrorUrl">镜像下载（推荐国内）</a>
            <a class="btn ghost" :href="releaseUrl" target="_blank" rel="noreferrer">查看发布页</a>
          </div>
          <div class="release-note">
            小提示：客户端的「设置 → 更新」会自动先测 GitHub 延迟，过高时清理 DNS 缓存重测，
            仍然偏高才切换到镜像，整个下载过程支持断点续传喵。
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="wrap">
        <div class="section-head">
          <div>
            <div class="section-tag">GET STARTED</div>
            <h2>三步开始玩</h2>
          </div>
        </div>
        <div class="steps">
          <div class="step">
            <h3>下载安装包</h3>
            <p>点击上面的按钮，拿到 <code>CrMCDownload-{{ latest }}-win64-setup.exe</code>。</p>
          </div>
          <div class="step">
            <h3>安装并登录</h3>
            <p>一路下一步即可。支持正版账号登录，也可以在离线模式里直接开玩。</p>
          </div>
          <div class="step">
            <h3>挑版本，出发</h3>
            <p>选一个游戏版本或整合包，剩下的依赖和资源下载交给镜像。</p>
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="wrap">
        <div class="section-head">
          <div>
            <div class="section-tag">REQUIREMENTS</div>
            <h2>运行环境</h2>
          </div>
        </div>
        <table class="spec-table">
          <tbody>
            <tr><th>操作系统</th><td>Windows 10 / 11（64 位）</td></tr>
            <tr><th>运行依赖</th><td>无需额外安装运行库，安装包自带 Python 运行环境</td></tr>
            <tr><th>磁盘空间</th><td>启动器约 80 MB；每个游戏版本另需数百 MB 到数 GB</td></tr>
            <tr><th>网络</th><td>可访问 GitHub 或镜像服务；镜像地址可在设置里修改</td></tr>
            <tr><th>账号</th><td>Microsoft 正版账号，或离线模式</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="section">
      <div class="wrap">
        <div class="band">
          <div>
            <h2>装好了？去看看 v{{ latest }} 改了什么</h2>
            <p>官网、更新路由、自动选源、强制更新……都写进更新日志了。</p>
          </div>
          <RouterLink class="btn" to="/changelog">查看更新日志</RouterLink>
        </div>
      </div>
    </section>
  </div>
</template>
