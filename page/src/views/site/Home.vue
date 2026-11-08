<script setup lang="ts">
import { onMounted, ref } from "vue";
import { RouterLink } from "vue-router";
import { api, latestVersion, loadLatestVersion, humanBytes, type Metrics, type ServersOverview } from "@/api/client";

const metrics = ref<Metrics>({});
const servers = ref<ServersOverview>({});
const cacheBytes = ref<number | undefined>();

onMounted(async () => {
  void loadLatestVersion();
  try {
    const overview = await api.overview();
    metrics.value = overview.metrics || {};
    servers.value = overview.servers || {};
  } catch {
    /* 服务不可达时保留占位符 */
  }
  try {
    const cache = await api.cache();
    cacheBytes.value = cache.bytes;
  } catch {
    /* 同上 */
  }
});

const hitRate = () => {
  const hits = metrics.value.hits || 0;
  const misses = metrics.value.misses || 0;
  return hits + misses ? `${Math.round((hits / (hits + misses)) * 100)}%` : "—";
};

const features = [
  {
    title: "镜像加速",
    desc: "Mojang、Fabric、Forge、Modrinth 统统走自建镜像，下载不用再看运气。",
    bg: "#e6f6df",
    path: "M4 15a2 2 0 0 1 2-2h8l2 3h4a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8Zm10 1v5m0 0-2-2m2 2 2-2",
  },
  {
    title: "断点续传",
    desc: "网断了、源抽风了都没关系，小猫咪会记住你走到哪儿，接着下。",
    bg: "#e8f6ff",
    path: "M21 12a9 9 0 1 1-3-6.7M21 4v5h-5",
  },
  {
    title: "一键安装",
    desc: "挑版本、装加载器、补依赖，一条流程走完，剩下去喝口水。",
    bg: "#ffe6ee",
    path: "M12 3 3 7.5v9L12 21l9-4.5v-9L12 3Zm0 0v9m9-4.5L12 12 3 7.5",
  },
  {
    title: "内容库",
    desc: "模组、整合包、资源包、光影，Modrinth 里有什么就能装什么。",
    bg: "#fff3d6",
    path: "M4 4h7v7H4V4Zm9 0h7v7h-7V4ZM4 13h7v7H4v-7Zm9 0h7v7h-7v-7Z",
  },
  {
    title: "联机小站",
    desc: "看看哪台服务器人最多、延迟最低，热乎的榜单直接排在首页。",
    bg: "#e6f6df",
    path: "M16 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M10 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm12 10v-2a4 4 0 0 0-3-3.87",
  },
  {
    title: "皮肤与背景",
    desc: "PNG、GIF、纯色都能当底图，客户端也要有自己的小脾气。",
    bg: "#e8f6ff",
    path: "M12 3a9 9 0 1 0 0 18c1.1 0 2-.9 2-2 0-.5-.2-.9-.5-1.3-.3-.4-.5-.8-.5-1.2 0-1 .8-1.8 1.8-1.8H17a4 4 0 0 0 4-4c0-4-4-7.7-9-7.7Z",
  },
];

const faqs = [
  {
    q: "第一次用应该点哪里？",
    a: "去下载页拿最新版安装包，装好后在正版账号或离线模式里选个版本，点开始就行。镜像会自动帮你把该下的文件搬过来喵。",
  },
  {
    q: "下载老是卡住怎么办？",
    a: "客户端会自动测速并选择更快的源：先试 GitHub 直连，延迟太高就清理 DNS 缓存重测，仍然偏高就切到镜像。中断的文件会保留在本地，下次从断点继续。",
  },
  {
    q: "版本太低会被拒绝使用吗？",
    a: "会的。镜像服务可以配置「最低支持版本」，低于这个版本的客户端会被返回 426 并提示强制更新，避免旧版本拉取到不兼容的资源。",
  },
  {
    q: "这个项目跟 Mojang 有关系吗？",
    a: "没有哦。CrMcDownload 是社区自建的启动器与镜像服务，只代理公开的元数据和文件，和 Mojang / Microsoft 没有任何隶属关系。",
  },
];
</script>

