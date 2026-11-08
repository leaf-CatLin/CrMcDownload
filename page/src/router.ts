import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";
import DashboardLayout from "@/layouts/DashboardLayout.vue";

const routes: RouteRecordRaw[] = [
  {
    path: "/",
    name: "home",
    component: () => import("@/views/site/Home.vue"),
    meta: { title: "首页" },
  },
  {
    path: "/download",
    name: "site-download",
    component: () => import("@/views/site/DownloadPage.vue"),
    meta: { title: "下载" },
  },
  {
    path: "/changelog",
    name: "changelog",
    component: () => import("@/views/site/Changelog.vue"),
    meta: { title: "更新日志" },
  },
  {
    path: "/about",
    name: "about",
    component: () => import("@/views/site/About.vue"),
    meta: { title: "关于" },
  },
  {
    path: "/privacy",
    name: "privacy",
    component: () => import("@/views/Privacy.vue"),
    meta: { title: "隐私政策" },
  },
  {
    // 运维看板：保留原有页面，挂在 /status 下，和官网共用同一份构建产物
    path: "/status",
    component: DashboardLayout,
    children: [
      {
        path: "",
        name: "status-overview",
        component: () => import("@/views/Overview.vue"),
        meta: { title: "概览" },
      },
      {
        path: "cache",
        name: "status-cache",
        component: () => import("@/views/Cache.vue"),
        meta: { title: "缓存" },
      },
      {
        path: "hot",
        name: "status-hot",
        component: () => import("@/views/Download.vue"),
        meta: { title: "下载热度" },
      },
      {
        path: "online",
        name: "status-online",
        component: () => import("@/views/Online.vue"),
        meta: { title: "联机" },
      },
      {
        path: "skins",
        name: "status-skins",
        component: () => import("@/views/Skins.vue"),
        meta: { title: "皮肤" },
      },
    ],
  },
  { path: "/:pathMatch(.*)*", redirect: "/" },
];

// base 与服务端 /page 前缀一致；构建产物部署在 /page/ 下时路由才不会丢前缀。
export const router = createRouter({
  history: createWebHistory("/page/"),
  routes,
});

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) || "首页";
  document.title = `${title} · CrMcDownload`;
});
