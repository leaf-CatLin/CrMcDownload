<script setup lang="ts">
// 正文口径与客户端 user/offcraft/privacy.py 保持一致，修改时请同步两处。
const POLICY_UPDATED = "2026-11-08";
const POLICY_VERSION = "0.8.0";
const CONTACT_EMAIL = "3890320020@qq.com";

interface PolicySection {
  id: string;
  title: string;
  body?: string[];
  items?: string[];
  tail?: string[];
}

const sections: PolicySection[] = [
  {
    id: "scope",
    title: "适用范围",
    body: [
      "本政策适用于 CrMCDownload 启动器（下称「客户端」）以及镜像站 mc.yjlt.top。",
      "客户端是自建镜像启动器，不含账号体系，也没有广告、统计或用户画像 SDK。",
    ],
  },
  {
    id: "default",
    title: "默认不收集、不上传的信息",
    body: ["除非你主动操作，客户端默认不会把数据发往任何服务器。"],
    items: [
      "你添加或收藏的服务器地址、备注与最近访问记录，只保存在本机，不会主动上传；",
      "游戏实例、存档、配置、聊天记录、截图与本地文件，客户端不会读取或上传；",
      "不使用设备指纹，不采集剪贴板、通讯录、位置等与启动器功能无关的信息。",
    ],
  },
  {
    id: "local-data",
    title: "仅在本机保存的数据",
    body: ["以下数据存放在客户端数据目录，可随时在「设置 → 打开数据目录」查看："],
    items: [
      "启动器设置、账户名、皮肤缓存；",
      "正版（Microsoft）登录令牌：只写入本机 data 目录的 account.json，退出登录即删除；",
      "服务器收藏、最近访问与本地联机记录；",
      "下载缓存、安装日志与崩溃日志。",
    ],
    tail: ["清空收藏、清除缓存或卸载客户端即可删除上述数据；卸载时会一并清理安装目录。"],
  },
  {
    id: "requests",
    title: "网络请求与必要的服务端信息",
    body: [
      "下载游戏文件、检查更新、查询服务器榜单、上传皮肤时，客户端会向镜像站或第三方服务发起请求。",
      "为完成请求、进行限流与排查故障，服务端可能记录 IP 地址、User-Agent、客户端版本、请求路径与时间；这些记录不用于广告或用户画像。",
    ],
  },
  {
    id: "report",
    title: "只有你主动上报或举报时才发送的信息",
    body: [
      "客户端不会自动上报你添加的服务器。只有你在服务器卡片上点击「上报」或「举报」并确认后，才会发送以下信息：",
    ],
    items: [
      "服务器地址（主机与端口），以及你可选填的备注；",
      "举报原因、说明，以及你可选填的联系方式；",
      "客户端版本（用于定位问题）。",
    ],
    tail: [
      "举报不会公开你的身份，联系方式仅在必要时用于回访，不会展示在公共榜单。",
      "若你不上报、不举报，你添加的服务器不会离开本机。",
    ],
  },
  {
    id: "third-party",
    title: "第三方服务",
    body: ["客户端会按需访问以下第三方服务，它们各自遵循其隐私政策："],
    items: [
      "GitHub：客户端更新包与部分资源；",
      "Mojang / Microsoft：游戏版本清单与正版登录；",
      "Modrinth：模组、整合包、资源包与光影的元数据；",
      "镜像站 mc.yjlt.top：下载加速、皮肤与服务器榜单。",
    ],
    tail: ["客户端只发送完成上述请求所必需的信息。"],
  },
  {
    id: "purpose",
    title: "用途与共享",
    body: [
      "收集的信息仅用于完成下载与联机功能、审核举报、限流与安全防护。",
      "我们不出售、不出租，也不会向无关第三方共享这些信息；法律法规另有要求的除外。",
    ],
  },
  {
    id: "retention",
    title: "存储与保留",
    body: [
      "举报记录保留至处理完毕后 180 天，用于申诉复核；服务端访问日志（如启用）保留不超过 30 天。",
      "超过期限后删除或匿名化处理。",
    ],
  },
  {
    id: "rights",
    title: "你的权利",
    body: [
      "你可以随时查看或删除本机数据，例如清空收藏、清除缓存。",
      `如需查询或删除你提交过的举报记录，请使用提交时的联系方式发送邮件至 ${CONTACT_EMAIL}，并注明提交时间与服务器地址。`,
    ],
  },
  {
    id: "minors",
    title: "未成年人",
    body: [
      "未满 14 周岁的用户请在监护人陪同与同意下使用客户端。",
      "客户端不主动收集年龄信息。",
    ],
  },
  {
    id: "changes",
    title: "变更与联系方式",
    body: [
      "本政策更新时会在客户端「关于」页与官网提示；涉及重大变更时会再次征得你的同意。",
      `隐私相关问题请联系：${CONTACT_EMAIL}`,
    ],
  },
];
</script>

