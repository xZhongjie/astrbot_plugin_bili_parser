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
| `send_cover` | `true` | 发送封面图 |
| `send_video` | `false` | 下载并发送视频 |
| `max_video_mb` | `30` | 视频大小上限，`0` 不限 |
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
- **视频会作为单独一条消息发送**：QQ / OneBot 要求 `video` 段不能与文字、图片混在同一条消息里（否则报 `retcode 1400: message element "video" must be the only segment`）。如果 AstrBot 与 QQ 协议端不在同一文件系统，请配置 AstrBot 的 `callback_api_base`，插件会把本地视频转成 HTTP 链接再发送；否则请保持 `send_video` 关闭
- 解析失败时默认静默（`show_error` 可打开），日志里仍有详细记录

---

## 许可证

MIT License。第三方解析接口由其各自提供方运营，与本插件无关。
