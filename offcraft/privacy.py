"""CrMCDownload 隐私政策正文。

客户端「设置 → 关于 → 隐私政策」与官网 /page/privacy 使用同一口径；
修改本文件时请同步 ``server/page/src/views/Privacy.vue``。
"""

from __future__ import annotations

PRIVACY_VERSION = "0.8.0"
PRIVACY_UPDATED = "2026-11-08"
CONTACT_EMAIL = "3890320020@qq.com"
SITE_PRIVACY_URL = "https://mc.yjlt.top/page/privacy"

#: (小节标题, 段落/条目列表)；以「- 」开头的条目在界面里渲染成列表项
SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "适用范围",
        (
            "本政策适用于 CrMCDownload 启动器（下称「客户端」）以及镜像站 mc.yjlt.top。"
            "客户端是自建镜像启动器，不含账号体系、广告、统计或用户画像 SDK。",
        ),
    ),
    (
        "默认不收集、不上传的信息",
        (
            "- 你添加或收藏的服务器地址、备注与最近访问记录，只保存在本机，不会主动上传；",
            "- 游戏实例、存档、配置、聊天记录、截图与本地文件，客户端不会读取或上传；",
            "- 不使用设备指纹，不采集剪贴板、通讯录、位置等与启动器功能无关的信息。",
        ),
    ),
    (
        "仅在本机保存的数据",
        (
            "以下数据存放在客户端数据目录，可在「设置 → 打开数据目录」查看：",
            "- 启动器设置、账户名、皮肤缓存；",
            "- 正版（Microsoft）登录令牌：只写入本机 data 目录的 account.json，退出登录即删除；",
            "- 服务器收藏、最近访问与本地联机记录；",
            "- 下载缓存、安装日志与崩溃日志。",
            "清空收藏、清除缓存或卸载客户端即可删除上述数据；卸载时会一并清理安装目录。",
        ),
    ),
    (
        "网络请求与必要的服务端信息",
        (
            "下载游戏文件、检查更新、查询服务器榜单、上传皮肤时，客户端会向镜像站或第三方服务发起请求。"
            "为完成请求、限流与排查故障，服务端可能记录 IP 地址、User-Agent、客户端版本、请求路径与时间；"
            "这些记录不用于广告或用户画像。",
        ),
    ),
    (
        "只有你主动上报或举报时才发送的信息",
        (
            "客户端不会自动上报你添加的服务器。只有你在服务器卡片上点击「上报」或「举报」并确认后，"
            "才会发送以下信息：",
            "- 服务器地址（主机与端口）与可选的备注；",
            "- 举报原因、说明，以及你可选填的联系方式；",
            "- 客户端版本（用于定位问题）。",
            "举报不会公开你的身份，联系方式仅在必要时用于回访，不会展示在公共榜单。"
            "若你不上报、不举报，你添加的服务器不会离开本机。",
        ),
    ),
    (
        "第三方服务",
        (
            "客户端会按需访问以下第三方服务，它们各自遵循其隐私政策：",
            "- GitHub：客户端更新包与部分资源；",
            "- Mojang / Microsoft：游戏版本清单与正版登录；",
            "- Modrinth：模组、整合包、资源包与光影的元数据；",
            "- 镜像站 mc.yjlt.top：下载加速、皮肤与服务器榜单。",
            "客户端只发送完成上述请求所必需的信息。",
        ),
    ),
    (
        "用途与共享",
        (
            "收集的信息仅用于完成下载与联机功能、审核举报、限流与安全防护。"
            "我们不出售、不出租，也不会向无关第三方共享这些信息；法律法规另有要求的除外。",
        ),
    ),
    (
        "存储与保留",
        (
            "举报记录保留至处理完毕后 180 天，用于申诉复核；服务端访问日志（如启用）保留不超过 30 天。"
            "超过期限后删除或匿名化处理。",
        ),
    ),
    (
        "你的权利",
        (
            "你可以随时查看或删除本机数据；如需查询或删除你提交过的举报记录，"
            f"请使用提交时的联系方式发送邮件至 {CONTACT_EMAIL}，并注明提交时间与服务器地址。",
        ),
    ),
    (
        "未成年人",
        (
            "未满 14 周岁的用户请在监护人陪同与同意下使用客户端。客户端不主动收集年龄信息。",
        ),
    ),
    (
        "变更与联系方式",
        (
            "本政策更新时会在客户端「关于」页与官网提示；涉及重大变更时会再次征得你的同意。",
            f"隐私相关问题请联系：{CONTACT_EMAIL}",
        ),
    ),
)


def policy_text() -> str:
    """纯文本版本（用于复制或日志）。"""
    lines = [f"CrMCDownload 隐私政策", f"更新日期：{PRIVACY_UPDATED} · 适用版本：{PRIVACY_VERSION}", ""]
    for title, paragraphs in SECTIONS:
        lines.append(title)
        lines.extend(paragraphs)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def policy_html() -> str:
    """富文本版本（QTextBrowser 直接展示）。"""
    parts = [
        f"<h2>{_escape('CrMCDownload 隐私政策')}</h2>",
        f"<p><span style='color:#5B6678'>更新日期：{PRIVACY_UPDATED} · 适用版本：{PRIVACY_VERSION}</span></p>",
    ]
    for title, paragraphs in SECTIONS:
        parts.append(f"<h3>{_escape(title)}</h3>")
        items: list[str] = []
        for paragraph in paragraphs:
            if paragraph.startswith("- "):
                items.append(f"<li>{_escape(paragraph[2:])}</li>")
                continue
            if items:
                parts.append("<ul>" + "".join(items) + "</ul>")
                items = []
            parts.append(f"<p>{_escape(paragraph)}</p>")
        if items:
            parts.append("<ul>" + "".join(items) + "</ul>")
    return "\n".join(parts)