<template>
  <div>
    <section class="hero">
      <div class="wrap">
        <div>
          <span class="eyebrow">MINECRAFT 启动器 · v{{ latestVersion }} 已上线</span>
          <h1>
            把 Minecraft 装进背包，<br />
            <span class="mark">一键出发喵！</span>
          </h1>
          <p class="lead">
            CrMcDownload 是一只不太挑食的小猫咪：原版、Fabric、Forge、NeoForge、整合包、皮肤、光影……
            统统帮你搬回家。自带镜像加速与断点续传，线路抽风的时候它会自己换条路。
          </p>
          <div class="hero-actions">
            <RouterLink class="btn primary" to="/download">下载 v{{ latestVersion }}</RouterLink>
            <RouterLink class="btn" to="/changelog">看看更新了什么</RouterLink>
          </div>
          <div class="hero-meta">
            <span>Windows 10 / 11</span>
            <span>开源 MIT</span>
            <span>镜像 + 断点续传</span>
            <span>不收集隐私</span>
          </div>
        </div>

        <div class="hero-art" aria-hidden="true">
          <span class="art-tag">今日份猫猫能量</span>
          <svg viewBox="0 0 520 380" role="img">
            <circle cx="440" cy="58" r="26" fill="#FFC94D" stroke="#26231c" stroke-width="3" />
            <g fill="#ffffff" stroke="#26231c" stroke-width="3">
              <rect x="28" y="48" width="92" height="26" rx="13" />
              <rect x="52" y="34" width="48" height="22" rx="11" />
              <rect x="350" y="146" width="70" height="22" rx="11" opacity="0.85" />
            </g>

            <ellipse cx="260" cy="356" rx="126" ry="15" fill="rgba(38,35,28,0.14)" />

            <polygon points="110,232 260,186 410,232 260,278" fill="#5BC35A" stroke="#26231c" stroke-width="3" stroke-linejoin="round" />
            <polygon points="110,232 260,278 260,350 110,304" fill="#8C6239" stroke="#26231c" stroke-width="3" stroke-linejoin="round" />
            <polygon points="410,232 260,278 260,350 410,304" fill="#7A522F" stroke="#26231c" stroke-width="3" stroke-linejoin="round" />
            <polygon points="110,232 260,278 260,294 110,248" fill="#4FAE45" />
            <polygon points="410,232 260,278 260,294 410,248" fill="#459A3D" />
            <polygon points="306,292 318,295 318,307 306,304" fill="#1F4D22" />
            <polygon points="338,300 350,303 350,315 338,312" fill="#1F4D22" />
            <polygon points="322,302 334,305 334,320 322,317" fill="#1F4D22" />
            <polygon points="334,309 346,312 346,319 334,316" fill="#1F4D22" />
            <polygon points="310,311 322,314 322,321 310,318" fill="#1F4D22" />

            <path d="M246 206q-26-6-24-30" fill="none" stroke="#26231c" stroke-width="3" stroke-linecap="round" />
            <rect x="236" y="182" width="66" height="46" rx="20" fill="#FFF9F0" stroke="#26231c" stroke-width="3" />
            <circle cx="270" cy="166" r="28" fill="#FFF9F0" stroke="#26231c" stroke-width="3" />
            <polygon points="250,148 256,118 270,138" fill="#FFF9F0" stroke="#26231c" stroke-width="3" stroke-linejoin="round" />
            <polygon points="292,148 286,118 272,138" fill="#FFF9F0" stroke="#26231c" stroke-width="3" stroke-linejoin="round" />
            <circle cx="261" cy="164" r="3.2" fill="#26231c" />
            <circle cx="280" cy="164" r="3.2" fill="#26231c" />
            <path d="M266 174q4 4 8 0" fill="none" stroke="#26231c" stroke-width="2" stroke-linecap="round" />
            <circle cx="252" cy="172" r="4" fill="#FF8FB1" opacity="0.75" />
            <circle cx="289" cy="172" r="4" fill="#FF8FB1" opacity="0.75" />
            <path d="M248 166h-14M248 172h-12M292 166h14M292 172h12" stroke="#26231c" stroke-width="1.6" stroke-linecap="round" opacity="0.5" />

            <rect x="336" y="86" width="72" height="34" rx="17" fill="#ffffff" stroke="#26231c" stroke-width="2.5" />
            <polygon points="352,118 348,132 366,118" fill="#ffffff" stroke="#26231c" stroke-width="2.5" stroke-linejoin="round" />
            <text x="372" y="108" text-anchor="middle" font-size="14" font-weight="800" fill="#26231c">喵～</text>

            <path d="M62 116c0-3 2-5 5-5s5 2 5 5c0 4-5 8-5 8s-5-4-5-8Z" fill="#FF8FB1" />
            <path d="M92 88c0-2 1.5-4 4-4s4 2 4 4c0 3-4 6-4 6s-4-3-4-6Z" fill="#FF8FB1" opacity="0.8" />
            <g stroke="#FFC94D" stroke-width="3" stroke-linecap="round">
              <path d="M462 150v14M455 157h14" />
              <path d="M486 196v9M482 200h9" />
            </g>
          </svg>
        </div>
      </div>
    </section>

    <div class="ticker" aria-hidden="true">
      <div class="ticker-track">
        <span v-for="n in 2" :key="n">
          原版 · FABRIC · FORGE · NEOFORGE · MODRINTH · 镜像缓存 · 断点续传 · 皮肤自定义 · 联机热度榜 · 开源 MIT ·
          原版 · FABRIC · FORGE · NEOFORGE · MODRINTH · 镜像缓存 · 断点续传 · 皮肤自定义 · 联机热度榜 · 开源 MIT ·
        </span>
      </div>
    </div>

    <section class="stats">
      <div class="wrap">
        <div class="stats-grid">
          <div class="stat-sticker">
            <div class="k">缓存命中率</div>
            <div class="v">{{ hitRate() }}</div>
          </div>
          <div class="stat-sticker">
            <div class="k">已缓存体积</div>
            <div class="v">{{ cacheBytes === undefined ? "—" : humanBytes(cacheBytes) }}</div>
          </div>
          <div class="stat-sticker">
            <div class="k">联机服务器</div>
            <div class="v">{{ servers.online_servers ?? servers.servers ?? "—" }}</div>
          </div>
          <div class="stat-sticker">
            <div class="k">在线玩家</div>
            <div class="v">{{ servers.players ?? "—" }}</div>
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="wrap">
        <div class="section-head">
          <div>
            <div class="section-tag">FEATURES</div>
            <h2>小猫咪能帮你做什么</h2>
            <p>该快的快，该稳的稳，该可爱的也可爱。</p>
          </div>
        </div>
        <div class="cards">
          <article v-for="item in features" :key="item.title" class="feature">
            <span class="icon" :style="{ background: item.bg }">
              <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#26231c" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path :d="item.path" />
              </svg>
            </span>
            <h3>{{ item.title }}</h3>
            <p>{{ item.desc }}</p>
          </article>
        </div>

        <div class="chips">
          <span class="chip on">原版</span>
          <span class="chip on">Fabric</span>
          <span class="chip on">Forge</span>
          <span class="chip on">NeoForge</span>
          <span class="chip on">Modrinth</span>
          <span class="chip">整合包</span>
          <span class="chip">资源包</span>
          <span class="chip">光影</span>
          <span class="chip">皮肤</span>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="wrap">
        <div class="band">
          <div>
            <h2>准备好了吗？就差你点这一下</h2>
            <p>v{{ latestVersion }} · Windows 安装包 · 装完就能看到熟悉的方块世界。</p>
          </div>
          <div class="hero-actions" style="margin: 0">
            <RouterLink class="btn primary" to="/download">去下载页</RouterLink>
            <a class="btn" href="https://github.com/leaf-CatLin/CrMcDownload/releases" target="_blank" rel="noreferrer">GitHub Releases</a>
          </div>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="wrap">
        <div class="section-head">
          <div>
            <div class="section-tag">FAQ</div>
            <h2>常见问题</h2>
            <p>遇到问题先摸摸猫，再翻翻这里。</p>
          </div>
        </div>
        <div class="faq">
          <details v-for="item in faqs" :key="item.q">
            <summary>{{ item.q }}</summary>
            <p>{{ item.a }}</p>
          </details>
        </div>
      </div>
    </section>
  </div>
</template>
