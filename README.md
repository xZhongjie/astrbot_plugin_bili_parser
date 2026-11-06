# astrbot_plugin_bili_parser

在 AstrBot 里自动解析 **B 站视频链接**：识别链接后抓取视频信息，发送「封面 + 标题 + UP主 + 播放/点赞/投币/收藏 + 简介 + 链接」的图文卡片。

为了绕开 B 站对服务器 IP 的风控（HTTP **412 Precondition Failed**），插件采用**多解析源**设计：

- 内置**第三方解析接口**（星之阁）——不直接请求 B 站 API
- 内置 **B 站官方 API**——自动生成 `buvid3` Cookie，遇到 412 自动换 Cookie 重试
- 支持**任意多个自定义第三方接口**，请求时**并行竞速**，取最先成功的结果
- 可配置**请求代理**，换出口 IP
- 可选下载视频，下载时按 **B 站 CDN 镜像 host** 轮换

> 灵感来自 [koishi-plugin-bilibili-videolink-analysis](https://github.com/koishi-shangxue-plugins/koishi-shangxue-apps/tree/main/plugins/bilibili-videolink-analysis) 的多请求模式设计，但实现与配置思路针对 AstrBot 重写，并加入了官方 API 的 412 处理与镜像下载。

---

## 功能

- 自动识别消息中的 B 站链接：
  - `https://www.bilibili.com/video/BV...`
  - `https://bilibili.com/video/av123456`
  - `https://b23.tv/xxxxxx`、`https://bili2233.cn/xxxxxx` 短链
  - `?p=2` 分 P
  - 单独发送的 `BV1xxxxxxxxx` 号
- 一条消息多个链接时按配置上限依次解析
- 卡片文本模板可自定义
- 封面会先下载再发送，避免平台防盗链导致裂图
- 可选下载视频并发送（有大小上限、清晰度选择、镜像 host 回退）
- 同链接冷却时间，避免刷屏与接口额度浪费
- `/bili` 手动解析、`/bili_status` 查看接口状态

---

## 安装

```bash
cd AstrBot/data/plugins
git clone https://github.com/xZhongjie/astrbot_plugin_bili_parser.git
```

在 WebUI「插件」页重载插件，或重启 AstrBot。

依赖：`aiohttp`（AstrBot 本体已自带）。

---

## 使用

### 自动解析

群里/私聊直接发 B 站链接即可，插件会自动回复卡片。默认开启。

### 手动解析

```
/bili https://www.bilibili.com/video/BV1uv411q7Mv
/bili BV1uv411q7Mv
/bili https://b23.tv/xxxxxx
```

### 查看状态

```
/bili_status
```

会列出每个解析接口的启用状态、类型和域名。

---

## 配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 插件总开关 |
| `auto_parse` | `true` | 自动解析聊天里的链接 |
| `only_when_at` | `false` | 仅被 @ / 唤醒时自动解析 |
| `suppress_llm` | `true` | 解析时阻止默认 LLM 回复，避免双回复 |
| `show_error` | `false` | 解析失败时是否提示失败原因 |
| `request_mode` | `parallel` | `parallel` 并行竞速 / `fallback` 顺序回退 |
| `timeout` | `12` | 单接口超时（秒） |
| `cookie` | 空 | 可选，填 `SESSDATA=...` 提高官方接口成功率 |
| `proxy` | 空 | 所有请求走代理，例如 `http://127.0.0.1:7890` |
| `force_ipv4` | `true` | 强制 IPv4，避免 IPv6 出网导致大文件被截断 |
| `cover_proxy` | weserv 图片代理 | 封面直连失败时，用第三方图片代理抓取并缩放；留空关闭 |
| `video_proxy` | 空 | 视频下载代理模板，例如 `https://proxy.example/?url={url}` |
| `video_proxy_first` | `false` | 直接使用第三方下载代理，跳过直连尝试 |
| `video_proxy_apis` | kukutool | 视频下载代理接口列表，多个接口并行测速选最快 |
| `video_proxy_api` | 空 | 旧版单接口，保留兼容；填了也参与测速 |
| `download_chunk_kb` | `2048` | Range 分片下载大小，`0` 关闭分片 |
| `send_cover` | `true` | 发送封面图 |
| `send_video` | `false` | 下载并发送视频 |
| `max_video_mb` | `30` | 视频大小上限，`0` 不限 |
| `video_send_mode` | `auto` | 视频发送方式：`auto` / `base64` / `file` |
| `video_base64_max_mb` | `20` | 不超过该大小时用 base64 发送，避免协议端找不到本地文件 |
| `video_quality` | `64` | 视频清晰度：`32`/`64`/`80` |
| `template` | 见默认 | 卡片文本模板 |
| `desc_max_chars` | `80` | 简介截断长度，`0` 不限 |
| `cooldown_seconds` | `60` | 同会话同链接冷却时间 |
| `max_links_per_message` | `3` | 单条消息最多解析链接数 |
| `providers` | 见默认 | 解析接口列表（可增删任意条） |

### 卡片模板变量

`{title}` `{up}` `{bvid}` `{aid}` `{page}` `{duration}` `{view}` `{like}` `{coin}` `{favorite}` `{danmaku}` `{desc}` `{url}` `{source}`

默认模板：

```
【{title}】
UP主：{up} · 时长：{duration}
播放 {view} · 点赞 {like} · 投币 {coin} · 收藏 {favorite}
{desc}
{url}
```

---

## 多解析接口与防 412

### 请求模式

- **并行竞速（默认）**：同时请求所有启用的接口，谁先返回可用结果就用谁，其余请求立即取消。
- **顺序回退**：按列表顺序逐个尝试，前一个失败才用下一个，适合只想给主接口减压的场景。

### 官方 API 的 412 处理

B 站官方 `api.bilibili.com` 对无 Cookie、异常 UA 的请求很容易返回 **412**。插件会：

1. 自动生成 `buvid3`（形如 `UUID + infoc`）和 `b_nut`，带上标准 `User-Agent` / `Referer`
2. 收到 412 时**自动更换 `buvid3` 并重试**，最多 3 次
3. 三次都失败后交给其它接口（并行模式下其实已经在同时跑了）
4. 配置了 `SESSDATA` 时会一并带上，成功率更高

### 视频下载的镜像 host

解析出的视频直链通常落在 `upos-sz-mirrorcos.bilivideo.com` 这类 CDN 上。下载失败时插件会自动把 host 换成其它镜像依次尝试：

```
upos-sz-mirrorcos / upos-sz-mirrorali / upos-sz-mirrorhw
upos-sz-mirror08c / upos-sz-mirrorbd / upos-hz-mirrorakam.akamaized.net
```

> 注意：镜像 host 解决的是「某条 CDN 线路失败」，**不是 412**。412 是 B 站 API 风控，靠的是 Cookie、UA、代理 IP 和多接口分流。

### 添加自己的第三方接口

在「解析接口列表」里新增一条，类型选「自定义 JSON 接口」，填写地址模板即可，例如：

```
https://your-parse-api.example.com/bili?url={url}
```

占位符：

| 占位符 | 含义 |
| --- | --- |
| `{url}` | 视频地址（已 URL 编码，推荐） |
| `{raw_url}` | 视频地址（未编码） |
| `{bvid}` | BV 号 |
| `{aid}` | av 号 |
| `{page}` | 分 P 序号 |

响应只要是 JSON 且包含视频信息就能识别，插件会自动在返回结构里寻找最像视频的对象，兼容：

- 星之阁：`{"code":0,"data":{"bvid":...,"video":{"title":...,"fm":...,"url":...},"owner":...,"stat":...}}`
- B站官方：`{"code":0,"data":{"bvid":...,"title":...,"pic":...,"owner":...,"stat":...}}`
- 各类第三方：`{"code":200,"data":{...}}`、`{"data":{...}}` 或直接返回视频对象

常见字段名都做了兼容（`pic/cover/image`、`owner.name/author/up`、`stat.view/play`、`danmaku/danmuku` 等）。

### 关于内置的「备用」接口

配置里内置了几个公开免费解析站作为备用（Pearktrue、Kxzjoker、Oioweb、52vmy），**默认关闭**。这类免费接口非常不稳定，经常 404 / 522 / 换域名。建议：

- 只用默认开启的「星之阁 + 官方」通常就够
- 或把自己信任的解析站加进列表，开启并行竞速
- 第三方接口会看到你解析的视频链接，敏感场景请自建解析服务

---

## 已知限制

- **大会员 / 付费 / 充电专属视频**不支持，官方接口也会拿不到高清晰度流
- 官方接口只能给信息，视频直链要下载时再调 `playurl`，未登录时清晰度可能被限制
- 第三方免费接口随时可能失效，插件会在日志里打印每个接口的失败原因
- 视频下载会占用内存与磁盘，默认关闭；大群建议保持关闭
- **视频会作为单独一条消息发送**：QQ / OneBot 要求 `video` 段不能与文字、图片混在同一条消息里（否则报 `retcode 1400: message element "video" must be the only segment`）
- **视频发送方式**（`video_send_mode`）：
  - `auto`（默认）：**先按本地路径发送**（配置了 AstrBot 的 `callback_api_base` 时，AstrBot 会自动把文件转成 HTTP 链接）；只有路径发送真的报错，才在文件不超过 `video_base64_max_mb` 时退回 `base64://`
  - `base64`：强制 base64。注意几十 MB 的视频转 base64 后可能被协议端截断，表现为「1 秒且全黑」，只建议小文件使用
  - `file`：强制本地路径，适合 AstrBot 与协议端共享文件系统
- 本地路径发送失败且文件超过 base64 上限时，插件会发一条文字提示，而不是悄悄发一个坏视频
- **下载内容会校验**：视频必须是足够大的真正 MP4（`ftyp` 头，且至少 64KB），只返回几百字节/十几 KB 初始化分片会被判定失败；失败后依次换 CDN 镜像、改用 `durl` 普通 MP4、再改用第三方接口直链。封面必须**字节完整**（JPEG 有 EOI、PNG 有 IEND、WebP 长度匹配）且能被 Pillow 完整解码，否则直接跳过——不会再把错误页、截断图或分片当图片/视频发出去
- **视频优先用本机签名的官方直链**：第三方接口返回的播放地址通常把签名绑定在对方服务器 IP 上，从你的服务器下载往往只有初始化分片；插件会优先自己请求 `playurl`（`fnval=1` + `platform=html5`）拿绑定本机 IP 的 `durl` 普通 MP4
- **视频优先用 `durl` 普通 MP4**（`fnval=1`），拿不到才退回 DASH；这样得到的文件更完整、更适合当作附件发送
- **base64 超限会自动降清晰度**：本地路径发送失败且文件超过 `video_base64_max_mb` 时，插件会依次用 360P / 最低清晰度重新下载，再以 base64 发送，尽量让视频能发出去
- 视频文件会在发送后延迟 10 分钟清理，启动时也会清理一天前的缓存，不会一直占磁盘
- 解析失败时默认静默（`show_error` 可打开），日志里仍有详细记录

### 为什么不能「直接让第三方解析接口返回图片/视频」

第三方解析接口（星之阁等）返回的是 **B 站 CDN 上的签名 URL**，它们并不托管图片和视频。所以：

- **封面**：插件支持 `cover_proxy`，默认用公开图片代理 `images.weserv.nl`：由代理服务器抓取 B 站封面并缩到 640px 再返回（约 13KB），小于常见的截断阈值，能完整拿到；
- **视频**：插件内置了第三方下载代理接口（`video_proxy_api`，默认 kukutool 的公开接口），直连失败时会用它代下；也可以填自己的代理模板或 HTTP 代理。开启 `video_proxy_first` 可以跳过直连尝试，直接走第三方代理。

插件绝不会把截断的图片或十几 KB 的初始化分片当成成品发送。

### 视频代理方案

公开的「视频代理」基本不存在：通用 CORS 代理（allorigins、codetabs 等）通常限制 5MB 左右、不保证 Range 和视频流，20MB 的视频拉不下来。插件内置了两种可用方案：

0. **第三方下载代理接口（默认开启）**：内置 `https://dy.kukutool.com/api/download-proxy-url`（kukutool / maxhelper）。直连 CDN 失败时，插件 POST `{url, sourceUrl, type}` 拿到代理地址，再用 **HTTP Range 分片**下载。可以在「视频下载代理接口列表」里添加多个同类接口，插件会**并行测速**（每个接口取一次代理地址并 Range 探测 64KB），自动选用最快且可用的一个并缓存 10 分钟。注意：第三方非官方接口随时可能失效，且会把视频地址发送给该服务；介意隐私可清空列表关闭。
1. **HTTP 代理（推荐自建）**：VPS 上跑 3proxy / Squid / mihomo，插件配置填 `proxy: http://user:pass@vps:port`。插件会让 **playurl 和视频下载走同一个出口 IP**，签名一致，B 站 CDN 就不会只返回初始化分片。
2. **大陆 VPS + nginx 反代**：见 `proxy-examples/nginx.conf`，部署后填 `video_proxy: https://你的域名/bili-proxy?url={url}`。
3. **Cloudflare Worker**：见 `cloudflare-worker/worker.js`，免费、部署快；但出口 IP 通常在海外，B 站可能同样只给初始化分片。

`video_proxy` 支持 `{url}`（URL 编码）、`{raw_url}`、`{host_path}` 三种占位符；`cover_proxy` 同理。

### 所有 CDN 地址都只返回十几 KB 的 MP4

说明 B 站视频 CDN 对你的服务器 IP 只返回了初始化分片（常见于海外服务器、被风控的 IP、或 API 与下载走了不同出口 IP）。插件会判定失败并尝试镜像 / `durl` / 第三方接口，如果全部如此：

1. 在插件配置里填 `proxy`，让 **playurl 请求和视频下载走同一个代理**（这样签名 IP 才一致）；
2. 或关闭「下载并发送视频」，只发封面 + 信息卡片；
3. 或者配置 AstrBot 的 `callback_api_base`，把下载好的视频转成 HTTP 链接（但这不能解决下载不到的根因）。

---

### 图片只下到 32KB、视频只有十几 KB

日志里出现 `封面数据不完整（32768 字节）`、`不是完整 MP4（15xxx 字节）`，通常是**服务器出网连接在传输中途被掐断**，常见原因：

- **IPv6 出网不通**：服务器优先走 IPv6，大响应卡在 MTU 上被截断。插件默认开启 `force_ipv4` 强制 IPv4，可在配置里确认；如果服务器只有 IPv6，请关闭
- **代理/中转**：代理对响应体有大小限制或超时
- **B 站 CDN 风控**：服务器 IP 被限制，只返回初始化分片

插件已经会拒绝不完整的数据（不会发半张图/坏视频），并尝试小尺寸封面、CDN 镜像、`durl`、第三方直链。若仍然失败，建议换代理或关闭视频下载。

---

## 许可证

MIT License。第三方解析接口由其各自提供方运营，与本插件无关。
