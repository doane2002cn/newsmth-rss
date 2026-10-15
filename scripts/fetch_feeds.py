#!/usr/bin/env python3
"""抓取水木社区 nForum 的 RSS 输出口，转码为 UTF-8 并重新发布。

设计要点：
- 只使用官方自带的 RSS 接口，不需要任何登录态或凭据。
- 串行请求 + 固定间隔，绝不并发，避免触发站点风控。
- 支持 If-Modified-Since，内容未变化时服务端返回 304，不产生正文传输。
- 每个源保留滚动历史（默认 60 条），输出时不因上游 20 条窗口而丢内容。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
FEED_DIR = DOCS / "feeds"
DATA = ROOT / "data"
STORE_FILE = DATA / "store.json"
BOARDS_FILE = DATA / "boards.json"
BOARDS_PUBLISH = DOCS / "boards.json"
SOURCES_FILE = ROOT / "sources.json"

# Windows 控制台默认 GBK，日志里的中文会变乱码
if sys.platform == "win32":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

BASE = "https://www.newsmth.net"
# 发布后的站点地址，用于生成绝对的 atom:link（相对 URI 不符合 RFC 4287，部分阅读器会拒收）
SITE_BASE = "https://doane2002cn.github.io/newsmth-rss"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

REQUEST_DELAY = 1.8       # 每个源之间的间隔（秒），务必保持串行
REQUEST_TIMEOUT = 40
STORE_CAP = 60            # 每个源保留多少条历史
FEED_ITEMS = 50           # 每个源输出多少条
COMBO_ITEMS = 200         # 合并流输出多少条
BODY_LIMIT = 1200         # 单条正文截断长度，控制仓库体积
MAX_FETCHES = 3           # 单个源的最大尝试次数
BOARD_TTL_DAYS = 7        # 板块名表多久重建一次
SECTION_IDS = [str(i) for i in range(10)] + ["Estate"]


class Banned(Exception):
    """站点返回封禁页，必须立即停止整轮抓取。"""


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, last_modified: str | None = None) -> tuple[int, bytes | None, dict]:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status == 304:
                return 304, None, hdrs
            return resp.status, resp.read(), hdrs
    except urllib.error.HTTPError as exc:
        body = b""
        try:
            body = exc.read() or b""
        except Exception:
            pass
        # 304 也会以 HTTPError 形式抛出，这是正常的"内容未变化"
        if exc.code == 304:
            return 304, None, {k.lower(): v for k, v in (exc.headers or {}).items()}
        if exc.code == 403 or "封禁" in body[:4096].decode("utf-8", "ignore"):
            raise Banned(f"HTTP {exc.code} from {url}") from exc
        raise


def decode_gb(raw: bytes) -> str:
    return raw.decode("gb18030", errors="replace")


def strip_xml_decl(text: str) -> str:
    return re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1)


def clean_body(raw_html: str) -> str:
    """去掉发信人/标题/发信站/签名/来源等站内头部，只留正文。"""
    if not raw_html:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", "\n", raw_html)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    out: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^(发信人|信区|标\s*题|发信站|来\s*源)\s*[:：]", line):
            continue
        if line.startswith("※") or line.startswith("[FROM:"):
            continue
        if re.match(r"^[-=]{2,}$", line):
            break
        line = re.sub(r"\[FROM:\s*[^\]]*\]\s*$", "", line).strip()
        if line:
            out.append(line)
    body = "\n".join(out)
    if len(body) > BODY_LIMIT:
        body = body[:BODY_LIMIT] + "…"
    return body


def to_html_text(text: str) -> str:
    if not text:
        return ""
    return "<br/>".join(html.escape(line) for line in text.split("\n"))


def cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def parse_board_rss(raw: bytes) -> list[dict]:
    """解析上游 RSS，产出统一的 item 结构。"""
    tree = ET.fromstring(strip_xml_decl(decode_gb(raw)))
    items: list[dict] = []
    for node in tree.iter("item"):
        def text(tag: str) -> str:
            el = node.find(tag)
            return (el.text or "").strip() if el is not None else ""

        link = text("link")
        if not link:
            continue
        pub = text("pubDate")
        try:
            dt = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            dt = None
        if dt is None:
            dt = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        body = clean_body(text("description"))
        items.append(
            {
                "guid": text("guid") or link,
                "title": text("title"),
                "link": link.replace("http://", "https://", 1),
                "author": text("author"),
                "pub_date": format_datetime(dt),
                "ts": int(dt.timestamp()),
                "body": body,
            }
        )
    return items


PIC_ITEM_RE = re.compile(r"<li>(.*?)</li>", re.S)
PIC_LINK_RE = re.compile(r'href="(/nForum/article/([A-Za-z0-9_.\-]+)/(\d+))"')
PIC_IMG_RE = re.compile(r'<img\s+src="([^"]+)"')
PIC_BOARD_RE = re.compile(r'class="board"[^>]*>\[?([^\]<]+)\]?')
PIC_TITLE_RE = re.compile(r'class="title"[^>]*>([^<]+)<')


def parse_mainpage_pictures(raw: bytes) -> list[dict]:
    """精彩贴图没有 RSS，只能解析首页的图片聚合块。"""
    page = decode_gb(raw)
    start = page.find('id="pictures"')
    if start < 0:
        return []
    block = page[start : start + 20000]
    items: list[dict] = []
    for li in PIC_ITEM_RE.findall(block):
        link_m = PIC_LINK_RE.search(li)
        if not link_m:
            continue
        path, board, _ = link_m.group(1), link_m.group(2), link_m.group(3)
        img_m = PIC_IMG_RE.search(li)
        board_m = PIC_BOARD_RE.search(li)
        title_m = PIC_TITLE_RE.search(li)
        title = html.unescape((title_m.group(1) if title_m else "").strip())
        board_name = html.unescape((board_m.group(1) if board_m else board).strip())
        if not title:
            continue
        img = (img_m.group(1) if img_m else "").strip()
        if img.startswith("//"):
            img = "https:" + img
        full = BASE + path
        items.append(
            {
                "guid": full,
                "title": f"[{board_name}] {title}",
                "link": full.replace("http://", "https://", 1),
                "author": board_name,
                "pub_date": format_datetime(datetime.now(timezone.utc)),
                "ts": int(datetime.now(timezone.utc).timestamp()),
                "body": img,
                "html": (f'<img src="{xml_escape(img)}" alt="" /><br/>' if img else "") + to_html_text(title),
            }
        )
    return items


def load_store() -> dict:
    if STORE_FILE.exists():
        try:
            return json.loads(STORE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("state: store.json 解析失败，重建")
    return {"sources": {}}


def build_board_names() -> dict[str, str]:
    """抓各讨论区页面，建立「板块代码 → 中文名」映射。

    卡片里要把跨板块条目标出来，而 RSS 本身只给板块代码，所以需要这张表。
    每个讨论区页面一次请求，一周重建一次。
    """
    mapping: dict[str, str] = {}
    for sec in SECTION_IDS:
        try:
            code, raw, _ = http_get(f"{BASE}/nForum/section/{sec}")
            if code != 200 or not raw:
                continue
            page = decode_gb(raw)
        except Exception as exc:  # noqa: BLE001
            log(f"  板块名表：section/{sec} 失败（{type(exc).__name__}）")
            continue
        for m in re.finditer(
            r'<a href="/nForum/board/([A-Za-z0-9_.\-]+)"[^>]*>([\s\S]*?)</a>', page
        ):
            board = m.group(1)
            title = re.sub(r"<[^>]+>", "", m.group(2))
            title = title.replace("[", "").replace("]", "").strip()
            if title and board not in mapping:
                mapping[board] = title
        time.sleep(1.5)
    return mapping


def ensure_board_names() -> dict[str, str]:
    """板块名表有缓存就直接用，超过 TTL 才重建。"""
    try:
        if BOARDS_FILE.exists():
            data = json.loads(BOARDS_FILE.read_text(encoding="utf-8"))
            built = datetime.fromisoformat(data.get("generated", ""))
            age = (datetime.now(timezone.utc) - built).total_seconds() / 86400
            if age < BOARD_TTL_DAYS and data.get("boards"):
                log(f"板块名表：复用缓存（{len(data['boards'])} 个板块，{age:.1f} 天前）")
                if not BOARDS_PUBLISH.exists():
                    BOARDS_PUBLISH.write_text(
                        json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                        encoding="utf-8",
                    )
                return data["boards"]
    except Exception:  # noqa: BLE001
        pass

    log("板块名表：重建中（每周一次，约 11 次请求）")
    mapping = build_board_names()
    if mapping:
        payload = {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "boards": mapping,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
        BOARDS_FILE.write_text(text, encoding="utf-8")
        BOARDS_PUBLISH.write_text(text, encoding="utf-8")
        log(f"板块名表：已更新 {len(mapping)} 个板块")
    return mapping


def merge_items(old: list[dict], new: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {it["guid"]: it for it in old}
    for it in new:
        prev = merged.get(it["guid"])
        # 同一条内容再次出现时保留首次见到的时间，避免它每次都被顶到合并流最前面
        if prev and prev.get("ts"):
            it = {**it, "ts": prev["ts"], "pub_date": prev["pub_date"]}
        merged[it["guid"]] = it
    ordered = sorted(merged.values(), key=lambda x: x.get("ts", 0), reverse=True)
    return ordered[:STORE_CAP]


def snapshot_items(old: list[dict], new: list[dict]) -> list[dict]:
    """快照型数据源（今日十大、精彩贴图）只保留本次抓到的内容，不做滚动累积。

    仍然沿用旧的时间戳，否则每次抓取都会把它们顶到合并流最前面。
    """
    prev_by_guid = {it["guid"]: it for it in old}
    out = []
    for it in new:
        prev = prev_by_guid.get(it["guid"])
        if prev and prev.get("ts"):
            it = {**it, "ts": prev["ts"], "pub_date": prev["pub_date"]}
        out.append(it)
    return out[:STORE_CAP]


def build_rss(source: dict, items: list[dict], self_url: str) -> str:
    now = format_datetime(datetime.now(timezone.utc))
    if source["type"] == "topten":
        link = f"{BASE}/nForum/topten"
    elif source["type"] == "mainpage-pictures":
        link = f"{BASE}/nForum/mainpage"
    else:
        link = f"{BASE}/nForum/board/{source.get('board', '')}"
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "  <channel>",
        f"    <title>{xml_escape('水木社区 - ' + source['name'])}</title>",
        f"    <link>{xml_escape(link)}</link>",
        f"    <description>{xml_escape(source['name'] + '（水木社区 newsmth.net）')}</description>",
        "    <language>zh-cn</language>",
        f"    <lastBuildDate>{now}</lastBuildDate>",
        "    <generator>newsmth-rss</generator>",
        f'    <atom:link href="{xml_escape(self_url)}" rel="self" type="application/rss+xml"/>',
    ]
    for it in items:
        desc = it.get("html") or to_html_text(it.get("body", ""))
        parts.append("    <item>")
        parts.append(f"      <title>{xml_escape(it['title'])}</title>")
        parts.append(f"      <link>{xml_escape(it['link'])}</link>")
        parts.append(f'      <guid isPermaLink="true">{xml_escape(it["guid"])}</guid>')
        if it.get("author"):
            parts.append(f"      <author>{xml_escape(it['author'])}</author>")
        parts.append(f"      <pubDate>{it['pub_date']}</pubDate>")
        if desc:
            parts.append(f"      <description>{cdata(desc)}</description>")
        parts.append("    </item>")
    parts += ["  </channel>", "</rss>", ""]
    return "\n".join(parts)


def render_index(results: list[dict], generated: str) -> str:
    rows = []
    for r in results:
        rows.append(
            "    <tr>"
            f'<td><a href="feeds/{r["key"]}.xml">{html.escape(r["name"])}</a></td>'
            f'<td class="muted">{html.escape(r.get("section") or "")}</td>'
            f'<td class="num">{r.get("count", 0)}</td>'
            f'<td class="muted">{html.escape(r.get("status", ""))}</td>'
            "</tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>水木社区 RSS</title>
<style>
 body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
        max-width: 880px; margin: 0 auto; padding: 24px 16px; color: #1a1a1a; line-height: 1.6; }}
 h1 {{ font-size: 22px; margin: 0 0 4px; }}
 p.sub {{ color: #666; font-size: 13px; margin: 0 0 20px; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
 th, td {{ border-bottom: 1px solid #e8e8e8; padding: 8px 10px; text-align: left; }}
 th {{ font-weight: 600; background: #fafafa; font-size: 13px; }}
 .muted {{ color: #888; font-size: 12px; }}
 .num {{ width: 64px; color: #555; }}
 a {{ color: #0b62c4; text-decoration: none; }}
 a:hover {{ text-decoration: underline; }}
 footer {{ margin-top: 24px; color: #999; font-size: 12px; }}
</style>
</head>
<body>
  <h1>水木社区 RSS</h1>
  <p class="sub">数据来自 newsmth.net 官方 RSS 输出口，最近生成：{html.escape(generated)}</p>
  <p class="sub"><a href="all.xml">全部板块合并流</a></p>
  <table>
    <thead><tr><th>板块</th><th>分类</th><th>条数</th><th>状态</th></tr></thead>
    <tbody>
{chr(10).join(rows)}
    </tbody>
  </table>
  <footer>本页仅为第三方聚合，内容版权归水木社区及原作者所有。</footer>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", help="只抓指定 key，逗号分隔（用于调试）")
    parser.add_argument("--limit", type=int, help="只抓前 N 个源（用于调试）")
    parser.add_argument("--delay", type=float, default=REQUEST_DELAY)
    args = parser.parse_args()

    config = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    sources = config["sources"]
    if args.only:
        wanted = {k.strip() for k in args.only.split(",") if k.strip()}
        sources = [s for s in sources if s["key"] in wanted]
    if args.limit:
        sources = sources[: args.limit]

    store = load_store()
    stored = store.setdefault("sources", {})
    FEED_DIR.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    # 卡片要用到板块中文名；有缓存基本零成本
    ensure_board_names()

    results: list[dict] = []
    banned = False

    for idx, src in enumerate(sources):
        key = src["key"]
        entry = stored.setdefault(key, {"last_modified": None, "items": []})
        status = ""

        if banned:
            status = "跳过（本轮已触发风控）"
        else:
            if src["type"] == "topten":
                url = f"{BASE}/nForum/rss/topten"
            elif src["type"] == "mainpage-pictures":
                url = f"{BASE}/nForum/mainpage"
            else:
                url = f"{BASE}/nForum/rss/board-{src['board']}"

            for attempt in range(1, MAX_FETCHES + 1):
                try:
                    # 快照型数据源必须每次拿全量，否则 304 会让旧条目一直留在快照里
                    last_mod = None if src.get("snapshot") else entry.get("last_modified")
                    code, raw, hdrs = http_get(url, last_mod)
                    if code == 304:
                        status = "304 未变化"
                        break
                    if src["type"] == "mainpage-pictures":
                        new_items = parse_mainpage_pictures(raw or b"")
                    else:
                        new_items = parse_board_rss(raw or b"")
                    if src.get("snapshot"):
                        entry["items"] = snapshot_items(entry.get("items", []), new_items)
                    else:
                        entry["items"] = merge_items(entry.get("items", []), new_items)
                    entry["last_modified"] = hdrs.get("last-modified")
                    status = f"更新 +{len(new_items)}"
                    break
                except Banned as exc:
                    log(f"!! 触发风控，立即终止整轮：{exc}")
                    banned = True
                    status = "风控中止"
                    break
                except Exception as exc:  # noqa: BLE001
                    if attempt == MAX_FETCHES:
                        status = f"失败：{type(exc).__name__}"
                        log(f"[{key}] 抓取失败：{exc}")
                    else:
                        time.sleep(5 * attempt)

        count = len(entry.get("items", []))
        log(f"[{idx + 1}/{len(sources)}] {key:<16} {status:<24} 累计 {count}")
        results.append({**src, "count": count, "status": status})

        if idx < len(sources) - 1 and not banned:
            time.sleep(args.delay)

    # 生成每个源单独的输出
    generated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    for res in results:
        items = stored.get(res["key"], {}).get("items", [])[:FEED_ITEMS]
        self_url = f"{SITE_BASE}/feeds/{res['key']}.xml"
        (FEED_DIR / f"{res['key']}.xml").write_text(
            build_rss(res, items, self_url), encoding="utf-8"
        )

    # 合并流
    combo: dict[str, dict] = {}
    for res in results:
        for it in stored.get(res["key"], {}).get("items", []):
            combo.setdefault(it["guid"], it)
    combo_items = sorted(combo.values(), key=lambda x: x.get("ts", 0), reverse=True)[:COMBO_ITEMS]
    combined_source = {"key": "all", "name": "全部板块合并流", "type": "topten"}
    (DOCS / "all.xml").write_text(
        build_rss(combined_source, combo_items, f"{SITE_BASE}/all.xml"), encoding="utf-8"
    )

    (DOCS / "index.html").write_text(render_index(results, generated), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    (DOCS / "status.json").write_text(
        json.dumps(
            {
                "generated": generated,
                "banned": banned,
                "sources": [
                    {"key": r["key"], "name": r["name"], "count": r["count"], "status": r["status"]}
                    for r in results
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    store["last_run"] = generated
    STORE_FILE.write_text(
        json.dumps(store, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )

    log(f"完成：{len(results)} 个源，合并流 {len(combo_items)} 条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