<template>
  <div>
    <section class="page-head">
      <div class="wrap">
        <span class="eyebrow">PRIVACY · v{{ POLICY_VERSION }}</span>
        <h1>隐私政策</h1>
        <p>CrMCDownload 客户端与镜像站 mc.yjlt.top 共用同一份口径，这里把每一处数据流向讲清楚。</p>
      </div>
    </section>

    <section class="section" style="padding-top: 8px">
      <div class="wrap">
        <div class="privacy">
          <div class="privacy-meta">
            更新日期：{{ POLICY_UPDATED }} · 适用版本：{{ POLICY_VERSION }}
          </div>

          <div class="privacy-summary">
            一句话版本：你添加的服务器只留在本机；只有你亲手点下「上报」或「举报」并确认，
            数据才会离开你的电脑。
          </div>

          <article v-for="(section, index) in sections" :id="section.id" :key="section.id" class="policy-block">
            <h2>
              <span class="num">{{ String(index + 1).padStart(2, "0") }}</span>
              {{ section.title }}
            </h2>
            <p v-for="paragraph in section.body" :key="paragraph">{{ paragraph }}</p>
            <ul v-if="section.items">
              <li v-for="item in section.items" :key="item">{{ item }}</li>
            </ul>
            <p v-for="paragraph in section.tail" :key="paragraph">{{ paragraph }}</p>
          </article>

          <div class="privacy-contact">
            对本政策有疑问，或想查询、删除你提交过的记录？写信到
            <a :href="`mailto:${CONTACT_EMAIL}`">{{ CONTACT_EMAIL }}</a> 即可。
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.privacy {
  max-width: 760px;
  margin: 0 auto;
  padding: 30px 34px 26px;
  border: 2px solid var(--ink);
  border-radius: 22px;
  background: var(--panel);
  box-shadow: 4px 4px 0 rgba(38, 35, 28, 0.12);
}

.privacy-meta {
  color: var(--muted);
  font-size: 13px;
  letter-spacing: 0.02em;
}

.privacy-summary {
  margin: 14px 0 26px;
  padding: 14px 18px;
  border-radius: 14px;
  border: 1.5px dashed var(--line-strong);
  background: var(--grass-soft);
  color: #3f5b37;
  font-weight: 700;
}

.policy-block + .policy-block {
  margin-top: 24px;
  padding-top: 24px;
  border-top: 1.5px dashed var(--line);
}

.policy-block h2 {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 0 0 10px;
  font-size: 18px;
  font-weight: 900;
  letter-spacing: -0.01em;
  line-height: 1.4;
}

.policy-block .num {
  flex: none;
  font-family: "Cascadia Code", "Consolas", monospace;
  font-size: 12px;
  font-weight: 800;
  color: var(--grass-deep);
}

.policy-block p {
  margin: 0 0 10px;
  color: var(--muted);
}

.policy-block ul {
  margin: 0 0 10px;
  padding-left: 22px;
  color: var(--muted);
}

.policy-block li {
  margin: 5px 0;
}

.policy-block p:last-child,
.policy-block ul:last-child {
  margin-bottom: 0;
}

.privacy-contact {
  margin-top: 26px;
  padding-top: 18px;
  border-top: 1.5px dashed var(--line);
  color: var(--muted);
  font-size: 14px;
}

.privacy-contact a {
  color: var(--grass-deep);
  font-weight: 800;
  text-decoration: underline;
  text-underline-offset: 3px;
}

@media (max-width: 720px) {
  .privacy {
    padding: 24px 20px 22px;
  }

  .policy-block h2 {
    font-size: 17px;
  }
}
</style>
