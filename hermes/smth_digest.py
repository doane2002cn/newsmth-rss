#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""smth_digest.py — 《水木社区每日推荐》→ 飞书 interactive 卡片

数据源: https://doane2002cn.github.io/newsmth-rss/
  all.xml                 合并流(200 条, 按发布时间倒序)
  feeds/topten.xml        今日十大
  feeds/newexpress.xml    水木特快
  feeds/shida.xml         近期热帖
  feeds/pictures.xml      精彩贴图
  sources.json            板块清单(用于把帖子归类到讨论区)
上游: GitHub Actions `25 * * * *`(TZ=Asia/Shanghai) 重建 feed。

契约:
  · 成功 → stdout 为空(no_agent 任务保持静默)
  · 失败 → stdout 打印一行提示(会被投递给用户), 退出码 0
用法:
  python3 smth_digest.py            正常：拉数据 → 发卡
  python3 smth_digest.py --dry-run  不发送，只打印卡片摘要并校验链接(自检用)
"""
import json
import html
import hashlib
import os
import re
import secrets
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

BASE = "https://doane2002cn.github.io/newsmth-rss"
FEED_ALL = f"{BASE}/all.xml"
FEED_TOP = f"{BASE}/feeds/topten.xml"
FEED_NEW = f"{BASE}/feeds/newexpress.xml"
FEED_SHI = f"{BASE}/feeds/shida.xml"
FEED_PIC = f"{BASE}/feeds/pictures.xml"
SOURCES_JSON = "https://raw.githubusercontent.com/doane2002cn/newsmth-rss/main/sources.json"
BOARDS_JSON = f"{BASE}/boards.json"

ENV_FILE = "/opt/data/.env"
CARD_API = "/opt/data/workspace/scripts/feishu_card_api.py"
CARD_TMP_DIR = "/tmp"
CARD_FILE_PREFIX = "smth_card_"
CHAT = "oc_b8559c55f250b54d396a8d8105bb1355"
CST = timezone(timedelta(hours=8))
SNAPSHOT_SUMMARY_LIMIT = 1200
DEFAULT_GATEWAY_BASE = "https://smth-go.12879737.xyz"
FEISHU_TOKEN_URL = (
    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
)
FEISHU_IMAGE_URL = "https://open.feishu.cn/open-apis/im/v1/images"
FEISHU_JUMP_QUERY = (
    "lk_jump_to_browser=true&lk_mobile_jump_to_browser=true"
)
IMAGE_CACHE_TTL = 7 * 24 * 3600
IMAGE_DOWNLOAD_TIMEOUT = 5
IMAGE_UPLOAD_TIMEOUT = 12
IMAGE_TOTAL_BUDGET = 20
IMAGE_MIN_REQUEST_TIMEOUT = 0.25
IMAGE_MAX_BYTES = 10 * 1024 * 1024
IMAGE_MAX_DIMENSION = 12000
IMAGE_GIF_MAX_DIMENSION = 2000
IMAGE_LIMIT = 8
IMAGE_HOSTS = {
    "static.mysmth.net",
    "www.newsmth.net",
    "m.newsmth.net",
    "att.newsmth.net",
}

MAX_PER_SECTION = 8        # 特快 / 知性感性 每节最多几条
MAX_PICTURES = 5
POOL_LIMIT = 16            # 交给 AI 挑选的候选池大小
MAX_READS = 3

# 12:10 起，若合并流还是上一小时的版本，最多轮询等到 12:25
WAIT_UNTIL = (12, 25)
POLL_S = 60

# 纯系统贴/无信息量贴，不参与推荐
NOISE_RE = re.compile(r"版面积分变更记录|^\[?公告\]?|^\[?敬告\]?|^\[?版规\]?|合集$")

LLM_URL = "https://openrouter.ai/api/v1/chat/completions"
# 策略: 全部免费模型, 按 2026-10-15 实测稳定性排序, 禁止付费模型。
# 注意: 推理型模型会把 max_tokens 烧在思考上, 留足额度否则返回空内容。
LLM_MODELS = (
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",   # 实测最稳, ~66s
    "nvidia/nemotron-3-ultra-550b-a55b:free",               # 实测可用, ~37s
    "openrouter/free",                                      # 兜底, 偶发空输出
)
LLM_KEYS = ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY_2", "OPENROUTER_API_KEY_3")

# 读者画像：决定"什么值得推荐"。改这里就能调整偏好，不必动逻辑。
READER = ("读者是一位爱折腾 AI 工作流、自建服务和硬件的技术人："
          "日常关注大模型与 Agent、NAS/家庭服务器/自托管、数码硬件与消费电子、"
          "编程与开发效率、网络与云服务，也关心职场发展、经济投资、健康和生活消费。")

PREFER = ("挑选时按以下优先级判断：\n"
          "高优先：AI 与大模型、自建服务/家庭实验室、数码硬件、编程与工具、"
          "职场与职业选择、经济与投资、健康与生活消费，以及有实操细节、数据或明确观点的分享。\n"
          "低优先或排除：纯情绪发泄与对骂、地域或人身攻击、标题党、只有标题没有正文的打卡贴、"
          "无观点的纯转贴、重复灌水、明显与读者无关的琐碎八卦。")

SYSTEM = (f"你是水木社区的资讯编辑，服务一位读者：{READER}"
          "你的任务是从候选帖里挑出他最值得看的几条，并给出具体、不空泛的理由。"
          "只输出 JSON，不要解释、不要代码块围栏。")


class _ImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "img":
            return
        src = dict(attrs).get("src")
        if src:
            self.sources.append(src)


def _allowed_image_url(url):
    """只返回水木图片白名单内的干净 HTTP(S) URL。"""
    try:
        parsed = urllib.parse.urlparse(url or "")
        port = parsed.port
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in ("http", "https")
        or host not in IMAGE_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    if port not in (None, default_port):
        return ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
    )


# ── 基础设施 ────────────────────────────────────────────────────────────────
def load_env():
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                os.environ.setdefault(k.strip(), v)
    except Exception as e:                                     # noqa: BLE001
        print(f"[warn] 读取 .env 失败: {e}", file=sys.stderr)


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "smth-digest/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def extract_image_urls(raw_html, article_url):
    """从 RSS 原始描述提取可用首图，必须在清洗 HTML 之前调用。"""
    parser = _ImageParser()
    try:
        parser.feed(raw_html or "")
    except Exception:                                          # noqa: BLE001
        return []

    urls = []
    for src in parser.sources:
        absolute = urllib.parse.urljoin(article_url, html.unescape(src).strip())
        clean = _allowed_image_url(absolute)
        if not clean:
            continue
        parsed = urllib.parse.urlparse(clean)
        path = parsed.path.lower()
        if "/img/face/" in path or "/emotion/" in path or "/smiley/" in path:
            continue
        if clean not in urls:
            urls.append(clean)
    return urls


def parse_feed(raw):
    """返回 (items, last_build)。item 保留正文图片 URL 和 RSS 摘要。"""
    root = ET.fromstring(raw)
    chan = root.find("channel") if root.find("channel") is not None else root
    last_build = None
    lb = chan.findtext("lastBuildDate")
    if lb:
        try:
            last_build = parsedate_to_datetime(lb.strip()).astimezone(CST)
        except Exception:                                      # noqa: BLE001
            last_build = None
    items = []
    for it in chan.findall("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not title or not link:
            continue
        pub = it.findtext("pubDate") or ""
        try:
            dt = parsedate_to_datetime(pub).astimezone(CST)
        except Exception:                                      # noqa: BLE001
            dt = None
        raw_desc = it.findtext("description") or ""
        image_urls = extract_image_urls(raw_desc, link)
        body = re.sub(r"(?i)<br\s*/?>", "\n", raw_desc)
        body = re.sub(r"<[^>]+>", " ", body)
        body = html.unescape(body).replace("\xa0", " ")
        kept = []
        for ln in body.split("\n"):
            ln = ln.strip()
            if not ln:
                continue
            # 站内头部
            if re.match(r"^(发信人|信区|标\s*题|发信站|来\s*源)\s*[:：]", ln):
                continue
            # 签名与来源，出现即代表正文已结束
            if ln.startswith("--") or ln.startswith("※") or ln.startswith("发自"):
                break
            kept.append(ln)
        body = re.sub(r"\s+", " ", " ".join(kept)).strip()
        body = re.sub(r"\[FROM:\s*[^\]]*\]\s*$", "", body).strip()
        m = re.search(r"/article/([^/]+)/", link)
        items.append({
            "title": title, "link": link,
            "guid": (it.findtext("guid") or link).strip(),
            "board": urllib.parse.unquote(m.group(1)) if m else "",
            "pub": pub, "ts": dt.timestamp() if dt else 0,
            "author": (it.findtext("author") or "").strip(),
            "desc": body[:SNAPSHOT_SUMMARY_LIMIT],
            "image_urls": image_urls,
        })
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items, last_build


def load_sections():
    """板块代码 → (讨论区, 中文名)。

    sources.json 给出我们订阅的 41 个源；boards.json 是站点全量板块名表，
    用来给跨板块条目（今日十大、精彩贴图）标注来源。
    """
    out = {}
    try:
        cfg = json.loads(fetch(SOURCES_JSON).decode("utf-8"))
        out = {s["board"]: (s.get("section") or "", s.get("name") or s["board"])
               for s in cfg["sources"] if s.get("board")}
    except Exception as e:                                     # noqa: BLE001
        print(f"[warn] sources.json 读取失败，跳过讨论区分组: {e}", file=sys.stderr)
    try:
        bd = json.loads(fetch(BOARDS_JSON).decode("utf-8")).get("boards") or {}
        for code, name in bd.items():
            if code not in out:
                out[code] = ("", name)
    except Exception as e:                                     # noqa: BLE001
        print(f"[warn] boards.json 读取失败，跨板块条目不标来源: {e}", file=sys.stderr)
    return out


def board_label(sections, board):
    name = sections.get(board, ("", ""))[1]
    return name or board


def wait_for_fresh():
    """等上游 GitHub Actions 把 RSS 重建到当前小时；超时则照发并标注延迟。"""
    uh, um = WAIT_UNTIL
    while True:
        raw = fetch(FEED_ALL)
        items, last_build = parse_feed(raw)
        now = datetime.now(CST)
        fresh = bool(last_build and last_build.hour == now.hour
                     and last_build.date() == now.date())
        deadline = now.replace(hour=uh, minute=um, second=0, microsecond=0)
        if fresh or now >= deadline:
            return items, last_build, (not fresh)
        print(f"[info] 上游还是 {last_build:%H:%M} 的版本，等 {POLL_S}s 重试",
              file=sys.stderr)
        time.sleep(POLL_S)


# ── 选取 ────────────────────────────────────────────────────────────────────
def is_noise(it):
    return bool(NOISE_RE.search(it["title"]))


def dedupe(items):
    """按 guid 和归一化标题双重去重（近期热帖与板块流会出现同一帖的两种 guid）。"""
    seen, out = set(), []
    for it in items:
        tk = re.sub(r"^\[[^\]]{1,12}\]\s*", "", it["title"]).strip()
        if it["guid"] in seen or (tk and f"t:{tk}" in seen):
            continue
        seen.add(it["guid"])
        if tk:
            seen.add(f"t:{tk}")
        if not is_noise(it):
            out.append(it)
    return out


def diversify(items, per_board=2, limit=None):
    """同一板块最多取 per_board 条，避免活跃版刷屏。"""
    counts, out = {}, []
    for it in items:
        b = it["board"]
        if counts.get(b, 0) >= per_board:
            continue
        counts[b] = counts.get(b, 0) + 1
        out.append(it)
        if limit and len(out) >= limit:
            break
    return out


def clip(s, limit):
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    head = s[:limit]
    m = list(re.finditer(r"[。！？；…]", head))
    if m and m[-1].end() >= limit * 0.5:
        return head[:m[-1].end()]
    return head.rstrip("，、。 ") + "…"


def strip_board_prefix(title):
    """去掉「[板块名] 标题」里的前缀，让各条标题样式统一。"""
    return re.sub(r"^\s*\[[^\]]{1,12}\]\s*", "", title or "").strip()


def article_identity(it):
    """返回水木文章身份，不从标题或 guid 猜测板块和主题号。"""
    match = re.search(r"/article/([^/?#]+)/(\d+)", it.get("link") or "")
    if not match:
        raise RuntimeError(f"无法识别水木文章链接: {it.get('link') or ''}")
    return urllib.parse.unquote(match.group(1)), match.group(2)


def gateway_link(it):
    """生成稳定、可在未来无痛升级 App Link 的私有跳转链接。"""
    board, topic = article_identity(it)
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", board):
        raise RuntimeError(f"非法板块代码: {board!r}")
    public_base = os.environ.get(
        "SMTH_GATEWAY_BASE", DEFAULT_GATEWAY_BASE
    ).strip().rstrip("/")
    parsed = urllib.parse.urlparse(public_base)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("SMTH_GATEWAY_BASE 必须是 HTTPS 绝对地址")
    quoted_board = urllib.parse.quote(board, safe="")
    return f"{public_base}/r/{quoted_board}/{topic}?{FEISHU_JUMP_QUERY}"


# 飞书会把行首的普通空格吃掉，缩进只能靠可见字符表达；
# 这里用全角空格 + 中点，即使空格被吞掉也能看出这是子行。
SUB = "　· "


def sub_line(it, sections, with_board=True, limit=48):
    """条目下方的副行：来源板块 + 正文摘要（互相重复时不重复展示）。"""
    bits = []
    if with_board:
        label = board_label(sections, it["board"])
        if label:
            bits.append(label)
    desc = it["desc"]
    if desc and re.sub(r"\s+", "", strip_board_prefix(desc)) == \
            re.sub(r"\s+", "", strip_board_prefix(it["title"])):
        desc = ""
    if desc:
        bits.append(clip(desc, limit))
    return SUB + " · ".join(bits) if bits else ""


# ── AI 摘要 / 推荐理由 ──────────────────────────────────────────────────────
def build_prompt(pool, stamp, sections):
    lines = [
        f"今日日期: {stamp}", "",
        READER, "",
        PREFER, "",
        "下面是今天从水木社区各板块抓到的候选帖（已剔除公告与系统贴）：", "",
    ]
    for i, it in enumerate(pool, 1):
        lines.append(f"[{i}] ({board_label(sections, it['board'])}) {it['title']}")
        if it["desc"]:
            lines.append(f"    摘要: {it['desc'][:120]}")
    lines += [
        "",
        "请输出 JSON（不要其它文字）：",
        '{"must_read":[{"n":<池中编号>,"why":"一句推荐理由，≤40字，要具体说清看点，'
        '禁止\\"值得一读/内容不错\\"这类空话"},共3条],'
        '"summary":"2-3句今日总结，≤120字，点出水木今天最值得注意的话题走向"}',
        "",
        "要求：must_read 必须来自上面的池子，按读者偏好从高到低排序，"
        "尽量覆盖不同方向（技术 / 职场 / 生活 / 经济），编号不要重复；宁可少选也不要凑数。",
    ]
    return "\n".join(lines)


def parse_json(txt):
    if not txt:
        return None
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:                                          # noqa: BLE001
        return None
    picks = d.get("must_read")
    if not isinstance(picks, list) or not picks:
        return None
    ok = [p for p in picks if isinstance(p, dict) and str(p.get("n", "")).strip().isdigit()]
    if not ok:
        return None
    d["must_read"] = ok[:MAX_READS]
    d["summary"] = (d.get("summary") or "").strip()
    return d


def free_models():
    """硬性只允许 OpenRouter 免费档：`:free` 后缀，或 `openrouter/free` 路由。

    写错模型名（比如漏掉 `:free`）会在这里被拦下，避免误用付费模型。
    """
    allowed = [m for m in LLM_MODELS if m == "openrouter/free" or m.endswith(":free")]
    dropped = [m for m in LLM_MODELS if m not in allowed]
    if dropped:
        print(f"[warn] 已忽略非免费模型: {dropped}", file=sys.stderr)
    return allowed


def call_llm(prompt):
    """失败返回 None（调用方走兜底）。只走免费模型，失败的依次往下试。"""
    body = {"messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": prompt}],
            "temperature": 0.3, "max_tokens": 1600}
    last_err = None
    for model in free_models():
        for key in LLM_KEYS:
            k = os.environ.get(key)
            if not k:
                continue
            body["model"] = model
            payload = json.dumps(body).encode()
            for attempt in (1, 2):
                req = urllib.request.Request(LLM_URL, data=payload, headers={
                    "Authorization": f"Bearer {k}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": BASE,
                    "X-Title": "SMTH Digest",
                })
                try:
                    with urllib.request.urlopen(req, timeout=90) as r:
                        d = json.loads(r.read().decode())
                    ch = (d.get("choices") or [{}])[0]
                    txt = (ch.get("message") or {}).get("content")
                    if not txt:
                        last_err = f"{model} 空输出(finish={ch.get('finish_reason')})"
                        break
                    out = parse_json(txt)
                    if out:
                        print(f"[info] LLM={model} key={key}", file=sys.stderr)
                        return out
                    last_err = f"{model} JSON 解析失败"
                    break
                except urllib.error.HTTPError as e:
                    last_err = f"{model} HTTP {e.code}"
                    if e.code == 429 and attempt == 1:
                        import time
                        time.sleep(20)
                        continue
                    break
                except Exception as e:                         # noqa: BLE001
                    last_err = f"{model} {type(e).__name__}: {e}"
                    break
    print(f"[warn] AI 不可用({last_err})，改用原始摘要兜底", file=sys.stderr)
    return None


def fallback_reads(pool):
    return [{"n": i + 1,
             "why": clip(it["desc"], 50) if it["desc"] else "原始摘要（AI 不可用）"}
            for i, it in enumerate(pool[:MAX_READS])]


# ── 飞书首图 ────────────────────────────────────────────────────────────────
_feishu_token_state = {"value": "", "expires": 0.0}


def _image_cache_file():
    return os.environ.get(
        "SMTH_IMAGE_CACHE_FILE",
        "/opt/data/scripts/state/smth_image_cache.json",
    )


def load_image_cache():
    path = _image_cache_file()
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    now = time.time()
    return {
        str(key): value
        for key, value in payload.items()
        if isinstance(value, dict)
        and value.get("image_key")
        and float(value.get("expires", 0)) > now
    }


def save_image_cache(cache):
    path = _image_cache_file()
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".smth-image-cache-",
            dir=directory,
            delete=False,
        ) as f:
            temp_path = f.name
            json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except Exception as exc:                                   # noqa: BLE001
        print(f"[warn] 飞书图片缓存写入失败: {type(exc).__name__}", file=sys.stderr)


def safe_image_url(url):
    return _allowed_image_url(url)


def image_kind(payload, content_type=""):
    """只接受飞书接口允许的真实图片格式，不信任扩展名或响应头。"""
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if payload.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if payload.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff", "image/tiff"
    if payload.startswith(b"BM"):
        return ".bmp", "image/bmp"
    if payload.startswith(b"\x00\x00\x01\x00"):
        return ".ico", "image/x-icon"
    raise ValueError(f"不支持的图片格式: {content_type or 'unknown'}")


def _jpeg_dimensions(payload):
    """读取 JPEG SOF 头；只解析结构，不解码像素。"""
    sof_markers = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    offset = 2
    while offset + 1 < len(payload):
        if payload[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if offset + 2 > len(payload):
            break
        segment_length = int.from_bytes(payload[offset:offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(payload):
            break
        if marker in sof_markers and segment_length >= 7:
            height = int.from_bytes(payload[offset + 3:offset + 5], "big")
            width = int.from_bytes(payload[offset + 5:offset + 7], "big")
            return width, height
        offset += segment_length
    raise ValueError("JPEG 缺少有效尺寸")


def _webp_dimensions(payload):
    offset = 12
    while offset + 8 <= len(payload):
        chunk = payload[offset:offset + 4]
        size = int.from_bytes(payload[offset + 4:offset + 8], "little")
        data_start = offset + 8
        data_end = min(len(payload), data_start + size)
        data = payload[data_start:data_end]
        if chunk == b"VP8X" and len(data) >= 10:
            width = int.from_bytes(data[4:7], "little") + 1
            height = int.from_bytes(data[7:10], "little") + 1
            return width, height
        if chunk == b"VP8 ":
            marker = data.find(b"\x9d\x01\x2a")
            if marker >= 0 and marker + 7 <= len(data):
                width = int.from_bytes(data[marker + 3:marker + 5], "little")
                height = int.from_bytes(data[marker + 5:marker + 7], "little")
                return width & 0x3FFF, height & 0x3FFF
        if chunk == b"VP8L" and len(data) >= 5 and data[0] == 0x2F:
            bits = int.from_bytes(data[1:5], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        offset = data_start + size + (size & 1)
    raise ValueError("WebP 缺少有效尺寸")


def _tiff_dimensions(payload):
    endian = "<" if payload[:2] == b"II" else ">"
    if len(payload) < 8:
        raise ValueError("TIFF 头不完整")
    ifd_offset = struct.unpack_from(f"{endian}I", payload, 4)[0]
    if ifd_offset + 2 > len(payload):
        raise ValueError("TIFF IFD 越界")
    count = struct.unpack_from(f"{endian}H", payload, ifd_offset)[0]
    values = {}
    for index in range(count):
        entry = ifd_offset + 2 + index * 12
        if entry + 12 > len(payload):
            break
        tag, value_type = struct.unpack_from(f"{endian}HH", payload, entry)
        if tag not in (0x0100, 0x0101):
            continue
        if value_type == 3:
            values[tag] = struct.unpack_from(f"{endian}H", payload, entry + 8)[0]
        elif value_type == 4:
            values[tag] = struct.unpack_from(f"{endian}I", payload, entry + 8)[0]
    if 0x0100 in values and 0x0101 in values:
        return values[0x0100], values[0x0101]
    raise ValueError("TIFF 缺少有效尺寸")


def image_dimensions(payload, extension):
    """从文件魔数对应结构读取宽高，不信任扩展名或响应头。"""
    if extension == ".png":
        if len(payload) < 24 or payload[12:16] != b"IHDR":
            raise ValueError("PNG 头不完整")
        return struct.unpack_from(">II", payload, 16)
    if extension == ".gif":
        if len(payload) < 10:
            raise ValueError("GIF 头不完整")
        return struct.unpack_from("<HH", payload, 6)
    if extension == ".jpg":
        return _jpeg_dimensions(payload)
    if extension == ".webp":
        return _webp_dimensions(payload)
    if extension == ".bmp":
        if len(payload) < 26:
            raise ValueError("BMP 头不完整")
        dib_size = struct.unpack_from("<I", payload, 14)[0]
        if dib_size == 12:
            return struct.unpack_from("<HH", payload, 18)
        if dib_size >= 40:
            width, height = struct.unpack_from("<ii", payload, 18)
            return abs(width), abs(height)
        raise ValueError("不支持的 BMP 头")
    if extension == ".tiff":
        return _tiff_dimensions(payload)
    if extension == ".ico":
        if len(payload) < 8 or payload[4:6] == b"\x00\x00":
            raise ValueError("ICO 头不完整")
        count = struct.unpack_from("<H", payload, 4)[0]
        dimensions = []
        for index in range(count):
            entry = 6 + index * 16
            if entry + 2 > len(payload):
                break
            dimensions.append((payload[entry] or 256, payload[entry + 1] or 256))
        if dimensions:
            return max(dimensions, key=lambda size: size[0] * size[1])
        raise ValueError("ICO 缺少图像目录")
    raise ValueError(f"不支持的图片扩展名: {extension}")


def validate_image_dimensions(payload, extension):
    width, height = image_dimensions(payload, extension)
    if width <= 0 or height <= 0:
        raise ValueError("图片尺寸无效")
    limit = (
        IMAGE_GIF_MAX_DIMENSION
        if extension == ".gif"
        else IMAGE_MAX_DIMENSION
    )
    if width > limit or height > limit:
        raise ValueError(f"图片尺寸 {width}x{height} 超过 {limit}px")
    return width, height


class _SafeImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    """允许水木图床内部重定向，但拒绝跳到白名单之外。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        absolute = urllib.parse.urljoin(req.full_url, newurl)
        safe = safe_image_url(absolute)
        if not safe:
            raise urllib.error.URLError("图片跨域重定向不在白名单")
        return super().redirect_request(req, fp, code, msg, headers, safe)


