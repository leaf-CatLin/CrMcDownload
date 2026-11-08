<script setup lang="ts">
import { RouterLink } from "vue-router";
import { latestVersion } from "@/api/client";

const github = "https://github.com/leaf-CatLin/CrMcDownload";
const releases = "https://github.com/leaf-CatLin/CrMcDownload/releases";
const morphicons = "https://github.com/guillermolg00/morphicons";
</script>

<template>
  <footer class="site-footer">
    <div class="wrap">
      <div class="footer-grid">
        <div>
          <h4>CrMcDownload</h4>
          <p>一只不太挑食的 Minecraft 启动器，自带镜像、断点续传和一点点猫猫能量。</p>
          <p>当前版本 v{{ latestVersion }} · MIT License</p>
        </div>
        <div>
          <h4>开始使用</h4>
          <RouterLink to="/download">下载客户端</RouterLink>
          <RouterLink to="/changelog">更新日志</RouterLink>
          <RouterLink to="/status">服务状态</RouterLink>
        </div>
        <div>
          <h4>开源</h4>
          <a :href="github" target="_blank" rel="noreferrer">GitHub 仓库</a>
          <a :href="releases" target="_blank" rel="noreferrer">Releases</a>
          <a :href="`${github}#readme`" target="_blank" rel="noreferrer">项目介绍</a>
        </div>
        <div>
          <h4>致谢</h4>
          <a :href="morphicons" target="_blank" rel="noreferrer">Morphicons（MIT）</a>
          <p>图标形变动效</p>
        </div>
      </div>
      <div class="footer-bottom">
        <span>
          © 2026 CrMcDownload · 与 Mojang / Microsoft 无关 ·
          <RouterLink class="footer-legal" to="/privacy">隐私政策</RouterLink>
        </span>
        <span>今天也要开心地开服喵～</span>
      </div>
    </div>
  </footer>
</template>

<style scoped>
.footer-legal {
  color: rgba(239, 231, 212, 0.78);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.footer-legal:hover {
  color: #fff;
}
</style>
