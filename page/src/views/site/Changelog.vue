<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { api, latestVersion, type ReleaseEntry } from "@/api/client";

// 更新日志不再写死在前端：直接渲染服务端从 GitHub Releases 自动检测到的发布。
// 服务端缓存 10 分钟，发新版本后官网自己就更新了，不用重新构建前端。
const releases = ref<ReleaseEntry[]>([]);
const loading = ref(true);
const failed = ref(false);
const checkedAt = ref<string>("");

const RELEASES_URL = "https://github.com/leaf-CatLin/CrMcDownload/releases";

onMounted(async () => {
  try {
    const payload = await api.releases();
    releases.value = payload.releases || [];
    if (payload.check?.fetched_at) {
      checkedAt.value = new Date(payload.check.fetched_at * 1000).toLocaleString();
    }
  } catch {
    failed.value = true;
  } finally {
    loading.value = false;
  }
});

const logs = computed(() =>
  releases.value.map((release) => ({
    version: (release.version || release.tag || "").replace(/^v/i, ""),
    tag: release.tag,
    date: (release.published_at || "").slice(0, 10),
    url: release.url || `${RELEASES_URL}/tag/${release.tag}`,
    channel: release.channel || "stable",
    items: markdownItems(release.body || ""),
  })),
);

/** 把 GitHub Release 的 Markdown 正文拆成「一条一句」的列表，便于排版。 */
function markdownItems(body: string): string[] {
  const out: string[] = [];
  for (const raw of body.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || line.startsWith("|") || line.startsWith("```")) continue;
    // 跳过纯分隔线、校验和表格残留与徽章图片
    if (/^([-*_=]\s*){2,}$/.test(line) || line.startsWith("![")) continue;
    const cleaned = line
      .replace(/^[-*+]\s+/, "")
      .replace(/^\[[ x]\]\s+/i, "")
      .replace(/\*\*(.+?)\*\*/g, "$1")
      .replace(/`([^`]+)`/g, "$1")
      .trim();
    if (cleaned.length < 2) continue;
    out.push(cleaned);
    // 单个版本最多列 14 条，太长就把人吓跑了
    if (out.length >= 14) break;
  }
  return out;
}
</script>

<template>
  <div>
    <section class="page-head">
      <div class="wrap">
        <span class="eyebrow">CHANGELOG · 当前 v{{ latestVersion }}</span>
        <h1>更新日志</h1>
        <p>直接读 GitHub Releases，发一版这里就多一条，不再手抄喵。</p>
      </div>
    </section>

    <section class="section" style="padding-top: 8px">
      <div class="wrap">
        <p v-if="loading" class="muted">正在读取版本历史…</p>
        <p v-else-if="failed" class="muted">
          版本历史暂时读不到。<a :href="RELEASES_URL" target="_blank" rel="noreferrer">去 GitHub 看</a>。
        </p>
        <p v-else-if="!logs.length" class="muted">
          仓库还没有发布记录。<a :href="RELEASES_URL" target="_blank" rel="noreferrer">去 GitHub 看</a>。
        </p>

        <div v-else class="changelog">
          <article v-for="log in logs" :key="log.tag" class="log-item">
            <div class="log-head">
              <span class="log-version">v{{ log.version }}</span>
              <span class="log-date">{{ log.date }}</span>
              <span v-if="log.version === latestVersion" class="pill green">当前版本</span>
              <span v-else-if="log.channel === 'beta'" class="pill">预发布</span>
              <a class="log-link" :href="log.url" target="_blank" rel="noreferrer">发布页</a>
            </div>
            <ul>
              <li v-for="item in log.items" :key="item">{{ item }}</li>
            </ul>
          </article>
        </div>

        <p v-if="checkedAt" class="muted" style="margin-top: 18px; font-size: 13px">
          版本历史由服务端自动检测并缓存（最近检测：{{ checkedAt }}）。
        </p>
      </div>
    </section>

    <section class="section" style="padding-top: 0">
      <div class="wrap">
        <div class="band">
          <div>
            <h2>想追最新的开发进度？</h2>
            <p>仓库、提交记录、Release 都在 GitHub 上，欢迎来玩。</p>
          </div>
          <a class="btn" :href="RELEASES_URL" target="_blank" rel="noreferrer">去 GitHub</a>
        </div>
      </div>
    </section>
  </div>
</template>