def _remaining_timeout(deadline, maximum):
    remaining = deadline - time.monotonic()
    if remaining < IMAGE_MIN_REQUEST_TIMEOUT:
        raise TimeoutError("图片处理总时长预算已耗尽")
    return min(float(maximum), remaining)


def download_image(url, timeout=IMAGE_DOWNLOAD_TIMEOUT):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "smth-digest-image/1.0",
            "Accept": "image/*",
            "Referer": "https://www.newsmth.net/",
        },
    )
    opener = urllib.request.build_opener(_SafeImageRedirectHandler())
    with opener.open(request, timeout=timeout) as response:
        content_length = int(response.headers.get("Content-Length") or 0)
        if content_length > IMAGE_MAX_BYTES:
            raise ValueError("图片超过 10MB")
        payload = response.read(IMAGE_MAX_BYTES + 1)
        if not payload or len(payload) > IMAGE_MAX_BYTES:
            raise ValueError("图片为空或超过 10MB")
        extension, content_type = image_kind(
            payload, response.headers.get("Content-Type", "")
        )
        validate_image_dimensions(payload, extension)
        return payload, extension, content_type


def feishu_tenant_token(timeout=15):
    now = time.monotonic()
    if _feishu_token_state["value"] and _feishu_token_state["expires"] > now:
        return _feishu_token_state["value"]
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID/FEISHU_APP_SECRET 未配置")
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    request = urllib.request.Request(
        FEISHU_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") != 0 or not result.get("tenant_access_token"):
        raise RuntimeError(f"飞书 token 获取失败: code={result.get('code')}")
    expire = max(60, int(result.get("expire") or 7200))
    _feishu_token_state["value"] = result["tenant_access_token"]
    _feishu_token_state["expires"] = now + expire - 120
    return _feishu_token_state["value"]


def upload_feishu_image(payload, extension, content_type, deadline=None):
    deadline = deadline or (time.monotonic() + IMAGE_TOTAL_BUDGET)
    token = feishu_tenant_token(timeout=_remaining_timeout(deadline, 15))
    boundary = f"----smth-{secrets.token_hex(16)}"
    filename = f"smth-image{extension}"
    chunks = [
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="image_type"\r\n\r\n',
        b"message\r\n",
        f"--{boundary}\r\n".encode(),
        (
            'Content-Disposition: form-data; name="image"; '
            f'filename="{filename}"\r\n'
        ).encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        payload,
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    request = urllib.request.Request(
        FEISHU_IMAGE_URL,
        data=b"".join(chunks),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    timeout = _remaining_timeout(deadline, IMAGE_UPLOAD_TIMEOUT)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    image_key = (result.get("data") or {}).get("image_key")
    if result.get("code") != 0 or not image_key:
        raise RuntimeError(f"飞书图片上传失败: code={result.get('code')}")
    return image_key


def prepare_card_images(items):
    """串行准备少量首图；任何失败都静默降级为无图卡片。"""
    if not items:
        return {}
    if not (
        os.environ.get("FEISHU_APP_ID", "").strip()
        and os.environ.get("FEISHU_APP_SECRET", "").strip()
    ):
        print("[info] 飞书应用凭证缺失，卡片使用纯文本降级", file=sys.stderr)
        return {}
    try:
        limit = max(0, min(12, int(os.environ.get("SMTH_CARD_IMAGE_LIMIT", "8"))))
    except ValueError:
        limit = IMAGE_LIMIT

    cache = load_image_cache()
    image_keys = {}
    seen_topics = set()
    started = time.monotonic()
    deadline = started + IMAGE_TOTAL_BUDGET
    failures = 0
    budget_exhausted = False
    for it in items:
        if len(image_keys) >= limit:
            break
        board, topic = article_identity(it)
        identity = f"{board}/{topic}"
        if identity in seen_topics or not it.get("image_urls"):
            continue
        seen_topics.add(identity)
        url = safe_image_url(it["image_urls"][0])
        if not url:
            continue
        cache_key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cached = cache.get(cache_key)
        if cached:
            image_keys[identity] = cached["image_key"]
            continue
        try:
            download_timeout = _remaining_timeout(deadline, IMAGE_DOWNLOAD_TIMEOUT)
            payload, extension, content_type = download_image(
                url, timeout=download_timeout
            )
        except TimeoutError as exc:
            if "预算" in str(exc):
                budget_exhausted = True
                break
            failures += 1
            print(
                f"[warn] 卡片配图跳过 {identity}: download_timeout",
                file=sys.stderr,
            )
            continue
        except (socket.timeout, urllib.error.URLError) as exc:
            failures += 1
            print(
                f"[warn] 卡片配图跳过 {identity}: download_error "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            continue
        except ValueError as exc:
            failures += 1
            print(
                f"[warn] 卡片配图跳过 {identity}: invalid_image_or_dim {exc}",
                file=sys.stderr,
            )
            continue
        except Exception as exc:                               # noqa: BLE001
            failures += 1
            print(
                f"[warn] 卡片配图跳过 {identity}: download_error "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            continue

        try:
            image_key = upload_feishu_image(
                payload, extension, content_type, deadline=deadline
            )
        except TimeoutError as exc:
            if "预算" in str(exc):
                budget_exhausted = True
                break
            failures += 1
            print(
                f"[warn] 卡片配图跳过 {identity}: feishu_upload_timeout",
                file=sys.stderr,
            )
            continue
        except Exception as exc:                               # noqa: BLE001
            failures += 1
            print(
                f"[warn] 卡片配图跳过 {identity}: feishu_upload_error "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
            continue

        image_keys[identity] = image_key
        cache[cache_key] = {
            "image_key": image_key,
            "expires": int(time.time()) + IMAGE_CACHE_TTL,
        }
    if budget_exhausted:
        print(
            "[info] 卡片配图达到总时长预算，余下条目改用纯文本",
            file=sys.stderr,
        )
    save_image_cache(cache)
    print(
        f"[info] 飞书卡片首图 {len(image_keys)} 张"
        f"（失败 {failures}，耗时 {time.monotonic() - started:.1f}s）",
        file=sys.stderr,
    )
    return image_keys


# ── 卡片 ────────────────────────────────────────────────────────────────────
def article_markdown(it):
    return (
        f"[{strip_board_prefix(it['title'])}]"
        f"({gateway_link(it)})"
    )


def article_image(it, image_keys):
    board, topic = article_identity(it)
    image_key = image_keys.get(f"{board}/{topic}")
    if not image_key:
        return None
    return {
        "tag": "img",
        "img_key": image_key,
        "alt": {"tag": "plain_text", "content": "帖子配图预览"},
        "mode": "fit_horizontal",
        "preview": True,
    }


def render_card(
    sec,
    reads,
    summary,
    last_build,
    ai_broken,
    pool,
    delayed,
    sections,
    image_keys=None,
):
    by_n = {i + 1: it for i, it in enumerate(pool)}
    image_keys = image_keys or {}
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    elements = []

    if ai_broken:
        elements.append({
            "tag": "markdown",
            "content": "⚠️ **AI 分析暂不可用，以下为原始摘要**",
        })

    # 今日必读：条目间留空行，避免摘要和下一条标题糊在一起
    elements.append({"tag": "markdown", "content": "**📌 今日必读**"})
    for i, r in enumerate(reads, 1):
        it = by_n.get(int(r["n"]))
        if not it:
            continue
        lines = [f"{i}. {article_markdown(it)}"]
        why = (r.get("why") or "").strip()
        if why:
            lines.append(f"{SUB}推荐理由：{clip(why, 46)}")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
        image = article_image(it, image_keys)
        if image:
            elements.append(image)
    if not reads:
        elements.append({"tag": "markdown", "content": "（今日无）"})

    md = []
    # 今日十大：标注来源板块 + 正文摘要
    top = sec["topten"]
    md.append(f"**🔥 今日十大**（{len(top)} 条）")
    for i, it in enumerate(top, 1):
        md.append(f"{i}. {article_markdown(it)}")
        line = sub_line(it, sections, limit=44)
        if line:
            md.append(line)
    if not top:
        md.append("（今日无）")
    md.append("")

    for emoji, title, key in (("📰", "水木特快", "newexpress"),
                              ("💬", "知性感性精选", "sensibility")):
        items = sec[key][:MAX_PER_SECTION]
        md.append(f"**{emoji} {title}**（{len(items)} 条）")
        for i, it in enumerate(items, 1):
            md.append(f"{i}. {article_markdown(it)}")
            line = sub_line(it, sections, with_board=(key == "sensibility"))
            if line:
                md.append(line)
        if not items:
            md.append("（今日无）")
        md.append("")
    elements.append({"tag": "markdown", "content": "\n".join(md).rstrip()})

    pics = sec["pictures"][:MAX_PICTURES]
    elements.append({
        "tag": "markdown",
        "content": f"**🖼 精彩贴图**（{len(pics)} 张）",
    })
    for i, it in enumerate(pics, 1):
        lines = [f"{i}. {article_markdown(it)}"]
        # 贴图重在图片本身；desc 里是图片地址，不能当摘要，只在有中文板块名时标来源
        if sections.get(it["board"], ("", ""))[1]:
            lines.append(SUB + board_label(sections, it["board"]))
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
        image = article_image(it, image_keys)
        if image:
            elements.append(image)
    if not pics:
        elements.append({"tag": "markdown", "content": "（今日无）"})

    footer = []
    if summary:
        footer += ["**🧭 今日总结**", summary, ""]

    ts = last_build.strftime("%Y-%m-%d %H:%M") if last_build else "未知"
    footer.append(f"**🕐 数据更新时间**：{ts}（北京时间）")
    if delayed:
        footer.append("⚠️ 数据可能延迟：上游 RSS 尚未重建到当前小时")
    elements.append({"tag": "markdown", "content": "\n".join(footer)})

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                             "content": f"水木社区每日推荐｜{stamp}"},
                   "template": "turquoise"},
        "elements": elements,
    }


def send_card(card):
    card_file = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix=CARD_FILE_PREFIX,
            dir=CARD_TMP_DIR,
            delete=False,
        ) as f:
            card_file = f.name
            json.dump(card, f, ensure_ascii=False)
        r = subprocess.run(
            ["python3", CARD_API, "--card-file", card_file, "--chat", CHAT],
            capture_output=True,
            text=True,
            timeout=90,
        )
        ok = r.returncode == 0 and "OK message_id" in (r.stdout or "")
        log = f"[info] 卡片已送达 {r.stdout.strip()}" if ok else \
              f"[warn] 卡片直发失败 rc={r.returncode} " \
              f"{r.stdout.strip()} {r.stderr.strip()}"
        print(log, file=sys.stderr)
        return ok
    finally:
        if card_file:
            try:
                os.unlink(card_file)
            except FileNotFoundError:
                pass


def validate_card(card):
    """确保卡片只包含自有 HTTPS 跳转链接，绝不把原链发出去。"""
    serialized = json.dumps(card, ensure_ascii=False)
    lowered = serialized.lower()
    if "smzdm.com" in lowered:
        raise RuntimeError("卡片中混入了 SMZDM 域名")
    if "newsmth.net" in lowered:
        raise RuntimeError("卡片中仍有未转换的水木域名")

    links = re.findall(r"https?://[^\"\\\s)]+", serialized)
    if not links:
        raise RuntimeError("卡片中没有找到文章链接")

    public_base = os.environ.get(
        "SMTH_GATEWAY_BASE", DEFAULT_GATEWAY_BASE
    ).strip().rstrip("/")
    base = urllib.parse.urlparse(public_base)
    for link in links:
        parsed = urllib.parse.urlparse(link)
        query = urllib.parse.parse_qs(parsed.query)
        if (
            parsed.scheme != "https"
            or parsed.netloc != base.netloc
            or not re.fullmatch(
                r"/r/[A-Za-z0-9_]{1,64}/\d{1,12}/?", parsed.path
            )
            or query.get("lk_jump_to_browser") != ["true"]
            or query.get("lk_mobile_jump_to_browser") != ["true"]
        ):
            raise RuntimeError(f"非自有水木跳转链接: {link}")
    return links


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    dry = "--dry-run" in sys.argv
    load_env()

    try:
        if dry:
            all_items, last_build = parse_feed(fetch(FEED_ALL))
            delayed = False
        else:
            all_items, last_build, delayed = wait_for_fresh()
        top_items, _ = parse_feed(fetch(FEED_TOP))
        new_items, _ = parse_feed(fetch(FEED_NEW))
        shi_items, _ = parse_feed(fetch(FEED_SHI))
        pic_items, _ = parse_feed(fetch(FEED_PIC))
    except Exception as e:                                     # noqa: BLE001
        print(f"⚠️ 水木社区每日推荐取数失败：{type(e).__name__} {e}")
        return 0

    sections = load_sections()
    today0 = datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def by_section(name):
        return [it for it in all_items
                if it["ts"] >= today0 and sections.get(it["board"], ("", ""))[0] == name]

    feeling = by_section("知性感性")
    if len(feeling) < 4:                       # 冷启动或凌晨跑，放宽到最近 24 小时
        cutoff = (datetime.now(CST) - timedelta(hours=24)).timestamp()
        feeling = [it for it in all_items
                   if it["ts"] >= cutoff
                   and sections.get(it["board"], ("", ""))[0] == "知性感性"]

    # 知性感性：先按板块均衡取样（每版最多 2 条防刷屏），再用近期热帖补位，
    # 最后按标题去重，避免同一帖在两条流里各出现一次。
    sec = {
        "topten": dedupe(top_items),
        "newexpress": dedupe(new_items)[:MAX_PER_SECTION],
        "sensibility": dedupe(diversify(feeling, per_board=2) + shi_items)[:MAX_PER_SECTION],
        "pictures": dedupe(pic_items),
    }
    print(f"[info] 合并流 {len(all_items)} / 十大 {len(sec['topten'])} / "
          f"特快 {len(sec['newexpress'])} / 知性 {len(sec['sensibility'])} / "
          f"贴图 {len(sec['pictures'])}", file=sys.stderr)

    # 候选池：跨板块取样，覆盖十大 / 特快 / 知性感性，
    # 否则最活跃的版会把池子占满，模型没得挑
    pool = dedupe(
        sec["topten"][:5]
        + sec["newexpress"][:6]
        + diversify(sec["sensibility"], per_board=1)[:5]
    )[:POOL_LIMIT]

    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    data = call_llm(build_prompt(pool, stamp, sections)) if pool else None
    ai_broken = data is None
    reads = fallback_reads(pool) if ai_broken else (data.get("must_read") or fallback_reads(pool))
    summary = "" if ai_broken else (data.get("summary") or "")

    image_items = []
    for read in reads:
        try:
            number = int(read["n"])
        except (KeyError, TypeError, ValueError):
            continue
        if 1 <= number <= len(pool):
            image_items.append(pool[number - 1])
    image_items += sec["pictures"][:MAX_PICTURES]
    image_keys = {} if dry else prepare_card_images(image_items)

    try:
        card = render_card(
            sec,
            reads,
            summary,
            last_build,
            ai_broken,
            pool,
            delayed,
            sections,
            image_keys,
        )
        links = validate_card(card)
    except RuntimeError as e:
        print(f"⚠️ 水木社区每日推荐卡片生成或链接校验失败：{e}")
        return 0
    if dry:
        print("─" * 28 + " CARD PREVIEW (dry-run) " + "─" * 28)
        print(json.dumps(card, ensure_ascii=False, indent=2))
        print("─" * 70)
        planned_images = sum(
            bool(it.get("image_urls")) for it in image_items[:IMAGE_LIMIT]
        )
        print(
            f"[dry-run] 未发送；已校验 {len(links)} 个跳转链接，"
            f"计划首图 {planned_images} 张"
        )
        return 0
    if not send_card(card):
        print("⚠️ 水木社区每日推荐：卡片发送失败（详见日志）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
