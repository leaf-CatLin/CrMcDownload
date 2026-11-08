<script setup lang="ts">
import { ref } from "vue";
import { RouterLink } from "vue-router";
import { MorphIcon } from "morphicons/vue";
import { latestVersion } from "@/api/client";

const MENU = "M4 7h16M4 12h16M4 17h16";
const CLOSE = "M6 6l12 12M18 6L6 18";
const GITHUB = "https://github.com/leaf-CatLin/CrMcDownload";

const links = [
  { to: "/", label: "首页" },
  { to: "/download", label: "下载" },
  { to: "/changelog", label: "更新日志" },
  { to: "/about", label: "关于" },
  { to: "/status", label: "服务状态" },
];

const open = ref(false);
</script>

<template>
  <header class="site-header">
    <div class="wrap">
      <RouterLink class="logo" to="/" @click="open = false">
        <span class="logo-mark">
          <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true">
            <rect x="4" y="5" width="5" height="5" fill="#2f7a34" />
            <rect x="10" y="5" width="5" height="5" fill="#1f4d22" />
            <rect x="16" y="5" width="4" height="5" fill="#2f7a34" />
            <rect x="7" y="13" width="10" height="6" fill="#1f4d22" />
          </svg>
        </span>
        CrMcDownload
      </RouterLink>

      <nav class="site-nav">
        <RouterLink v-for="link in links" :key="link.to" :to="link.to">
          {{ link.label }}
        </RouterLink>
      </nav>

      <div class="header-actions">
        <a class="icon-link" :href="GITHUB" target="_blank" rel="noreferrer" aria-label="GitHub">
          <svg width="19" height="19" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M12 2a10 10 0 0 0-3.16 19.49c.5.09.68-.22.68-.48v-1.7c-2.78.6-3.37-1.34-3.37-1.34-.45-1.16-1.11-1.47-1.11-1.47-.9-.62.07-.6.07-.6 1 .07 1.53 1.03 1.53 1.03.89 1.52 2.34 1.08 2.91.83.09-.65.35-1.08.63-1.33-2.22-.25-4.55-1.11-4.55-4.94 0-1.09.39-1.98 1.03-2.68-.1-.25-.45-1.26.1-2.63 0 0 .84-.27 2.75 1.02a9.5 9.5 0 0 1 5 0c1.91-1.29 2.75-1.02 2.75-1.02.55 1.37.2 2.38.1 2.63.64.7 1.03 1.59 1.03 2.68 0 3.84-2.34 4.68-4.57 4.93.36.31.68.92.68 1.85v2.74c0 .27.18.58.69.48A10 10 0 0 0 12 2Z" />
          </svg>
        </a>
        <RouterLink class="btn primary small" to="/download">
          下载 v{{ latestVersion }}
        </RouterLink>
        <button class="menu-toggle" type="button" aria-label="切换导航" @click="open = !open">
          <MorphIcon :icon="open ? CLOSE : MENU" :size="18" :stroke-width="2" />
        </button>
      </div>
    </div>

    <nav class="mobile-nav" :class="{ open }">
      <div class="wrap">
        <RouterLink v-for="link in links" :key="link.to" :to="link.to" @click="open = false">
          {{ link.label }}
        </RouterLink>
      </div>
    </nav>
  </header>
</template>
