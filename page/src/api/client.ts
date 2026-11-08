// 看板与官网取数：全部走相对路径 /api/...、/health，由 vite 开发代理或服务端同源提供。
// 令牌不进前端（读接口免 token），因此这里不附带任何鉴权头。
// 每个请求都带 X-OffCraft-Version，版本过低时服务端会返回 426。

import { ref } from "vue";

export const SITE_VERSION = __APP_VERSION__;

//: 官网展示的版本号：初始为构建版本，加载 /api/update 后自动跟随线上最新版本
export const latestVersion = ref<string>(SITE_VERSION);
let latestVersionTask: Promise<string> | null = null;

/** 拉取一次线上最新版本；失败或接口缺失时保留构建版本。 */
export function loadLatestVersion(): Promise<string> {
  if (!latestVersionTask) {
    latestVersionTask = api
      .update()
      .then((payload: UpdatePayload) => {
        const value = String(payload.latest_version || "").trim().replace(/^v/i, "");
        if (value) latestVersion.value = value;
        return latestVersion.value;
      })
      .catch(() => latestVersion.value);
  }
  return latestVersionTask;
}

export interface UpdatePayload {
  ok?: boolean;
  client_version?: string;
  latest_version?: string;
  min_version?: string;
  force_update?: boolean;
  release_url?: string;
  download_url?: string;
  mirror_download_url?: string;
  asset?: { name?: string; size?: number | null };
  error?: string;
}


export interface Metrics {
  hits?: number;
  misses?: number;
  upstream_bytes?: number;
  errors?: number;
  uptime?: number;
}

export interface ServersOverview {
  servers?: number;
  online_servers?: number;
  players?: number;
}

export interface CacheStats {
  bytes?: number;
  max_bytes?: number;
  max_file_bytes?: number;
  evicted_bytes?: number;
  swept_bytes?: number;
}

export interface SkinStatus {
  count?: number;
  last_updated?: number;
}

export interface ServerListing {
  host: string;
  port: number;
  label: string;
  source: string;
  reports: number;
  ok: boolean;
  error: string;
  latency_ms: number | null;
  motd: string;
  version: string;
  online: number;
  max: number;
  players: Array<{ name: string; uuid?: string }>;
  players_visible: boolean;
  updated_at: number;
  hot: number;
  history?: Array<{ ts: number; latency_ms: number | null; online: number | null }>;
}

export interface Overview {
  metrics: Metrics;
  servers: ServersOverview;
}

export interface ServerListResponse {
  total: number;
  limit: number;
  offset: number;
  hits: ServerListing[];
}

export interface Health {
  status?: string;
  cache?: Record<string, unknown>;
}

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    headers: { Accept: "application/json", "X-OffCraft-Version": SITE_VERSION },
    signal,
  });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }
  return (await response.json()) as T;
}

export const api = {
  update: (signal?: AbortSignal) => getJSON<UpdatePayload>("/api/update", signal),
  health: (signal?: AbortSignal) => getJSON<Health>("/health", signal),
  overview: (signal?: AbortSignal) => getJSON<Overview>("/api/stats/overview", signal),
  cache: (signal?: AbortSignal) => getJSON<CacheStats>("/api/stats/cache", signal),
  download: (signal?: AbortSignal) => getJSON<{ metrics: Metrics }>("/api/stats/download", signal),
  servers: (signal?: AbortSignal) => getJSON<ServersOverview>("/api/stats/servers", signal),
  serverList: (q = "", limit = 30, signal?: AbortSignal) =>
    getJSON<ServerListResponse>(
      `/api/servers/list?q=${encodeURIComponent(q)}&limit=${limit}&offset=0`,
      signal,
    ),
  serverDetail: (host: string, port: number, signal?: AbortSignal) =>
    getJSON<ServerListing>(
      `/api/servers/detail?host=${encodeURIComponent(host)}&port=${port}`,
      signal,
    ),
  skins: (signal?: AbortSignal) => getJSON<SkinStatus>("/api/skins/status", signal),
};

export function humanBytes(value: number | undefined | null): string {
  const n = Number(value || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  let size = n;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${index === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

export function humanDuration(seconds: number | undefined | null): string {
  const s = Math.max(0, Math.floor(Number(seconds || 0)));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小时`;
  if (h > 0) return `${h} 小时 ${m} 分`;
  return `${m} 分 ${s % 60} 秒`;
}
