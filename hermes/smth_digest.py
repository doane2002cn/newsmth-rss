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
  python3 smth_digest.py --dry-run  不发送，只把卡片写到 /tmp 并打印摘要(自检用)
"""
import json
import html
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

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
CARD_FILE = "/tmp/smth_card.json"
CHAT = "oc_b8559c55f250b54d396a8d8105bb1355"
CST = timezone(timedelta(hours=8))

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


def parse_feed(raw):
    """返回 (items, last_build)。item: title/link/guid/board/pub/desc/ts"""
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
        body = re.sub(r"(?i)<br\s*/?>", "\n", it.findtext("description") or "")
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
            "board": m.group(1) if m else "",
            "pub": pub, "ts": dt.timestamp() if dt else 0,
            "desc": body[:300],
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


# ── 卡片 ────────────────────────────────────────────────────────────────────
def render_card(sec, reads, summary, last_build, ai_broken, pool, delayed, sections):
    by_n = {i + 1: it for i, it in enumerate(pool)}
    stamp = datetime.now(CST).strftime("%Y-%m-%d")
    md = []

    if ai_broken:
        md += ["⚠️ **AI 分析暂不可用，以下为原始摘要**", ""]

    # 今日必读：条目间留空行，避免摘要和下一条标题糊在一起
    md += ["**📌 今日必读**", ""]
    for i, r in enumerate(reads, 1):
        it = by_n.get(int(r["n"]))
        if not it:
            continue
        md.append(f"{i}. [{strip_board_prefix(it['title'])}]({it['link']})")
        why = (r.get("why") or "").strip()
        if why:
            md.append(f"{SUB}推荐理由：{clip(why, 46)}")
        md.append("")
    if not reads:
        md += ["（今日无）", ""]

    # 今日十大：标注来源板块 + 正文摘要
    top = sec["topten"]
    md.append(f"**🔥 今日十大**（{len(top)} 条）")
    for i, it in enumerate(top, 1):
        md.append(f"{i}. [{strip_board_prefix(it['title'])}]({it['link']})")
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
            md.append(f"{i}. [{strip_board_prefix(it['title'])}]({it['link']})")
            line = sub_line(it, sections, with_board=(key == "sensibility"))
            if line:
                md.append(line)
        if not items:
            md.append("（今日无）")
        md.append("")

    pics = sec["pictures"][:MAX_PICTURES]
    md.append(f"**🖼 精彩贴图**（{len(pics)} 张）")
    for i, it in enumerate(pics, 1):
        md.append(f"{i}. [{strip_board_prefix(it['title'])}]({it['link']})")
        # 贴图重在图片本身；desc 里是图片地址，不能当摘要，只在有中文板块名时标来源
        if sections.get(it["board"], ("", ""))[1]:
            md.append(SUB + board_label(sections, it["board"]))
    if not pics:
        md.append("（今日无）")
    md.append("")

    if summary:
        md += ["**🧭 今日总结**", summary, ""]

    ts = last_build.strftime("%Y-%m-%d %H:%M") if last_build else "未知"
    md.append(f"**🕐 数据更新时间**：{ts}（北京时间）")
    if delayed:
        md.append("⚠️ 数据可能延迟：上游 RSS 尚未重建到当前小时")

    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text",
                             "content": f"水木社区每日推荐｜{stamp}"},
                   "template": "turquoise"},
        "elements": [{"tag": "markdown", "content": "\n".join(md)}],
    }


def send_card(card):
    with open(CARD_FILE, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False)
    r = subprocess.run(["python3", CARD_API, "--card-file", CARD_FILE, "--chat", CHAT],
                       capture_output=True, text=True, timeout=90)
    ok = r.returncode == 0 and "OK message_id" in (r.stdout or "")
    log = f"[info] 卡片已送达 {r.stdout.strip()}" if ok else \
          f"[warn] 卡片直发失败 rc={r.returncode} {r.stdout.strip()} {r.stderr.strip()}"
    print(log, file=sys.stderr)
    return ok


def main():
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
        "newexpress": dedupe(new_items),
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

    card = render_card(sec, reads, summary, last_build, ai_broken, pool,
                       delayed, sections)
    if dry:
        print("─" * 28 + " CARD PREVIEW (dry-run) " + "─" * 28)
        print(card["elements"][0]["content"])
        print("─" * 70)
        print(f"[dry-run] 未发送；卡片 JSON 见 {CARD_FILE}")
        return 0
    if not send_card(card):
        print("⚠️ 水木社区每日推荐：卡片发送失败（详见日志）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
