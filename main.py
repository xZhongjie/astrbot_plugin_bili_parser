# -*- coding: utf-8 -*-
"""astrbot_plugin_bili_parser — B 站视频链接解析插件。

设计目标：
* 自动识别聊天里的 B 站视频链接（BV / av / b23.tv 短链 / ?p= 分 P），解析后发送图文卡片
* 多解析源：第三方解析 API 可配置任意多个，请求时按「并行竞速」或「顺序回退」取第一个成功结果
* 官方 API 兜底：自动生成 buvid3 Cookie + 规范 UA/Referer，遇到 412 自动换 Cookie 重试
* 可选下载视频，下载时按 B 站 CDN 镜像 host 轮换，避免单条线路失败
* 频率限制、同链接冷却、失败提示，尽量不给第三方接口添麻烦

参考思路：koishi-plugin-bilibili-videolink-analysis（多请求模式 / 第三方解析接口）。
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse, urlunparse

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

try:
    from astrbot.core.star.filter.command import GreedyStr
except Exception:  # pragma: no cover - 兼容旧版本 AstrBot

    class GreedyStr(str):
        """旧版本回退：退化为普通字符串。"""


PLUGIN_NAME = "astrbot_plugin_bili_parser"

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_COVER_PROXY = (
    "https://images.weserv.nl/?url={host_path}&w=640&output=jpg"
)

DEFAULT_CARD_TEMPLATE = (
    "【{title}】\n"
    "UP主：{up} · 时长：{duration}\n"
    "播放 {view} · 点赞 {like} · 投币 {coin} · 收藏 {favorite}\n"
    "{desc}\n"
    "{url}"
)

DEFAULT_PROVIDERS = [
    {
        "__template_key": "provider",
        "name": "星之阁",
        "type": "xingzhige",
        "enabled": True,
        "url_template": "https://api.xingzhige.com/API/b_parse/?url={url}",
        "timeout": 12,
    },
    {
        "__template_key": "provider",
        "name": "B站官方",
        "type": "official",
        "enabled": True,
        "url_template": "https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        "timeout": 12,
    },
    {
        "__template_key": "provider",
        "name": "Pearktrue（备用）",
        "type": "custom",
        "enabled": False,
        "url_template": "https://api.pearktrue.cn/api/bilibili/?url={url}",
        "timeout": 12,
    },
    {
        "__template_key": "provider",
        "name": "Kxzjoker（备用）",
        "type": "custom",
        "enabled": False,
        "url_template": "https://api.kxzjoker.cn/api/bilibili?url={url}",
        "timeout": 12,
    },
    {
        "__template_key": "provider",
        "name": "Oioweb（备用）",
        "type": "custom",
        "enabled": False,
        "url_template": "https://api.oioweb.cn/api/bilibili/?url={url}",
        "timeout": 12,
    },
    {
        "__template_key": "provider",
        "name": "52vmy（备用）",
        "type": "custom",
        "enabled": False,
        "url_template": "https://api.52vmy.cn/api/video/bilibili?url={url}",
        "timeout": 12,
    },
]

BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# B 站视频 CDN 的镜像 host，下载失败时依次替换尝试
MIRROR_HOSTS = (
    "upos-sz-mirrorcos.bilivideo.com",
    "upos-sz-mirrorali.bilivideo.com",
    "upos-sz-mirrorhw.bilivideo.com",
    "upos-sz-mirror08c.bilivideo.com",
    "upos-sz-mirrorbd.bilivideo.com",
    "upos-hz-mirrorakam.akamaized.net",
)

RE_VIDEO_URL = re.compile(
    r"https?://(?:www\.|m\.|t\.)?bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)",
    re.IGNORECASE,
)
RE_SHORT_URL = re.compile(
    r"https?://(?:b23\.tv|bili2233\.cn)/([0-9A-Za-z]+)", re.IGNORECASE
)
RE_BARE_BV = re.compile(r"\b(BV[0-9A-Za-z]{10})\b", re.IGNORECASE)
RE_ANY_URL = re.compile(r"https?://[^\s<>\"']+")
RE_PAGE = re.compile(r"[?&]p=(\d+)", re.IGNORECASE)
RE_DURATION = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class BiliTarget:
    """一条待解析的 B 站视频目标。"""

    bvid: str = ""
    aid: int = 0
    page: int = 1
    url: str = ""

    @property
    def key(self) -> str:
        return f"{self.bvid or 'av' + str(self.aid)}:p{self.page}"

    @property
    def canonical_url(self) -> str:
        if self.url:
            return self.url
        if self.bvid:
            base = f"https://www.bilibili.com/video/{self.bvid}"
        else:
            base = f"https://www.bilibili.com/video/av{self.aid}"
        return f"{base}?p={self.page}" if self.page > 1 else base


@dataclass
class BiliVideo:
    """解析结果，字段都做了缺省处理，方便兼容各种第三方 JSON。"""

    bvid: str = ""
    aid: int = 0
    cid: int = 0
    title: str = ""
    desc: str = ""
    cover: str = ""
    duration: int = 0
    owner_name: str = ""
    owner_face: str = ""
    stat: dict = field(default_factory=dict)
    pages: list = field(default_factory=list)
    video_url: str = ""
    source: str = ""
    page: int = 1

    @property
    def page_title(self) -> str:
        for item in self.pages:
            try:
                if int(item.get("page", 0)) == self.page:
                    return str(item.get("part") or "")
            except (TypeError, ValueError):
                continue
        return ""


class ProviderError(Exception):
    """单个解析源失败。"""


class AllProvidersFailed(Exception):
    """全部解析源失败。"""


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def to_int(value: Any) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return 0
        if text.isdigit():
            return int(text)
        # "1.2万" / "3.4亿"
        match = re.match(r"^([\d.]+)\s*([万亿wWkK]?)$", text)
        if match:
            number = float(match.group(1))
            unit = match.group(2)
            if unit in ("万", "w", "W"):
                number *= 10000
            elif unit == "亿":
                number *= 100000000
            elif unit in ("k", "K"):
                number *= 1000
            return int(number)
        return 0
    except Exception:
        return 0


def format_count(value: Any) -> str:
    number = to_int(value)
    if number <= 0:
        return "0"
    if number >= 100000000:
        return f"{number / 100000000:.1f}亿"
    if number >= 10000:
        return f"{number / 10000:.1f}万"
    return str(number)


def format_duration(value: Any) -> str:
    seconds = to_int(value)
    if seconds <= 0:
        match = RE_DURATION.match(str(value or "").strip())
        if match:
            hours = int(match.group(1) or 0)
            seconds = hours * 3600 + int(match.group(2)) * 60 + int(match.group(3))
    if seconds <= 0:
        return "未知"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def normalize_cover(url: str) -> str:
    url = str(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    return url.replace("http://", "https://", 1) if url.startswith("http://") else url


def _pick(node: dict, *paths: str, default: Any = None) -> Any:
    """按点号路径取值，返回第一个非空值。"""
    for path in paths:
        current: Any = node
        ok = True
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                ok = False
                break
        if ok and current not in (None, "", [], {}):
            return current
    return default


def _find_video_node(payload: Any) -> dict | None:
    """在任意 JSON 里找最像「视频信息」的那个对象。"""
    best: dict | None = None
    best_score = -1
    stack: list[Any] = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            score = 0
            bvid = str(current.get("bvid") or "")
            if bvid.startswith("BV"):
                score += 3
            if "aid" in current:
                score += 1
            if any(k in current for k in ("title", "video", "owner", "stat", "pic")):
                score += 2
            if score > best_score:
                best, best_score = current, score
            stack.extend(v for v in current.values() if isinstance(v, (dict, list)))
        elif isinstance(current, list):
            stack.extend(v for v in current if isinstance(v, (dict, list)))
    return best if best_score > 0 else None


def extract_video(payload: Any, source: str = "") -> BiliVideo | None:
    """从第三方 / 官方 JSON 中提取视频信息。

    兼容以下几种常见结构：
    * 星之阁：{"code":0,"data":{"bvid":..,"video":{"title":..,"fm":..,"url":..},"owner":..,"stat":..}}
    * B站官方：{"code":0,"data":{"bvid":..,"title":..,"pic":..,"owner":..,"stat":..}}
    * 各类第三方：{"code":200,"data":{...}} / {"data":{...}} / 直接返回 {...}
    """
    node = _find_video_node(payload)
    if node is None:
        return None

    bvid = str(_pick(node, "bvid", "data.bvid", default="") or "").strip()
    aid = to_int(_pick(node, "aid", "data.aid", default=0))
    title = str(
        _pick(
            node,
            "title",
            "video.title",
            "data.title",
            "data.video.title",
            default="",
        )
        or ""
    ).strip()
    if not title:
        return None
    if not bvid and not aid and not _pick(node, "video.url", "url", "data.video.url"):
        return None

    stat = _pick(node, "stat", "data.stat", default={}) or {}
    if not isinstance(stat, dict):
        stat = {}

    pages = _pick(node, "pages", "data.pages", default=[]) or []

    video_url = str(
        _pick(node, "video.url", "url", "data.video.url", "play_url", default="") or ""
    ).strip()
    if video_url and not video_url.startswith(("http://", "https://")):
        video_url = ""

    return BiliVideo(
        bvid=bvid,
        aid=aid,
        cid=to_int(_pick(node, "cid", "data.cid", default=0)),
        title=title,
        desc=str(_pick(node, "desc", "video.desc", "data.desc", default="") or ""),
        cover=normalize_cover(
            str(
                _pick(
                    node,
                    "pic",
                    "cover",
                    "video.fm",
                    "video.cover",
                    "data.pic",
                    default="",
                )
                or ""
            )
        ),
        duration=to_int(
            _pick(node, "duration", "video.duration", "data.duration", default=0)
        ),
        owner_name=str(
            _pick(
                node,
                "owner.name",
                "author.name",
                "author",
                "up",
                "uname",
                "data.owner.name",
                default="",
            )
            or ""
        ),
        owner_face=normalize_cover(
            str(
                _pick(
                    node,
                    "owner.face",
                    "avatar",
                    "data.owner.face",
                    default="",
                )
                or ""
            )
        ),
        stat={
            "view": _pick(stat, "view", "play", "views", default=0),
            "like": _pick(stat, "like", "likes", default=0),
            "coin": _pick(stat, "coin", "coins", default=0),
            "favorite": _pick(stat, "favorite", "favorites", "collect", default=0),
            "danmaku": _pick(stat, "danmaku", "danmuku", default=0),
            "share": _pick(stat, "share", "shares", default=0),
            "reply": _pick(stat, "reply", "replies", default=0),
        },
        pages=[p for p in pages if isinstance(p, dict)][:50],
        video_url=video_url,
        source=source,
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class HttpClient:
    """带超时、体积上限、可选代理和强制 IPv4 的 aiohttp 封装。"""

    def __init__(
        self, max_bytes: int, proxy: str = "", force_ipv4: bool = True
    ) -> None:
        self.max_bytes = max_bytes
        self.proxy = proxy.strip()
        self.force_ipv4 = force_ipv4
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = None
            if self.force_ipv4:
                # 很多服务器 IPv6 出网不通：大响应会在传输中途被掐断/卡住，
                # 表现为图片只下到 32KB、视频只下到十几 KB。强制 IPv4 可绕过。
                try:
                    import socket

                    connector = aiohttp.TCPConnector(family=socket.AF_INET)
                except Exception:
                    connector = None
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=180),
                headers={"User-Agent": BILI_HEADERS["User-Agent"]},
                connector=connector,
            )
        return self._session

    async def request_full(
        self,
        url: str,
        *,
        headers: dict | None = None,
        timeout: float = 15,
        allow_redirects: bool = True,
        method: str = "GET",
        json_body: Any = None,
    ) -> tuple[int, bytes, dict]:
        """返回 (状态码, 响应体, 响应头)。"""
        session = await self._get_session()
        kwargs: dict[str, Any] = {
            "headers": headers or {},
            "timeout": aiohttp.ClientTimeout(total=timeout),
            "allow_redirects": allow_redirects,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        if json_body is not None:
            kwargs["json"] = json_body
        async with session.request(method, url, **kwargs) as response:
            body = await response.content.read(self.max_bytes + 1)
            resp_headers = {k.lower(): v for k, v in response.headers.items()}
            return response.status, body, resp_headers

    async def request(
        self,
        url: str,
        *,
        headers: dict | None = None,
        timeout: float = 15,
        allow_redirects: bool = True,
    ) -> tuple[int, bytes]:
        status, body, _ = await self.request_full(
            url, headers=headers, timeout=timeout, allow_redirects=allow_redirects
        )
        return status, body

    async def post_json(
        self,
        url: str,
        payload: Any,
        *,
        headers: dict | None = None,
        timeout: float = 20,
    ) -> tuple[int, Any]:
        status, body, _ = await self.request_full(
            url,
            headers={"Content-Type": "application/json", **(headers or {})},
            timeout=timeout,
            method="POST",
            json_body=payload,
        )
        if status != 200:
            return status, None
        try:
            return status, json.loads(body.decode("utf-8", errors="replace"))
        except Exception:
            return status, None

    async def download_range(
        self,
        url: str,
        *,
        headers: dict | None = None,
        chunk_size: int = 2 * 1024 * 1024,
        max_bytes: int = 0,
        timeout: float = 180,
    ) -> bytes | None:
        """分片下载，绕过单次连接被截断的问题。

        先请求 `bytes=0-(chunk-1)`：
        * 返回 206 时按 Content-Range 的总长度循环取完；
        * 返回 200 说明服务端不支持 Range，直接使用整包响应体。
        """
        request_headers = dict(headers or {})
        status, first, resp_headers = await self.request_full(
            url,
            headers={**request_headers, "Range": f"bytes=0-{chunk_size - 1}"},
            timeout=timeout,
        )
        if status == 200:
            return first or None
        if status != 206 or not first:
            return None

        total = 0
        content_range = resp_headers.get("content-range") or ""
        if "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[-1])
            except ValueError:
                total = 0

        data = bytearray(first)
        offset = len(first)
        while total and offset < total:
            if max_bytes and offset >= max_bytes:
                return None
            end = min(offset + chunk_size - 1, total - 1)
            status, part, _ = await self.request_full(
                url,
                headers={**request_headers, "Range": f"bytes={offset}-{end}"},
                timeout=timeout,
            )
            if status not in (200, 206) or not part:
                break
            data.extend(part)
            offset += len(part)
            if len(part) == 0:
                break

        if total and len(data) < total:
            logger.warning(
                "[bili] 分片下载不完整：期望 %d 字节，实际 %d 字节", total, len(data)
            )
            return None
        return bytes(data)

    async def get_text(
        self, url: str, *, headers: dict | None = None, timeout: float = 15
    ) -> tuple[int, str]:
        status, body = await self.request(url, headers=headers, timeout=timeout)
        return status, body.decode("utf-8", errors="replace")

    async def get_json(
        self, url: str, *, headers: dict | None = None, timeout: float = 15
    ) -> tuple[int, Any]:
        status, text = await self.get_text(url, headers=headers, timeout=timeout)
        if status != 200:
            return status, None
        try:
            return status, json.loads(text)
        except Exception:
            return status, None

    async def get_bytes(
        self, url: str, *, headers: dict | None = None, timeout: float = 120
    ) -> bytes | None:
        try:
            status, body = await self.request(url, headers=headers, timeout=timeout)
        except Exception as exc:
            logger.warning("[bili] 下载失败：%s（%s）", url[:160], exc)
            return None
        if status != 200 or not body or len(body) > self.max_bytes:
            return None
        return body

    async def resolve(self, url: str, *, headers: dict | None = None) -> str:
        """跟随重定向，返回最终 URL（用于 b23.tv 短链）。"""
        session = await self._get_session()
        kwargs: dict[str, Any] = {
            "headers": headers or {},
            "timeout": aiohttp.ClientTimeout(total=15),
            "allow_redirects": True,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        async with session.get(url, **kwargs) as response:
            return str(response.url)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


class BiliParserPlugin(Star):
    """B 站视频链接解析。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self.http: HttpClient | None = None
        self.providers: list[dict] = self._load_providers()
        self._buvid3 = ""
        self._recent: dict[str, float] = {}
        self._proxy_endpoint_cache: tuple[str, float] | None = None
        self._data_dir: Path | None = None
        self._initialized = False

    # -- 配置 ---------------------------------------------------------------
    def _load_providers(self) -> list[dict]:
        raw = self.config.get("providers")
        if raw is None:
            raw = DEFAULT_PROVIDERS
        providers: list[dict] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                ptype = str(item.get("type") or "custom").strip() or "custom"
                name = str(item.get("name") or "").strip() or ptype
                timeout = float(item.get("timeout") or 0)
                if timeout <= 0:
                    timeout = float(self.config.get("timeout") or 12)
                providers.append(
                    {
                        "name": name,
                        "type": ptype,
                        "enabled": bool(item.get("enabled", True)),
                        "url_template": str(item.get("url_template") or "").strip(),
                        "timeout": timeout,
                    }
                )
        return providers

    @staticmethod
    def _default_template(provider_type: str) -> str:
        if provider_type == "xingzhige":
            return "https://api.xingzhige.com/API/b_parse/?url={url}"
        if provider_type == "official":
            return "https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        return ""

    # -- 生命周期 -----------------------------------------------------------
    async def initialize(self) -> None:
        self._data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        max_mb = int(self.config.get("max_video_mb") or 30)
        max_bytes = max(max_mb, 8) * 1024 * 1024
        self.http = HttpClient(
            max_bytes,
            proxy=str(self.config.get("proxy") or "").strip(),
            force_ipv4=bool(self.config.get("force_ipv4", True)),
        )
        self.providers = self._load_providers()
        self._rotate_buvid()
        await self._prune_media_cache()
        enabled = [p["name"] for p in self.providers if p["enabled"]]
        self._initialized = True
        logger.info(
            "[bili] 插件已加载：%d 个解析接口启用（%s）",
            len(enabled),
            "、".join(enabled) if enabled else "无",
        )

    async def terminate(self) -> None:
        if self.http is not None:
            await self.http.close()
            self.http = None
        self._initialized = False

    async def _prune_media_cache(self) -> None:
        """清理一天前的临时视频，避免磁盘越用越多。"""
        if self._data_dir is None:
            return
        cutoff = time.time() - 86400
        try:
            for path in self._data_dir.glob("bili_*.mp4"):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                except OSError:
                    continue
        except Exception:
            pass

    # -- 官方 cookie --------------------------------------------------------
    def _rotate_buvid(self) -> None:
        self._buvid3 = str(uuid.uuid4()).upper()

    def _cookie(self) -> str:
        parts = [f"buvid3={self._buvid3}infoc", f"b_nut={int(time.time())}"]
        extra = str(self.config.get("cookie") or "").strip()
        if extra:
            parts.append(extra)
        return "; ".join(parts)

    # -- 单源请求 -----------------------------------------------------------
    def _render_template(self, template: str, target: BiliTarget) -> str:
        values = {
            "url": quote(target.canonical_url, safe=""),
            "raw_url": target.canonical_url,
            "bvid": target.bvid,
            "aid": str(target.aid or ""),
            "page": str(target.page),
        }
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError) as exc:
            raise ProviderError(f"接口模板不合法：{exc}") from exc

    async def _query_official(self, provider: dict, target: BiliTarget) -> BiliVideo:
        template = provider["url_template"] or self._default_template("official")
        if not target.bvid and "{bvid}" in template:
            template = "https://api.bilibili.com/x/web-interface/view?aid={aid}"
        url = self._render_template(template, target)

        last_error = ""
        for attempt in range(3):
            headers = dict(BILI_HEADERS)
            headers["Cookie"] = self._cookie()
            status, payload = await self.http.get_json(
                url, headers=headers, timeout=provider["timeout"]
            )
            if status == 412:
                last_error = "HTTP 412（触发 B 站风控）"
                logger.info("[bili] 官方接口返回 412，更换 buvid3 重试（第 %d 次）", attempt + 1)
                self._rotate_buvid()
                await asyncio.sleep(0.4 * (attempt + 1))
                continue
            if status != 200:
                raise ProviderError(f"HTTP {status}")
            if not isinstance(payload, dict):
                raise ProviderError("返回内容不是 JSON")
            code = payload.get("code")
            if code not in (0, None):
                message = payload.get("message") or payload.get("msg") or f"code={code}"
                raise ProviderError(str(message))
            video = extract_video(payload, source=provider["name"])
            if video is None:
                raise ProviderError("返回中没有视频信息")
            data = payload.get("data") or {}
            if not video.cid:
                video.cid = to_int(data.get("cid"))
            if not video.pages:
                video.pages = [
                    item for item in (data.get("pages") or []) if isinstance(item, dict)
                ]
            return video
        raise ProviderError(last_error or "官方接口不可用")

    async def _query_third_party(
        self, provider: dict, target: BiliTarget
    ) -> BiliVideo:
        template = provider["url_template"] or self._default_template(
            provider["type"]
        )
        if not template:
            raise ProviderError("未配置 url_template")
        url = self._render_template(template, target)
        status, payload = await self.http.get_json(
            url, headers=BILI_HEADERS, timeout=provider["timeout"]
        )
        if status != 200:
            raise ProviderError(f"HTTP {status}")
        if payload is None:
            raise ProviderError("返回内容不是 JSON")
        video = extract_video(payload, source=provider["name"])
        if video is None:
            raise ProviderError("返回中没有视频信息")
        return video

    async def _query_provider(
        self, provider: dict, target: BiliTarget
    ) -> BiliVideo:
        timeout = float(provider.get("timeout") or 12)
        if provider["type"] == "official":
            coro = self._query_official(provider, target)
        else:
            coro = self._query_third_party(provider, target)
        try:
            video = await asyncio.wait_for(coro, timeout=timeout + 2)
        except asyncio.TimeoutError as exc:
            raise ProviderError(f"超时（{timeout:.0f}s）") from exc
        if video is None:
            raise ProviderError("没有解析结果")
        video.page = target.page
        return video

    # -- 多源调度 -----------------------------------------------------------
    async def fetch(self, target: BiliTarget) -> BiliVideo:
        if self.http is None:
            raise AllProvidersFailed("插件尚未初始化")
        providers = [item for item in self.providers if item.get("enabled", True)]
        if not providers:
            raise AllProvidersFailed("没有启用的解析接口，请在插件配置里开启至少一个")

        mode = str(self.config.get("request_mode") or "parallel").strip().lower()
        if mode == "fallback":
            errors: list[str] = []
            for provider in providers:
                try:
                    video = await self._query_provider(provider, target)
                    logger.info("[bili] %s 解析成功：%s", provider["name"], target.key)
                    return video
                except Exception as exc:
                    errors.append(f"{provider['name']}：{self._brief(exc)}")
                    logger.info(
                        "[bili] 接口 %s 失败：%s", provider["name"], self._brief(exc)
                    )
            raise AllProvidersFailed("；".join(errors))

        tasks: dict[asyncio.Task, dict] = {
            asyncio.create_task(self._query_provider(provider, target)): provider
            for provider in providers
        }
        errors = []
        try:
            while tasks:
                done, _ = await asyncio.wait(
                    list(tasks.keys()), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    provider = tasks.pop(task, None)
                    try:
                        video = task.result()
                    except Exception as exc:
                        label = (provider or {}).get("name", "接口")
                        errors.append(f"{label}：{self._brief(exc)}")
                        logger.info(
                            "[bili] 接口 %s 失败：%s", label, self._brief(exc)
                        )
                        continue
                    logger.info(
                        "[bili] %s 解析成功：%s", (provider or {}).get("name"), target.key
                    )
                    return video
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.keys(), return_exceptions=True)
        raise AllProvidersFailed("；".join(errors) or "全部接口失败")

    @staticmethod
    def _brief(exc: Any) -> str:
        text = str(exc).replace("\n", " ").strip()
        return text[:120] or type(exc).__name__

    # -- 链接解析 -----------------------------------------------------------
    @staticmethod
    def _clean_url(url: str) -> str:
        return url.rstrip("，。、！!？?）)】]}>\"'`;；")

    def _target_from_url(self, url: str) -> BiliTarget | None:
        try:
            parsed = urlparse(url)
        except Exception:
            return None
        match = re.search(
            r"/video/(BV[0-9A-Za-z]{10}|av(\d+))", parsed.path, re.IGNORECASE
        )
        if not match:
            return None
        token = match.group(1)
        bvid = f"BV{token[2:]}" if token.lower().startswith("bv") else ""
        aid = int(match.group(2)) if match.group(2) else 0
        page = 1
        query = parse_qs(parsed.query)
        if "p" in query:
            page = to_int(query["p"][0]) or 1
        page = max(1, page)
        return BiliTarget(bvid=bvid, aid=aid, page=page, url=url)

    async def resolve_short(self, url: str) -> BiliTarget | None:
        if self.http is None:
            return None
        try:
            final = await self.http.resolve(url, headers=BILI_HEADERS)
        except Exception as exc:
            logger.info("[bili] 短链解析失败：%s（%s）", url, self._brief(exc))
            return None
        target = self._target_from_url(final)
        if target is not None:
            return target
        match = RE_VIDEO_URL.search(final)
        if match:
            return self._target_from_url(match.group(0))
        return None

    async def extract_targets(self, text: str) -> list[BiliTarget]:
        """从一段文本里提取所有可解析的 B 站视频目标。"""
        text = text or ""
        targets: list[BiliTarget] = []
        seen: set[str] = set()

        for raw in RE_ANY_URL.finditer(text):
            url = self._clean_url(raw.group(0))
            host = (urlparse(url).hostname or "").lower()
            target: BiliTarget | None = None
            if host.endswith("bilibili.com") or host.endswith("bilibili.tv"):
                target = self._target_from_url(url)
            elif host in ("b23.tv", "bili2233.cn"):
                target = await self.resolve_short(url)
            if target is not None and target.key not in seen:
                seen.add(target.key)
                targets.append(target)

        if not targets:
            for match in RE_BARE_BV.finditer(text):
                bvid = match.group(1)
                # 统一前缀大写，兼容用户手打的小写 bv
                bvid = f"BV{bvid[2:]}"
                target = BiliTarget(
                    bvid=bvid, url=f"https://www.bilibili.com/video/{bvid}"
                )
                if target.key not in seen:
                    seen.add(target.key)
                    targets.append(target)
        return targets

    # -- 卡片 ---------------------------------------------------------------
    def render_card(self, video: BiliVideo, target: BiliTarget) -> str:
        template = str(self.config.get("template") or "").strip()
        if not template:
            template = DEFAULT_CARD_TEMPLATE

        title = video.title
        if target.page > 1:
            page_title = video.page_title
            suffix = f"（P{target.page}{' ' + page_title if page_title else ''}）"
            if suffix not in title:
                title = f"{title}{suffix}"

        desc = re.sub(r"\s+", " ", video.desc or "").strip()
        max_desc = int(self.config.get("desc_max_chars") or 0)
        if max_desc > 0 and len(desc) > max_desc:
            desc = desc[:max_desc].rstrip() + "…"

        values = {
            "title": title,
            "up": video.owner_name or "未知",
            "bvid": video.bvid or f"av{video.aid}",
            "aid": str(video.aid or ""),
            "page": str(target.page),
            "duration": format_duration(video.duration),
            "view": format_count(video.stat.get("view")),
            "like": format_count(video.stat.get("like")),
            "coin": format_count(video.stat.get("coin")),
            "favorite": format_count(video.stat.get("favorite")),
            "danmaku": format_count(video.stat.get("danmaku")),
            "desc": desc,
            "url": target.canonical_url,
            "source": video.source or "未知",
        }
        try:
            text = template.format(**values)
        except (KeyError, IndexError, ValueError):
            text = DEFAULT_CARD_TEMPLATE.format(**values)

        lines = [line.rstrip() for line in text.splitlines()]
        text = "\n".join(line for line in lines if line.strip())
        return text.strip()

    # -- 媒体 ---------------------------------------------------------------
    @staticmethod
    def _wrap_proxy(template: str, url: str) -> str:
        """把媒体地址套进第三方代理模板。

        可用占位符：
        * {url}      完整地址（URL 编码）
        * {raw_url}  完整地址（不编码）
        * {host_path} 去掉协议的 host+path（weserv 等代理常用）
        """
        if not template or not url:
            return ""
        try:
            parsed = urlparse(url)
            host_path = parsed.netloc + parsed.path
            if parsed.query:
                host_path += "?" + parsed.query
            return template.format(
                url=quote(url, safe=""),
                raw_url=url,
                host_path=quote(host_path, safe="/?=&:@"),
            )
        except Exception:
            return ""

    def _cover_candidates(self, cover: str) -> list[tuple[str, dict]]:
        """封面的多种候选地址。

        B 站图片 CDN 支持 `@宽_高_裁剪` 缩略图语法。部分服务器网络会把响应
        截断在 32KB 左右，此时请求一张更小的缩略图反而能完整拿到；如果直连
        怎么都不行，就交给第三方图片代理（由代理服务器去抓 B 站，返回小图）。
        """
        image_headers = {
            "User-Agent": BILI_HEADERS["User-Agent"],
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        base = cover.split("@", 1)[0]
        candidates: list[tuple[str, dict]] = [
            (cover, BILI_HEADERS),
            (cover, image_headers),
        ]
        for suffix in (
            "@480w_270h_1c.webp",
            "@480w_270h_1c",
            "@320w_180h_1c.jpg",
        ):
            candidates.append((base + suffix, image_headers))
        if cover.startswith("https://"):
            candidates.append((cover.replace("https://", "http://", 1), image_headers))

        # 第三方图片代理兜底（配置为空字符串表示关闭）
        raw_proxy = self.config.get("cover_proxy")
        if raw_proxy is None:
            proxy_template = DEFAULT_COVER_PROXY
        else:
            proxy_template = str(raw_proxy).strip()
        if proxy_template:
            seen: set[str] = set()
            for source in (base, cover, base + "@480w_270h_1c.jpg"):
                wrapped = self._wrap_proxy(proxy_template, source)
                if wrapped and wrapped not in seen:
                    seen.add(wrapped)
                    candidates.append((wrapped, image_headers))
        return candidates

    async def _cover_component(self, video: BiliVideo):
        if not video.cover or self.http is None:
            return None

        data = None
        for candidate_url, headers in self._cover_candidates(video.cover):
            if not candidate_url:
                continue
            data = await self.http.get_bytes(
                candidate_url, headers=headers, timeout=20
            )
            if not data:
                continue
            # 连接被提前关闭时可能只拿到半张图，PIL 会容忍截断的 JPEG 并把
            # 缺失部分填灰。这里先做字节级完整性校验。
            if not self._is_complete_image(data):
                logger.warning(
                    "[bili] 封面数据不完整（%d 字节），换方式重试：%s",
                    len(data),
                    candidate_url[:120],
                )
                data = None
                continue
            break
        if not data:
            logger.warning("[bili] 封面下载失败，跳过封面：%s", video.cover[:120])
            return None

        # B站封面经常是 WebP / AVIF / 带透明通道，部分 QQ 客户端会解码不全
        # （表现为下半截灰色），统一转成 JPEG 再发。
        normalized = await asyncio.to_thread(self._normalize_cover, data)
        if not normalized:
            # 下载到的不是有效图片（可能是防盗链返回的错误页），宁可不发，
            # 也不要发一张灰色破图。
            logger.warning(
                "[bili] 封面不是有效图片（%d 字节，文件头 %s），跳过封面",
                len(data),
                data[:16].hex(),
            )
            return None
        logger.debug(
            "[bili] 封面已转码：%d 字节 -> %d 字节 JPEG",
            len(data),
            len(normalized),
        )
        return Comp.Image.fromBase64(base64.b64encode(normalized).decode())

    @staticmethod
    def _is_complete_image(data: bytes) -> bool:
        """粗略判断图片字节是否完整（防止半张灰图）。"""
        if len(data) < 128:
            return False
        if data[:2] == b"\xff\xd8":  # JPEG
            return data.rstrip(b"\x00").endswith(b"\xff\xd9")
        if data[:8] == b"\x89PNG\r\n\x1a\n":  # PNG
            return b"IEND" in data[-64:]
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # WebP
            try:
                size = int.from_bytes(data[4:8], "little") + 8
            except Exception:
                return False
            return len(data) >= size
        if data[:6] in (b"GIF87a", b"GIF89a"):  # GIF
            return data.rstrip(b"\x00").endswith(b"\x3b")
        return True

    @staticmethod
    def _normalize_cover(data: bytes) -> bytes | None:
        try:
            import io

            from PIL import Image as PILImage

            with PILImage.open(io.BytesIO(data)) as image:
                logger.debug(
                    "[bili] 封面原图：%s %s %s",
                    image.format,
                    image.mode,
                    image.size,
                )
                # 强制完整解码；截断文件会在这里抛异常，而不是生成灰图
                image.load()
                image = image.convert("RGB")
                max_width, max_height = 1280, 4000
                if image.width > max_width or image.height > max_height:
                    scale = min(
                        max_width / image.width, max_height / image.height
                    )
                    size = (
                        max(1, int(image.width * scale)),
                        max(1, int(image.height * scale)),
                    )
                    resample = getattr(PILImage, "Resampling", PILImage).LANCZOS
                    image = image.resize(size, resample)
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=88, optimize=True)
                return buffer.getvalue()
        except Exception as exc:
            logger.debug("[bili] 封面转码失败，回退原图：%s", exc)
            return None

    @staticmethod
    def _candidate_video_urls(url: str):
        yield url
        try:
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
        except Exception:
            return
        if "bilivideo.com" not in host and "akamaized.net" not in host:
            return
        for mirror in MIRROR_HOSTS:
            if mirror == host:
                continue
            yield urlunparse(parsed._replace(netloc=mirror))

    def _ensure_cid(self, video: BiliVideo) -> None:
        if video.cid:
            return
        for item in video.pages:
            try:
                if int(item.get("page", 0)) == video.page:
                    video.cid = to_int(item.get("cid"))
                    break
            except (TypeError, ValueError):
                continue
        if not video.cid and video.pages:
            video.cid = to_int(video.pages[0].get("cid"))

    async def _request_play_url(
        self, video: BiliVideo, qn: int, fnval: int
    ) -> str:
        """请求 playurl，返回直链。

        fnval=1 返回普通 MP4（durl），对「发文件」最稳；
        fnval=16 返回 DASH（m4s），作为 durl 不可用时的兜底。
        """
        if self.http is None or not (video.bvid or video.aid):
            return ""
        if video.bvid:
            url = (
                "https://api.bilibili.com/x/player/playurl"
                f"?bvid={video.bvid}&cid={video.cid}&qn={qn}&fnval={fnval}"
                "&fourk=1&platform=html5&high_quality=1"
            )
        else:
            url = (
                "https://api.bilibili.com/x/player/playurl"
                f"?avid={video.aid}&cid={video.cid}&qn={qn}&fnval={fnval}"
                "&fourk=1&platform=html5&high_quality=1"
            )
        headers = dict(BILI_HEADERS)
        headers["Cookie"] = self._cookie()
        status, payload = await self.http.get_json(url, headers=headers, timeout=15)
        if status != 200 or not isinstance(payload, dict):
            return ""
        data = payload.get("data") or {}
        if fnval == 1:
            for item in data.get("durl") or []:
                if not isinstance(item, dict):
                    continue
                candidate = str(item.get("url") or "").strip()
                if candidate.startswith(("http://", "https://")):
                    return candidate
            return ""
        dash = data.get("dash") or {}
        videos = dash.get("video") or []
        if videos:
            first = videos[0] or {}
            return str(first.get("baseUrl") or first.get("base_url") or "").strip()
        return ""

    async def _fill_play_url(
        self,
        video: BiliVideo,
        qn: int | None = None,
        force: bool = False,
    ) -> None:
        """官方接口只给信息不给直链，下载视频前再取一次 playurl。"""
        if self.http is None:
            return
        if video.video_url and not force:
            return
        self._ensure_cid(video)
        if not (video.bvid or video.aid):
            return
        qn = qn or int(self.config.get("video_quality") or 64)
        source = "durl"
        url = await self._request_play_url(video, qn, 1)
        if not url:
            source = "dash"
            url = await self._request_play_url(video, qn, 16)
        if url:
            video.video_url = url
            logger.info("[bili] 已获取视频直链（qn=%s, %s）", qn, source)

    @staticmethod
    def _looks_like_mp4(data: bytes) -> bool:
        return len(data) > 12 and data[4:8] == b"ftyp"

    async def _playurl_durl_urls(self, video: BiliVideo) -> list[str]:
        """直链下载失败时的兜底：用 fnval=1 取普通 MP4 的 durl 地址。"""
        if self.http is None or not (video.bvid or video.aid):
            return []
        qn = int(self.config.get("video_quality") or 64)
        if video.bvid:
            url = (
                "https://api.bilibili.com/x/player/playurl"
                f"?bvid={video.bvid}&cid={video.cid}&qn={qn}&fnval=1&fourk=1"
            )
        else:
            url = (
                "https://api.bilibili.com/x/player/playurl"
                f"?avid={video.aid}&cid={video.cid}&qn={qn}&fnval=1&fourk=1"
            )
        headers = dict(BILI_HEADERS)
        headers["Cookie"] = self._cookie()
        status, payload = await self.http.get_json(url, headers=headers, timeout=15)
        if status != 200 or not isinstance(payload, dict):
            return []
        data = payload.get("data") or {}
        urls = [
            str(item.get("url") or "").strip()
            for item in (data.get("durl") or [])
            if isinstance(item, dict)
        ]
        return [item for item in urls if item.startswith(("http://", "https://"))]

    async def _provider_video_urls(self, target: BiliTarget) -> list[str]:
        """直链全失败时，再向第三方接口要一次视频直链。"""
        if self.http is None:
            return []
        urls: list[str] = []
        for provider in self.providers:
            if not provider.get("enabled", True):
                continue
            if provider["type"] == "official":
                continue
            try:
                result = await asyncio.wait_for(
                    self._query_provider(provider, target),
                    timeout=float(provider.get("timeout") or 12) + 2,
                )
            except Exception as exc:
                logger.info(
                    "[bili] 备用接口 %s 获取视频直链失败：%s",
                    provider["name"],
                    self._brief(exc),
                )
                continue
            if result is not None and result.video_url:
                urls.append(result.video_url)
        return urls

    def _add_video_proxy(self, urls: list[str]) -> list[str]:
        """给视频地址追加第三方代理候选（配置了 video_proxy 时）。"""
        template = str(self.config.get("video_proxy") or "").strip()
        if not template or not urls:
            return urls
        out = list(urls)
        for url in list(urls):
            wrapped = self._wrap_proxy(template, url)
            if wrapped and wrapped not in out:
                out.append(wrapped)
        return out

    async def _fetch_media(
        self, url: str, headers: dict, max_bytes: int
    ) -> bytes | None:
        """下载媒体：优先分片，失败再整包。"""
        if self.http is None:
            return None
        chunk_kb = int(self.config.get("download_chunk_kb") or 0)
        if chunk_kb > 0:
            data = await self.http.download_range(
                url,
                headers=headers,
                chunk_size=chunk_kb * 1024,
                max_bytes=max_bytes,
            )
            if data:
                return data
        return await self.http.get_bytes(url, headers=headers, timeout=180)

    async def _proxy_media_url(
        self, api_url: str, video_url: str, target: BiliTarget | None
    ) -> str:
        """调用第三方下载代理接口，把 CDN 地址换成代理地址。"""
        if self.http is None:
            return ""
        payload = {
            "url": video_url,
            "sourceUrl": target.canonical_url if target is not None else "",
            "type": "video",
        }
        try:
            parsed = urlparse(api_url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            origin = ""
        status, data = await self.http.post_json(
            api_url,
            payload,
            headers={
                "Origin": origin,
                "Referer": origin + "/" if origin else "",
            },
        )
        if status == 200 and isinstance(data, dict):
            url = data.get("url")
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return url
        logger.info("[bili] 视频代理接口 %s 未返回可用地址（status=%s）", api_url, status)
        return ""

    def _proxy_endpoints(self) -> list[tuple[str, str]]:
        """收集所有启用的视频下载代理接口（名称, 地址）。"""
        endpoints: list[tuple[str, str]] = []
        legacy = str(self.config.get("video_proxy_api") or "").strip()
        if legacy:
            endpoints.append(("legacy", legacy))
        raw = self.config.get("video_proxy_apis")
        if isinstance(raw, list):
            for index, item in enumerate(raw):
                if not isinstance(item, dict) or not item.get("enabled", True):
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                name = str(item.get("name") or f"api-{index + 1}").strip()
                endpoints.append((name, url))
        seen: set[str] = set()
        unique: list[tuple[str, str]] = []
        for name, url in endpoints:
            if url in seen:
                continue
            seen.add(url)
            unique.append((name, url))
        return unique

    async def _probe_proxy_url(self, url: str) -> bool:
        """快速验证代理地址可用（Range 取 64KB，确认是 MP4）。"""
        if self.http is None:
            return False
        try:
            status, body, _ = await self.http.request_full(
                url,
                headers={
                    **BILI_HEADERS,
                    "Accept": "video/*,*/*;q=0.8",
                    "Range": "bytes=0-65535",
                },
                timeout=20,
            )
        except Exception:
            return False
        return status in (200, 206) and len(body) > 1024 and self._looks_like_mp4(body)

    async def _resolve_proxy_url(
        self, video_url: str, target: BiliTarget | None
    ) -> str:
        """在多个代理接口中测速，选用最快且可用的那个。"""
        endpoints = self._proxy_endpoints()
        if not endpoints or self.http is None:
            return ""

        now = time.time()
        cached = self._proxy_endpoint_cache
        cached_endpoint = cached[0] if cached else ""
        if cached_endpoint and now - cached[1] < 600:
            for _, url in endpoints:
                if url == cached_endpoint:
                    proxied = await self._proxy_media_url(url, video_url, target)
                    if proxied and await self._probe_proxy_url(proxied):
                        return proxied
                    logger.info("[bili] 缓存的代理接口失效，重新测速")
                    self._proxy_endpoint_cache = None
                    break

        async def attempt(name: str, api_url: str):
            started = time.monotonic()
            proxied = await self._proxy_media_url(api_url, video_url, target)
            if not proxied:
                return None
            if not await self._probe_proxy_url(proxied):
                return None
            return (time.monotonic() - started, name, api_url, proxied)

        results = await asyncio.gather(
            *(attempt(name, url) for name, url in endpoints),
            return_exceptions=True,
        )
        valid = [item for item in results if isinstance(item, tuple)]
        if not valid:
            logger.warning("[bili] 所有视频代理接口都不可用")
            return ""

        valid.sort(key=lambda item: item[0])
        elapsed, name, api_url, proxied = valid[0]
        self._proxy_endpoint_cache = (api_url, time.time())
        logger.info(
            "[bili] 代理接口测速：%s 最快（%.0fms），其余：%s",
            name,
            elapsed * 1000,
            "、".join(
                f"{item[1]} {item[0] * 1000:.0f}ms"
                for item in valid[1:]
            )
            or "无",
        )
        return proxied

    async def _download_video(
        self,
        video: BiliVideo,
        target: BiliTarget | None = None,
        qn_override: int | None = None,
    ) -> str | None:
        if self.http is None or self._data_dir is None:
            return None

        # 第三方解析接口（星之阁等）返回的 video.url 就是可直接下载的直链。
        # 优先下载它；只有第三方直链都失败时才回退到官方 playurl。
        provider_urls: list[str] = []
        if video.video_url:
            provider_urls.append(video.video_url)
        if not provider_urls and target is not None:
            try:
                provider_urls = await self._provider_video_urls(target)
            except Exception as exc:
                logger.info("[bili] 第三方接口获取视频直链失败：%s", self._brief(exc))
        if qn_override:
            await self._fill_play_url(video, qn_override)
            if video.video_url and video.video_url not in provider_urls:
                provider_urls.insert(0, video.video_url)
        elif not provider_urls and (video.bvid or video.aid):
            await self._fill_play_url(video)
            if video.video_url:
                provider_urls.append(video.video_url)
        if provider_urls:
            video = replace(video, video_url=provider_urls[0])
        if not video.video_url:
            logger.info("[bili] 没有可用的视频直链，跳过视频下载")
            return None

        max_mb = int(self.config.get("max_video_mb") or 0)
        max_bytes = max_mb * 1024 * 1024 if max_mb > 0 else 0
        headers = dict(BILI_HEADERS)
        headers["Accept"] = "video/*,*/*;q=0.8"
        if video.bvid:
            headers["Referer"] = f"https://www.bilibili.com/video/{video.bvid}"

        candidates: list[str] = []
        for url in provider_urls:
            for item in self._candidate_video_urls(url):
                if item not in candidates:
                    candidates.append(item)
        if not candidates:
            candidates = list(self._candidate_video_urls(video.video_url))
        candidates = self._add_video_proxy(candidates)
        tried_durl = False
        tried_providers = True  # 第三方直链已经在候选里了
        tried_proxy_api = False

        # 开关：直接跳过直连尝试，优先交给第三方下载代理
        if bool(self.config.get("video_proxy_first")) and self._proxy_endpoints():
            tried_proxy_api = True
            proxied = await self._resolve_proxy_url(video.video_url, target)
            if proxied:
                logger.info("[bili] 跳过直连，优先使用第三方下载代理：%s", proxied[:100])
                data = await self._fetch_media(proxied, headers, max_bytes)
                if (
                    data
                    and len(data) >= 64 * 1024
                    and self._looks_like_mp4(data)
                ):
                    path = self._data_dir / (
                        f"bili_{int(time.time())}_{random.randint(1000, 9999)}.mp4"
                    )
                    await asyncio.to_thread(path.write_bytes, data)
                    logger.info(
                        "[bili] 视频已下载：%.1fMB（第三方代理）",
                        len(data) / 1024 / 1024,
                    )
                    return str(path)
                logger.warning("[bili] 第三方代理下载失败，回退直连尝试")
            else:
                logger.warning("[bili] 第三方代理未返回地址，回退直连尝试")

        while True:
            for candidate in candidates:
                data = await self._fetch_media(candidate, headers, max_bytes)
                if not data:
                    continue
                # 关键校验：CDN 有时会返回 200 + 错误页、JSON，或者只返回
                # 几百字节的 DASH 初始化分片。直接发出去就是「1 秒黑屏」，
                # 所以必须确认是真正的、足够大的 MP4。
                if len(data) < 64 * 1024 or not self._looks_like_mp4(data):
                    logger.warning(
                        "[bili] 视频地址返回的不是完整 MP4（%d 字节，文件头 %s），"
                        "可能被 CDN 拦截或只返回了初始化分片，尝试下一个地址：%s",
                        len(data),
                        data[:12].hex(),
                        candidate[:100],
                    )
                    continue
                if max_bytes and len(data) > max_bytes:
                    logger.info(
                        "[bili] 视频 %.1fMB 超过 %dMB 上限，跳过",
                        len(data) / 1024 / 1024,
                        max_mb,
                    )
                    return None
                path = self._data_dir / (
                    f"bili_{int(time.time())}_{random.randint(1000, 9999)}.mp4"
                )
                await asyncio.to_thread(path.write_bytes, data)
                logger.info("[bili] 视频已下载：%.1fMB", len(data) / 1024 / 1024)
                return str(path)

            if not tried_durl:
                tried_durl = True
                durl_urls = await self._playurl_durl_urls(video)
                if durl_urls:
                    candidates = []
                    for item in durl_urls:
                        candidates.extend(self._candidate_video_urls(item))
                    candidates = self._add_video_proxy(candidates)
                    logger.info(
                        "[bili] 直链下载失败，改用 durl 地址重试（%d 个）",
                        len(candidates),
                    )
                    continue

            if not tried_providers and target is not None:
                tried_providers = True
                provider_urls = await self._provider_video_urls(target)
                if provider_urls:
                    candidates = []
                    for item in provider_urls:
                        candidates.extend(self._candidate_video_urls(item))
                    candidates = self._add_video_proxy(candidates)
                    logger.info(
                        "[bili] 官方直链不可用，改用第三方接口直链重试（%d 个）",
                        len(candidates),
                    )
                    continue

            if not tried_proxy_api and target is not None:
                tried_proxy_api = True
                if self._proxy_endpoints():
                    proxied = await self._resolve_proxy_url(
                        video.video_url, target
                    )
                    if proxied:
                        candidates = [proxied]
                        logger.info(
                            "[bili] 改用第三方下载代理重试：%s", proxied[:100]
                        )
                        continue

            break

        logger.warning("[bili] 所有视频地址都下载失败（可能被 B 站 CDN 拦截）")
        return None

    async def _collect_video_links(
        self, video: BiliVideo, target: BiliTarget
    ) -> list[tuple[str, str]]:
        """解析出可用于播放/下载的视频直链（不下载视频本身）。

        * 在线播放：B 站签名直链，浏览器可以直接播放（代理链接带
          `Content-Disposition: attachment`，只会触发下载）
        * 下载：第三方代理直链

        由 `link_output` 控制输出哪种，默认两种都输出。
        """
        if self.http is None:
            return []
        self._ensure_cid(video)
        qn = int(self.config.get("video_quality") or 64)
        mode = str(self.config.get("link_output") or "both").strip().lower()
        links: list[tuple[str, str]] = []
        seen: set[str] = set()

        def add(label: str, url: str) -> None:
            if not url or not url.startswith(("http://", "https://")):
                return
            if url in seen:
                return
            seen.add(url)
            links.append((label, url))

        if not video.video_url:
            await self._fill_play_url(video)

        bili_mp4 = ""
        if mode in ("play", "both"):
            try:
                bili_mp4 = await self._request_play_url(video, qn, 1)
            except Exception as exc:
                logger.info("[bili] 获取播放直链失败：%s", self._brief(exc))
            if not bili_mp4:
                bili_mp4 = video.video_url
            add("在线播放", bili_mp4)

        if mode in ("download", "both") and self._proxy_endpoints():
            try:
                proxied = await self._resolve_proxy_url(video.video_url, target)
                if proxied:
                    add("下载（第三方代理）", proxied)
            except Exception as exc:
                logger.info("[bili] 获取第三方代理直链失败：%s", self._brief(exc))

        # 都没拿到时回退 B 站直链
        if not links:
            if video.video_url:
                add("原始直链", video.video_url)
            try:
                durl = await self._request_play_url(video, qn, 1)
                add(f"{qn}P MP4", durl)
                dash = await self._request_play_url(video, qn, 16)
                add(f"{qn}P DASH", dash)
            except Exception as exc:
                logger.info("[bili] 获取视频直链失败：%s", self._brief(exc))
        return links[:3]

    async def _send_video_links(
        self, event: AstrMessageEvent, video: BiliVideo, target: BiliTarget
    ) -> None:
        """把解析出的视频直链作为单独一条消息发送。"""
        links = await self._collect_video_links(video, target)
        if not links:
            logger.info("[bili] 没有可用的视频直链")
            return
        body = "\n".join(f"{label}：{url}" for label, url in links)
        await self._send_plain(event, f"视频直链：\n{body}")

    async def build_chain(
        self, video: BiliVideo, target: BiliTarget, *, allow_video: bool = True
    ) -> tuple[list[Any], str | None]:
        chain: list[Any] = []
        if bool(self.config.get("send_cover", True)):
            cover = await self._cover_component(video)
            if cover is not None:
                chain.append(cover)

        text = self.render_card(video, target)
        if text:
            chain.append(Comp.Plain(text))

        # 只输出解析直链模式：直链由调用方单独发送，不混在卡片里
        temp_path: str | None = None
        if allow_video:
            temp_path = await self._download_video(video, target)
            if temp_path:
                # 不能在这里立刻删除：AstrBot 的 respond 阶段是在插件处理器
                # 结束后才真正发送，提前删文件会导致协议端 ENOENT。
                self._schedule_cleanup(temp_path, delay=600)
        return chain, temp_path

    # -- 发送 ---------------------------------------------------------------
    async def _send_chain(self, event: AstrMessageEvent, chain: list[Any]) -> bool:
        """直接发送消息链，并把底层协议错误暴露出来，便于降级处理。"""
        if not chain:
            return True
        try:
            await event.send(MessageChain(chain))
            return True
        except Exception as exc:
            logger.warning("[bili] 发送消息失败：%s", self._brief(exc))
            return False

    async def _send_plain(self, event: AstrMessageEvent, text: str) -> bool:
        return await self._send_chain(event, [Comp.Plain(text)])

    async def _try_lower_quality(
        self,
        video: BiliVideo | None,
        target: BiliTarget | None,
        max_b64: int,
    ) -> bytes | None:
        """base64 发送前，文件太大就降低清晰度重新下载。"""
        if video is None or target is None:
            return None
        for qn in (32, 16):
            try:
                path = await self._download_video(video, target, qn_override=qn)
            except Exception as exc:
                logger.info("[bili] 降清晰度 qn=%s 下载失败：%s", qn, self._brief(exc))
                continue
            if not path:
                continue
            try:
                size = Path(path).stat().st_size
                if size <= max_b64:
                    data = await asyncio.to_thread(Path(path).read_bytes)
                    logger.info(
                        "[bili] 为 base64 发送降清晰度到 qn=%s（%.1fMB）",
                        qn,
                        size / 1024 / 1024,
                    )
                    return data
                logger.info(
                    "[bili] qn=%s 仍有 %.1fMB，继续降低清晰度",
                    qn,
                    size / 1024 / 1024,
                )
            finally:
                self._schedule_cleanup(path, 600)
        return None

    async def _send_video(
        self,
        event: AstrMessageEvent,
        temp_path: str,
        video: BiliVideo | None = None,
        target: BiliTarget | None = None,
    ) -> None:
        """发送视频：本地路径优先，失败再退回 base64（必要时自动降清晰度）。

        QQ / OneBot 要求 video 段单独成条；且把几十 MB 的视频塞进 base64
        再走 WebSocket，容易被协议端截断，表现为「只有 1 秒且全黑」。
        所以默认先发本地路径（AstrBot 在配置 callback_api_base 时会自动
        转成 HTTP 链接），只有路径发送真的报错时才退回 base64。
        """
        mode = str(self.config.get("video_send_mode") or "auto").strip().lower()
        try:
            size = Path(temp_path).stat().st_size
        except OSError:
            size = 0
        max_b64_raw = self.config.get("video_base64_max_mb")
        if max_b64_raw is None or str(max_b64_raw).strip() == "":
            max_b64_mb = 20
        else:
            try:
                max_b64_mb = max(0, int(max_b64_raw))
            except (TypeError, ValueError):
                max_b64_mb = 20
        max_b64 = max_b64_mb * 1024 * 1024

        if mode in ("auto", "file"):
            if await self._send_chain(
                event, [Comp.Video(file=temp_path, path=temp_path)]
            ):
                return
            logger.warning("[bili] 本地路径发送视频失败，尝试 base64 兜底")

        if mode in ("auto", "base64"):
            data: bytes | None = None
            if size and size > max_b64:
                data = await self._try_lower_quality(video, target, max_b64)
            if data is None and (not size or size <= max_b64):
                try:
                    data = await asyncio.to_thread(Path(temp_path).read_bytes)
                except OSError as exc:
                    logger.warning("[bili] 读取视频文件失败：%s", exc)
            if data is None and size and size > max_b64:
                logger.warning(
                    "[bili] 视频 %.1fMB 超过 base64 上限 %dMB，且降清晰度失败；"
                    "请配置 callback_api_base 让视频走 HTTP 链接",
                    size / 1024 / 1024,
                    max_b64_mb,
                )
            if data is not None:
                logger.info(
                    "[bili] 视频改用 base64 发送（%.1fMB）",
                    len(data) / 1024 / 1024,
                )
                if await self._send_chain(
                    event,
                    [
                        Comp.Video(
                            file="base64://" + base64.b64encode(data).decode()
                        )
                    ],
                ):
                    return

        await self._send_plain(
            event,
            "视频发送失败：协议端无法读取视频文件。"
            "请在 AstrBot 配置里设置 callback_api_base，"
            "或把视频发送方式改成 base64 并调大 video_base64_max_mb。",
        )

    def _schedule_cleanup(self, path: str, delay: float = 600) -> None:
        """延迟删除临时文件，给协议端留出读取时间。"""

        def _remove() -> None:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

        try:
            asyncio.get_running_loop().call_later(delay, _remove)
        except RuntimeError:
            _remove()


    # -- 事件 ---------------------------------------------------------------
    def _cooldown_hit(self, event: AstrMessageEvent, target: BiliTarget) -> bool:
        cooldown = int(self.config.get("cooldown_seconds") or 0)
        if cooldown <= 0:
            return False
        try:
            session_id = str(event.message_obj.session_id or "")
        except Exception:
            session_id = ""
        key = f"{event.get_platform_id()}:{session_id}:{target.key}"
        now = time.time()
        for old_key, stamp in list(self._recent.items()):
            if now - stamp > max(cooldown, 3600):
                self._recent.pop(old_key, None)
        last = self._recent.get(key)
        if last is not None and now - last < cooldown:
            return True
        self._recent[key] = now
        return False

    @staticmethod
    def _is_bili_command(text: str) -> bool:
        text = (text or "").strip()
        names = ("bili", "bili_status", "b站解析", "哔哩哔哩解析")
        for name in names:
            for candidate in (name, f"/{name}"):
                if text == candidate or text.startswith(f"{candidate} "):
                    return True
        return False

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        try:
            await self._handle_auto(event)
        except Exception:
            logger.exception("[bili] 自动解析失败")

    async def _handle_auto(self, event: AstrMessageEvent):
        if not self._initialized or self.http is None:
            return
        if not self.config.get("enabled", True):
            return
        if not self.config.get("auto_parse", True):
            return
        text = event.message_str or ""
        if not text or self._is_bili_command(text):
            return
        if str(event.get_sender_id()) == str(event.message_obj.self_id):
            return

        group = getattr(event.message_obj, "group", None)
        if (
            self.config.get("only_when_at")
            and group is not None
            and not getattr(event, "is_at_or_wake_command", False)
        ):
            return

        targets = await self.extract_targets(text)
        if not targets:
            return
        # v4.29 的语义：event.call_llm=True 才是「禁止默认 LLM」
        # （流水线条件是 `not event.call_llm`），所以这里必须设 True。
        if self.config.get("suppress_llm", True):
            try:
                event.call_llm = True
                event.should_call_llm(True)
            except Exception:
                pass

        limit = max(1, int(self.config.get("max_links_per_message") or 3))
        for target in targets[:limit]:
            if self._cooldown_hit(event, target):
                logger.info("[bili] 同链接仍在冷却期，跳过：%s", target.key)
                continue
            try:
                video = await self.fetch(target)
            except AllProvidersFailed as exc:
                logger.warning("[bili] 解析失败：%s", self._brief(exc))
                if self.config.get("show_error"):
                    await self._send_plain(
                        event, f"B站解析失败：{self._brief(exc)}"
                    )
                continue
            except Exception:
                logger.exception("[bili] 解析异常")
                continue
            attach_links = bool(self.config.get("attach_video_links"))
            chain, temp = await self.build_chain(
                video,
                target,
                allow_video=bool(self.config.get("send_video")) and not attach_links,
            )
            if not await self._send_chain(event, chain):
                # 图片发送失败时降级为纯文本卡片，至少把信息发出去
                await self._send_plain(event, self.render_card(video, target))
            if temp:
                # QQ 要求 video 段单独成条，不能和卡片混发
                await self._send_video(event, temp, video, target)
            elif self.config.get("send_video") and not attach_links:
                await self._send_plain(
                    event,
                    "视频下载失败：B 站 CDN 返回的不是有效视频（可能被拦截），"
                    "卡片信息已发送，详见插件日志。",
                )
            if attach_links:
                # 直链单独发一条，不和卡片混在一起
                await self._send_video_links(event, video, target)

    @filter.command("bili", alias={"b站解析", "哔哩哔哩解析"})
    async def bili(self, event: AstrMessageEvent, args: GreedyStr):
        """解析 B 站视频链接或 BV 号。"""
        if not self._initialized or self.http is None:
            await self._send_plain(event, "插件尚未初始化完成，请稍后再试。")
            return
        text = str(args or "").strip()
        if not text:
            await self._send_plain(event, self._help_text())
            return

        targets = await self.extract_targets(text)
        if not targets:
            await self._send_plain(
                event,
                "没有识别到 B 站视频链接。\n"
                "支持：bilibili.com/video/BV...、av...、b23.tv 短链、单独 BV 号。",
            )
            return

        limit = max(1, int(self.config.get("max_links_per_message") or 3))
        for target in targets[:limit]:
            try:
                video = await self.fetch(target)
            except AllProvidersFailed as exc:
                await self._send_plain(event, f"B站解析失败：{self._brief(exc)}")
                continue
            attach_links = bool(self.config.get("attach_video_links"))
            chain, temp = await self.build_chain(
                video,
                target,
                allow_video=bool(self.config.get("send_video")) and not attach_links,
            )
            if not await self._send_chain(event, chain):
                await self._send_plain(event, self.render_card(video, target))
            if temp:
                await self._send_video(event, temp, video, target)
            elif self.config.get("send_video") and not attach_links:
                await self._send_plain(
                    event,
                    "视频下载失败：B 站 CDN 返回的不是有效视频（可能被拦截），"
                    "卡片信息已发送，详见插件日志。",
                )
            if attach_links:
                await self._send_video_links(event, video, target)

    @filter.command("bili_status")
    async def bili_status(self, event: AstrMessageEvent):
        """查看 B 站解析接口状态。"""
        lines = [f"B站解析：{'开启' if self.config.get('enabled', True) else '关闭'}"]
        lines.append(
            f"自动解析：{'开启' if self.config.get('auto_parse', True) else '关闭'}"
        )
        lines.append(f"请求模式：{self.config.get('request_mode') or 'parallel'}")
        lines.append(f"链接冷却：{int(self.config.get('cooldown_seconds') or 0)} 秒")
        lines.append(f"已记录链接：{len(self._recent)} 条")
        lines.append("解析接口：")
        for index, provider in enumerate(self.providers, start=1):
            state = "✅" if provider.get("enabled", True) else "⏸"
            template = provider.get("url_template") or self._default_template(
                provider["type"]
            )
            host = ""
            if template:
                try:
                    host = urlparse(template).hostname or ""
                except Exception:
                    host = ""
            lines.append(
                f"{index}. {state} {provider['name']}（{provider['type']}）{host}"
            )
        if not self.providers:
            lines.append("（没有配置任何接口）")
        return event.plain_result("\n".join(lines))

    @staticmethod
    def _help_text() -> str:
        return (
            "B站视频解析\n"
            "自动识别聊天里的 B 站链接并发送图文卡片；也可以用指令手动解析。\n\n"
            "指令：\n"
            "/bili <链接或 BV 号> — 手动解析\n"
            "/bili_status — 查看解析接口状态\n\n"
            "支持：bilibili.com/video/BV...、av...、b23.tv 短链、?p= 分P、单独 BV 号。\n"
            "解析接口可在 WebUI 插件配置里增删，支持并行竞速与顺序回退。"
        )






