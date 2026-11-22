#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
火柴人 · AI 新闻桌宠
- 你不用电脑的时候，它去各个新闻源抓 AI 新闻
- 用 DeepSeek 挑出值得看的、去重、写成中文摘要
- 你回来时，在它旁边弹出气泡卡片，点一下就打开原文
"""

import base64
import calendar
import hashlib
import hmac
import json
import math
import os
import random
import re
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from PyQt6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QGuiApplication, QPainter, QPainterPath, QPen, QPolygonF
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMenu, QMessageBox, QPushButton, QScrollArea, QTextBrowser, QVBoxLayout, QWidget,
)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("AI_NEWS_PET_DATA") or (Path.home() / ".ai_news_pet"))   # 云端推送时会换个位置
DATA_DIR.mkdir(parents=True, exist_ok=True)
SEEN_FILE = DATA_DIR / "seen.json"
KEY_FILE = DATA_DIR / "api_key.txt"
HISTORY_FILE = DATA_DIR / "history.json"
LOG_FILE = DATA_DIR / "log.txt"
FEISHU_FILE = DATA_DIR / "feishu.json"
USAGE_FILE = DATA_DIR / "usage.json"
FEISHU_COUNT_FILE = DATA_DIR / "feishu_count.json"
EXPLAIN_DIR = APP_DIR / "深入浅出"              # 讲解文章存在项目文件夹里，方便找
EXPLAIN_INDEX = DATA_DIR / "explain_index.json"  # 记录 新闻链接 → 文件名
OLD_EXPLAIN_DIR = DATA_DIR / "explain"           # 旧版位置（会自动搬走）

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


# ------------------------------------------------------------------ 小工具
def log(msg):
    line = "[{}] {}".format(datetime.now().strftime("%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_config():
    cfg = load_json(APP_DIR / "config.json", None)
    if cfg is None:
        raise SystemExit("找不到 config.json，或者它的格式写错了（注意逗号和引号）。")
    return cfg


def get_api_key():
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return env
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_api_key(key):
    KEY_FILE.write_text(key.strip(), encoding="utf-8")
    try:
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass


def user_idle_seconds():
    """Mac 上：距离你上次动鼠标/键盘过了多少秒。"""
    if sys.platform != "darwin":
        return 0.0
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True,
                             text=True, timeout=3).stdout
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
        return int(m.group(1)) / 1e9 if m else 0.0
    except Exception:
        return 0.0


def today_str():
    return datetime.now().strftime("%Y-%m-%d")


def is_today_ts(ts):
    """发布时间（UTC 时间戳）换算成本地日期，是不是今天。"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d") == today_str()


def news_date(n):
    """新闻记录是哪天的。早期记录没有 date 字段，就从 time（如 "11-21 01:21"）推算。"""
    if n.get("date"):
        return n["date"]
    t = (n.get("time") or "")[:5]
    if re.match(r"\d\d-\d\d$", t):
        return "{}-{}".format(datetime.now().year, t)
    return ""


def only_today(items):
    """只留今天的新闻记录。"""
    return [n for n in items if news_date(n) == today_str()]


def day_str(days_ago):
    from datetime import timedelta
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def only_recent(items, days=3):
    """只留最近几天（含今天，默认 3 天）的新闻记录。"""
    keep = {day_str(i) for i in range(days)}
    return [n for n in items if news_date(n) in keep]


def on_day(items, days_ago):
    d = day_str(days_ago)
    return [n for n in items if news_date(n) == d]


def clean_text(s, limit=400):
    s = BeautifulSoup(s or "", "html.parser").get_text(" ", strip=True)
    s = re.sub(r"\s+", " ", s)
    return s[:limit]


# ------------------------------------------------------------------ 抓新闻
def fetch_rss(src):
    r = requests.get(src["url"], headers=UA, timeout=20)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    items = []
    for e in feed.entries[:30]:
        link = e.get("link") or ""
        if not link:
            continue
        ts = None
        for k in ("published_parsed", "updated_parsed"):
            if e.get(k):
                ts = calendar.timegm(e[k])
                break
        items.append({
            "id": link,
            "link": link,
            "title": clean_text(e.get("title"), 200),
            "snippet": clean_text(e.get("summary") or e.get("description"), 400),
            "source": src["name"],
            "ts": ts,
        })
    return items


def fetch_page(src):
    """给没有 RSS 的网站用：读新闻列表页，找出文章链接。"""
    r = requests.get(src["url"], headers=UA, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    pat = re.compile(src["link_pattern"])
    seen, items = set(), []
    for a in soup.find_all("a", href=True):
        link = urljoin(src["url"], a["href"]).split("#")[0].split("?")[0]
        if not pat.search(link) or link in seen:
            continue
        text = clean_text(a.get_text(" ", strip=True), 200)
        if len(text) < 6:
            continue
        seen.add(link)
        items.append({"id": link, "link": link, "title": text, "snippet": "",
                      "source": src["name"], "ts": None})
        if len(items) >= 20:
            break
    return items


SOURCE_STATS = {}   # 每个源上次抓取的结果：url → {"n": 条数, "ok": 成功?, "t": 时间}


def fetch_one(src):
    try:
        items = fetch_page(src) if src.get("type") == "page" else fetch_rss(src)
        log("{}：拿到 {} 条".format(src["name"], len(items)))
        SOURCE_STATS[src["url"]] = {"n": len(items), "ok": True, "t": time.time()}
        return items
    except Exception as ex:
        log("{}：抓取失败（{}）".format(src["name"], ex))
        SOURCE_STATS[src["url"]] = {"n": 0, "ok": False, "t": time.time(), "err": str(ex)[:120]}
        return []


def fetch_all(sources):
    sources = [s for s in list(sources) if s.get("enabled", True)]
    out = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for items in pool.map(fetch_one, sources):
            out.extend(items)
    return out


# ------------------------------------------------------------------ 管理信息源
def _feed_entries(content):
    try:
        f = feedparser.parse(content)
    except Exception:
        return None
    if f.entries and any(e.get("link") for e in f.entries):
        return f
    return None


def _short_name(s, url):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    # 去掉常见的尾巴，如「 - Blog」「| 官网」
    s = re.split(r"\s+[|\-–—·]\s+", s)[0].strip() or s
    if not s:
        from urllib.parse import urlparse
        s = urlparse(url).netloc.replace("www.", "")
    return s[:20]


def _guess_link_pattern(page_url, soup):
    """没有 RSS 的网站：在列表页里找「像文章」的链接，归纳出一条链接规律。"""
    from urllib.parse import urlparse
    base = urlparse(page_url)
    host = base.netloc
    groups = {}
    for a in soup.find_all("a", href=True):
        link = urljoin(page_url, a["href"]).split("#")[0].split("?")[0]
        u = urlparse(link)
        if u.netloc != host:
            continue
        path = u.path.rstrip("/")
        segs = [s for s in path.split("/") if s]
        if len(segs) < 2:
            continue
        text = clean_text(a.get_text(" ", strip=True), 200)
        if len(text) < 8:
            continue
        parent = "/" + "/".join(segs[:-1])
        g = groups.setdefault(parent, {"links": set(), "chars": 0})
        if link not in g["links"]:
            g["links"].add(link)
            g["chars"] += len(text)
    best, score = None, 0
    want = base.path.rstrip("/")
    for parent, g in groups.items():
        n = len(g["links"])
        if n < 3:
            continue
        s = n * (g["chars"] / n)            # 链接多、标题长的更像文章列表
        if want and parent.startswith(want):
            s *= 1.5                        # 和你给的网址同一栏目，加分
        if s > score:
            best, score = parent, s
    if not best:
        return None
    return re.escape(host + best) + r"/[^/]+/?$"


def detect_source(url):
    """
    你给一个链接，自动判断怎么抓：
    1) 本身就是 RSS → 直接用
    2) 网页里写了 RSS 地址 → 用那个
    3) 试几个常见的 RSS 地址（/feed、/rss.xml …）
    4) 都没有 → 当普通网页读，自动归纳文章链接规律
    成功返回 (源设置, 试抓到的条目)，失败抛出带中文原因的异常。
    """
    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url
    try:
        r = requests.get(url, headers=UA, timeout=20)
    except Exception as ex:
        raise RuntimeError("打不开这个链接（{}）".format(ex.__class__.__name__))
    if r.status_code >= 400:
        raise RuntimeError("网站返回错误 {}，可能需要登录或拒绝了访问".format(r.status_code))
    url = r.url  # 跟随跳转后的真实地址

    def as_rss(feed_url, feed):
        src = {"name": _short_name(feed.feed.get("title"), feed_url), "type": "rss", "url": feed_url}
        return src, fetch_rss(src)

    # 1) 本身就是 RSS
    f = _feed_entries(r.content)
    if f:
        return as_rss(url, f)

    soup = BeautifulSoup(r.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""

    # 2) 网页里声明的 RSS
    cands = []
    for ln in soup.find_all("link", href=True):
        t = (ln.get("type") or "").lower()
        if "rss" in t or "atom" in t:
            cands.append(urljoin(url, ln["href"]))
    # 3) 常见地址
    root = re.match(r"https?://[^/]+", url).group(0)
    here = url.rstrip("/")
    for p in ("/feed", "/rss", "/rss.xml", "/feed.xml", "/atom.xml", "/index.xml"):
        cands += [here + p, root + p]
    tried = set()
    for c in cands:
        if c in tried:
            continue
        tried.add(c)
        try:
            rr = requests.get(c, headers=UA, timeout=10)
            if rr.status_code < 400:
                f = _feed_entries(rr.content)
                if f:
                    return as_rss(c, f)
        except Exception:
            pass

    # 4) 当网页读
    pat = _guess_link_pattern(url, soup)
    if pat:
        src = {"name": _short_name(title, url), "type": "page", "url": url, "link_pattern": pat}
        items = fetch_page(src)
        if len(items) >= 3:
            return src, items
    raise RuntimeError("在这个页面里找不到文章列表。可能是需要登录，或者内容是网页加载后才显示的。"
                       "可以试试换成这个网站的「新闻 / 博客」列表页链接。")


# ------------------------------------------------------------------ AI 帮忙找信息源
FIND_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "在网上搜索。用来找这个作者/网站/公众号的官网、博客、RSS 地址。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词，简短具体"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "try_source",
        "description": "试抓一个链接（网站首页、文章列表页或 RSS 地址都行）。会自动找 RSS 或读网页，"
                       "返回能不能用、抓到几条、最新几篇标题。只有试抓成功的链接才能作为最终答案。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "要试的链接"}}, "required": ["url"]}}},
]

FIND_PROMPT = """你是火柴人的助手，帮用户给 AI 新闻桌宠添加一个「信息源」。
用户想加的是：{want}

已有的信息源（不要重复添加）：
{existing}

你的任务：找到一个能稳定抓到这个来源最新文章的链接，最好是 RSS。
做法：
1. 先用 web_search 弄清楚这是什么（官网？博客？微信公众号？个人作者？），找到它的官网 / 博客 / 文章列表页 / RSS。
2. 用 try_source 试抓候选链接。返回「可以用」并且标题确实是这个来源的文章，才算找到。
3. 小提示：
   - 微信公众号本身没有 RSS，要找第三方把公众号转成 RSS 的地址（比如 wechat2rss.bestblogs.dev 上的 feed、或别的公众号转 RSS 服务），可以搜「名字 RSS」「名字 wechat2rss」。
   - 很多作者在多个平台发文（个人博客、知乎专栏、Substack、Medium、少数派等），公众号找不到 RSS 时可以找这些平台。
   - 某个链接试不通就换下一个，别在一处反复试。
4. {limit_text}

最后**只输出一个 JSON**，不要别的文字：
- 找到了：{{"url": "试抓成功的那个链接", "name": "简短好认的中文名（如：数字生命卡兹克）", "note": "一句话说明这是什么、从哪找到的"}}
- 没找到：{{"url": "", "note": "一两句话说明原因，以及用户可以怎么办（比如自己去哪找个链接贴进来）"}}
"""


def find_source_agent(cfg, want, key, progress=None):
    """用户只说了名字、没给链接：让 AI 自己上网找能抓的地址。返回 (源设置, 条目, 说明)。"""
    from openai import OpenAI
    # 找源不受「每日预算」限制（花费照常记账），但最多 8 步，防止它一直搜下去。
    say = progress or (lambda t: None)
    max_calls = int(cfg.get("find_source_max_calls", 8))
    max_tokens = int(cfg.get("find_source_max_tokens", 0)) or 10 ** 12
    limit_text = "最多一共调用 {} 次工具。找到能用的就停，确实找不到也要说明原因。".format(max_calls)
    existing = "\n".join("- {}：{}".format(s["name"], s["url"]) for s in cfg.get("sources", [])) or "（无）"
    messages = [{"role": "user", "content": FIND_PROMPT.format(want=want, existing=existing, limit_text=limit_text)}]
    client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=120)
    tried = {}                     # url → (源设置, 条目)，只放试抓成功的
    calls, tokens, cost, resp = 0, 0, 0.0, None
    for _round in range(max_calls + 2):
        allow = calls < max_calls and tokens < max_tokens
        say("想一想…" if allow else "整理结果…")
        resp = client.chat.completions.create(model=cfg["model"], messages=messages, tools=FIND_TOOLS,
                                              tool_choice="auto" if allow else "none")
        u = getattr(resp, "usage", None)
        try:
            cost += record_usage(cfg, u)
        except Exception as ex:
            log("记录用量失败：{}".format(ex))
        tokens += (getattr(u, "total_tokens", 0) or 0) if u else 0
        msg = resp.choices[0].message
        tcs = getattr(msg, "tool_calls", None) or []
        if not tcs:
            break
        am = {"role": "assistant", "content": msg.content or "",
              "tool_calls": [{"id": t.id, "type": "function",
                              "function": {"name": t.function.name, "arguments": t.function.arguments}}
                             for t in tcs]}
        rc = getattr(msg, "reasoning_content", None)
        if rc:
            am["reasoning_content"] = rc
        messages.append(am)
        for t in tcs:
            try:
                args = json.loads(t.function.arguments or "{}")
            except Exception:
                args = {}
            if calls >= max_calls or tokens >= max_tokens:
                result = "已经达到工具调用上限，请直接输出最终 JSON。"
            else:
                calls += 1
                if t.function.name == "web_search":
                    q = args.get("query", "")
                    say("第 {}/{} 步 搜索：{}".format(calls, max_calls, q[:24]))
                    log("找信息源 第 {} 步：搜索 {}".format(calls, q))
                    try:
                        res = web_search(q, n=6)
                        result = "\n".join("{}. {}\n   {}\n   {}".format(i + 1, r["title"], r["url"], r["snippet"][:160])
                                           for i, r in enumerate(res)) or "没有搜到结果。"
                    except Exception as ex:
                        result = "搜索出错：{}".format(ex)
                elif t.function.name == "try_source":
                    url = args.get("url", "")
                    say("第 {}/{} 步 试抓：{}".format(calls, max_calls, url[:30]))
                    log("找信息源 第 {} 步：试抓 {}".format(calls, url))
                    try:
                        src, items = detect_source(url)
                        tried[url] = (src, items)
                        result = "可以用：{}（{}），地址 {}，抓到 {} 条。最新标题：\n{}".format(
                            src["name"], "RSS" if src["type"] == "rss" else "网页", src["url"], len(items),
                            "\n".join("- " + x["title"][:60] for x in items[:5]))
                    except Exception as ex:
                        result = "不能用：{}".format(ex)
                else:
                    result = "没有这个工具。"
            messages.append({"role": "tool", "tool_call_id": t.id, "content": result})
    log("找信息源「{}」：用了 {} 次工具，约 {} tokens，¥{:.4f}".format(want, calls, tokens, cost))

    text = ((resp.choices[0].message.content if resp else "") or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except Exception:
        data = {}
    url = (data.get("url") or "").strip()
    note = (data.get("note") or "").strip()
    if not url:
        raise RuntimeError(note or "AI 没找到能用的地址。可以自己找一个链接贴进来。")
    if url in tried:
        src, items = tried[url]
    else:
        # AI 给了个没试过的链接：自己再验一遍，不能只信它
        src, items = detect_source(url)
    if data.get("name"):
        src["name"] = str(data["name"]).strip()[:20]
    return src, items, note


def save_sources(sources):
    """只改 config.json 里的 sources，其余设置保持原样；每个源占一行，方便手改。"""
    path = APP_DIR / "config.json"
    cfg = load_json(path, None)
    if cfg is None:
        raise RuntimeError("config.json 读不出来，没敢改")
    cfg["sources"] = sources
    text = json.dumps(cfg, ensure_ascii=False, indent=2)
    # 短列表（如 "quiet_hours": [0, 8]）收成一行
    text = re.sub(r"\[\n\s*([^\[\]{}]*?)\n\s*\]",
                  lambda m: "[" + re.sub(r",\n\s*", ", ", m.group(1)) + "]", text)
    lines = ",\n".join("    " + json.dumps(s, ensure_ascii=False) for s in sources)
    block = '"sources": [\n' + lines + "\n  ]"
    text = re.sub(r'"sources": \[.*?\n  \]', lambda _: block, text, flags=re.S) if sources else text
    json.loads(text)  # 再检查一遍格式
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def mark_items_seen(items):
    """新加的源：把它现在列表里的旧文章记为看过，免得一加上就刷出一堆旧闻。今天发布的留着。"""
    old = [x["id"] for x in items if not (x.get("ts") and is_today_ts(x["ts"]))]
    if not old:
        return
    seen = load_json(SEEN_FILE, None)
    if seen is None:
        return  # 还没抓过第一次，第一次运行本来就只看今天的
    seen.extend(old)
    save_json(SEEN_FILE, seen[-5000:])


# ------------------------------------------------------------------ Token 用量统计
# deepseek-flash 价格（元 / 百万 tokens，闲时），高峰时段 ×2。可在 config.json 里用 "price" 覆盖
DEFAULT_PRICE = {"hit": 0.02, "miss": 1.0, "out": 4.0, "peak_multiplier": 2.0}


def is_peak_beijing():
    """DeepSeek 高峰时段：北京时间工作日 9-12 点、14-18 点。"""
    from datetime import timedelta, timezone
    bj = datetime.now(timezone(timedelta(hours=8)))
    return bj.weekday() < 5 and (9 <= bj.hour < 12 or 14 <= bj.hour < 18)


def record_usage(cfg, usage):
    """记一次 DeepSeek 调用的 token 和费用，返回这次的费用（元）。"""
    if usage is None:
        return 0.0
    extra = getattr(usage, "model_extra", None) or {}
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    out = getattr(usage, "completion_tokens", 0) or 0
    hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if hit is None:
        hit = extra.get("prompt_cache_hit_tokens", 0) or 0
    miss = getattr(usage, "prompt_cache_miss_tokens", None)
    if miss is None:
        miss = extra.get("prompt_cache_miss_tokens", prompt - hit)
    price = dict(DEFAULT_PRICE)
    price.update(cfg.get("price") or {})
    mult = price["peak_multiplier"] if is_peak_beijing() else 1.0
    cost = (hit * price["hit"] + miss * price["miss"] + out * price["out"]) / 1e6 * mult

    data = load_json(USAGE_FILE, {})
    d = data.setdefault(today_str(), {"calls": 0, "in": 0, "hit": 0, "out": 0, "cost": 0.0})
    d["calls"] += 1
    d["in"] += prompt
    d["hit"] += hit
    d["out"] += out
    d["cost"] = round(d["cost"] + cost, 6)
    save_json(USAGE_FILE, data)
    log("DeepSeek 用量：输入 {}（缓存命中 {}），输出 {}，约 ¥{:.4f}{}".format(
        prompt, hit, out, cost, "（高峰价）" if mult > 1 else ""))
    return cost


def usage_summary():
    data = load_json(USAGE_FILE, {})
    today, month = today_str(), today_str()[:7]

    def agg(keys):
        t = {"calls": 0, "in": 0, "out": 0, "cost": 0.0}
        for k in keys:
            for f in t:
                t[f] += data[k].get(f, 0)
        return t

    return (agg([k for k in data if k == today]),
            agg([k for k in data if k.startswith(month)]),
            agg(list(data)))


def fetch_balance(key, base_url):
    """查询 DeepSeek 账户余额，失败返回 None。"""
    try:
        r = requests.get(base_url.rstrip("/") + "/user/balance",
                         headers={"Authorization": "Bearer " + key}, timeout=10)
        infos = r.json().get("balance_infos") or []
        if infos:
            b = infos[0]
            return "{} {}".format("¥" if b.get("currency") == "CNY" else b.get("currency", ""),
                                  b.get("total_balance"))
    except Exception as ex:
        log("查询余额失败：{}".format(ex))
    return None


# ------------------------------------------------------------------ 偏好学习（带保险）
PREFS_FILE = DATA_DIR / "prefs.json"
TOPICS = ["大模型发布", "研究论文", "AI产品与应用", "开源模型与工具", "公司与行业动态",
          "芯片与算力", "机器人与具身智能", "AI安全与伦理", "政策与监管", "编程与开发者工具", "其他"]
# 每种行为算多少分：打开原文 +1，深入浅出 +2，推到飞书 +2，不感兴趣 -3
ACTION_WEIGHT = {"open": 1.0, "explain": 2.0, "feishu": 2.0, "dislike": -3.0}


def load_prefs():
    d = load_json(PREFS_FILE, {})
    d.setdefault("events", [])
    d.setdefault("paused", False)
    return d


def record_pref(cfg, item, action):
    """记一次你对某条新闻的行为（只记主题，不记内容）。"""
    if not cfg.get("learning", True):
        return
    prefs = load_prefs()
    if prefs.get("paused") or not item.get("topic"):
        return
    prefs["events"].append({"t": time.time(), "topic": item["topic"], "action": action})
    prefs["events"] = prefs["events"][-500:]
    save_json(PREFS_FILE, prefs)


def topic_scores(cfg):
    """按时间衰减算每个主题的得分：越久以前的行为影响越小（默认 14 天减半）。"""
    half = float(cfg.get("pref_half_life_days", 14)) * 86400
    now = time.time()
    scores = {}
    for ev in load_prefs()["events"]:
        w = ACTION_WEIGHT.get(ev.get("action"), 0) * 0.5 ** ((now - ev.get("t", now)) / half)
        scores[ev["topic"]] = scores.get(ev["topic"], 0) + w
    return scores


def preference_text(cfg):
    """给 DeepSeek 看的偏好描述；行为太少时返回空（不学习）。"""
    if not cfg.get("learning", True) or load_prefs().get("paused"):
        return "", []
    sc = topic_scores(cfg)
    if sum(abs(v) for v in sc.values()) < 4:      # 数据太少，先不影响推荐
        return "", []
    liked = [t for t, v in sorted(sc.items(), key=lambda x: -x[1]) if v >= 1][:4]
    disliked = [t for t, v in sorted(sc.items(), key=lambda x: x[1]) if v <= -1][:3]
    rare = [t for t in TOPICS if t != "其他" and sc.get(t, 0) < 0.5]
    parts = []
    if liked:
        parts.append("用户最近常看：" + "、".join(liked))
    if disliked:
        parts.append("用户不太感兴趣：" + "、".join(disliked))
    return "；".join(parts), rare


# ------------------------------------------------------------------ AI 总结
PROMPT = """你是一个 AI 新闻编辑。下面是刚抓到的新闻条目（编号、来源、标题、简介）。

用户关心的内容：{interests}

{pref_block}
请你：
1. 只挑出真正值得看的 AI 相关新闻，最多 {max_n} 条，按重要程度排序。和 AI 无关的、营销软文、标题党都不要。
2. **重大新闻必须入选，不受用户偏好影响**（例如头部公司发布重要新模型、行业格局级的大事），并标 "major": true。
3. 其余名额参考用户偏好排序，但偏好只是参考，不能因此漏掉重要的事。
{explore_rule}
5. 同一件事被多个来源报道时只留一条，优先保留官方来源。
6. 每条写一个中文标题（不超过 30 字）、一句话中文摘要（不超过 70 字），从这个列表里选一个主题：{topics}
7. 每条写一句很短的推荐理由（不超过 20 字），比如"重大新闻""你常看的开源模型方向""圈外推荐：了解一下监管动向"。
8. 如果没有值得推的，就返回空列表。

只输出 JSON，格式如下：
{{"picks": [{{"i": 编号, "title": "中文标题", "summary": "一句话摘要", "topic": "主题", "major": false, "explore": false, "reason": "推荐理由"}}]}}

新闻条目：
{items}"""


def check_daily_budget(cfg):
    """每天的 DeepSeek 总花费上限（默认 1 元），超了就停到明天。"""
    cap = float(cfg.get("daily_budget_yuan", 1.0))
    if cap > 0:
        spent = usage_summary()[0]["cost"]
        if spent >= cap:
            raise RuntimeError("今天 DeepSeek 已花 ¥{:.2f}，达到每日上限 ¥{:.2f}，明天再继续".format(spent, cap))


def summarize(cfg, cands, key):
    from openai import OpenAI
    check_daily_budget(cfg)

    max_n = int(cfg.get("max_news_per_push", 3))
    lines = []
    for i, c in enumerate(cands):
        snippet = (" —— " + c["snippet"][:200]) if c["snippet"] else ""
        lines.append("[{}]（{}）{}{}".format(i, c["source"], c["title"], snippet))
    pref, rare = preference_text(cfg)
    slots = int(cfg.get("explore_slots", 1))
    if pref and slots > 0 and max_n >= 2:
        explore_rule = ("4. 防止信息茧房：至少留 {} 个名额给「圈外推荐」——选一条值得看、但属于用户平时很少看的主题"
                        "（优先从这些里选：{}）的新闻，标 \"explore\": true，理由以「圈外推荐：」开头。"
                        "如果没有合适的就不用硬凑。").format(slots, "、".join(rare) or "任意其他主题")
    else:
        explore_rule = "4. 尽量让入选的新闻覆盖不同主题，不要都是同一类。"
    pref_block = ("用户偏好（根据他最近的点击自动学到的，仅供参考）：" + pref + "\n") if pref else ""
    prompt = PROMPT.format(interests=cfg.get("interests", "AI 领域的重要新闻"), pref_block=pref_block,
                           explore_rule=explore_rule, topics="、".join(TOPICS),
                           max_n=max_n, items="\n".join(lines))

    client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=120)
    resp = client.chat.completions.create(
        model=cfg["model"],
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )
    try:
        record_usage(cfg, getattr(resp, "usage", None))
    except Exception as ex:
        log("记录用量失败：{}".format(ex))
    text = resp.choices[0].message.content or ""
    m = re.search(r"\{.*\}", text, re.S)
    data = json.loads(m.group(0)) if m else {"picks": []}

    news = []
    for p in data.get("picks", [])[:max_n]:
        try:
            c = cands[int(p["i"])]
        except Exception:
            continue
        n = make_news(c, p.get("title") or c["title"], p.get("summary", ""))
        topic = p.get("topic") if p.get("topic") in TOPICS else "其他"
        n.update({"topic": topic, "major": bool(p.get("major")), "explore": bool(p.get("explore")),
                  "reason": (p.get("reason") or "")[:30]})
        news.append(n)
    return news


def make_news(c, title, summary):
    return {"title": title, "summary": summary, "link": c["link"],
            "source": c["source"], "time": datetime.now().strftime("%m-%d %H:%M"),
            "date": today_str()}


# ------------------------------------------------------------------ 飞书推送
def feishu_conf():
    c = load_json(FEISHU_FILE, {})
    return c if c.get("webhook") else None


def feishu_sent_today():
    return load_json(FEISHU_COUNT_FILE, {}).get(today_str(), 0)


def add_feishu_sent(n):
    data = {today_str(): feishu_sent_today() + n}   # 只留今天的计数
    save_json(FEISHU_COUNT_FILE, data)


# 飞书里打不开的链接：这些网站在飞书内置浏览器里会被拦（比如公众号文章要求「在微信客户端打开」）
DEFAULT_FEISHU_SKIP = ["mp.weixin.qq.com", "weixin.qq.com"]
BLOCK_MARKERS = ["请在微信客户端打开", "环境异常", "访问过于频繁", "Just a moment...", "Access denied",
                 "Attention Required", "verify you are human"]


SHORT_FILE = DATA_DIR / "explain_short.json"   # 精简版缓存：{新闻链接: 文字}

SHORT_PROMPT = """你是一个擅长「深入浅出」的讲解者。请把下面这条 AI 新闻浓缩成一段 {n} 字左右的中文讲解，让人读完就懂：
- 用一个生活化的场景或比喻开头，然后一句话点明这件事的本质，最后说为什么值得关注。
- 用"你"称呼读者，口语化，不说教。
- 只写一到两段纯文字：不要标题、不要列表、不要 Markdown 符号。
- 诚实：具体数字只能来自下面的材料，材料里没有的不要编。

新闻标题：{title}
来源：{source}
摘要：{summary}

材料（可能是原文节选，也可能是已经写好的长篇讲解）：
{material}"""


def short_explain(cfg, item, key, progress=None):
    """深入浅出精简版（默认约 250 字）。有全文讲解就从全文压缩，没有就直接看原文写；写过的存起来不重复花钱。"""
    cache = load_json(SHORT_FILE, {})
    if item["link"] in cache:
        return cache[item["link"]], 0.0
    from openai import OpenAI
    check_daily_budget(cfg)
    say = progress or (lambda t: None)
    full = explain_cache_path(item)
    if full.exists():
        material = full.read_text(encoding="utf-8")[:8000]
    else:
        say("读原文")
        material = fetch_article_text(item["link"], limit=6000) or "（原文没抓到，只能根据标题和摘要讲你能确定的部分）"
    say("写精简版")
    n = int(cfg.get("feishu_explain_chars", 250))
    prompt = SHORT_PROMPT.format(n=n, title=item["title"], source=item.get("source", ""),
                                 summary=item.get("summary", ""), material=material)
    client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=120)
    resp = client.chat.completions.create(model=cfg["model"], messages=[{"role": "user", "content": prompt}])
    cost = 0.0
    try:
        cost = record_usage(cfg, getattr(resp, "usage", None))
    except Exception as ex:
        log("记录用量失败：{}".format(ex))
    text = (resp.choices[0].message.content or "").strip()
    text = re.sub(r"[#*`>]+", "", text).strip()
    if text:
        cache = load_json(SHORT_FILE, {})
        cache[item["link"]] = text
        if len(cache) > 300:                       # 只留最近 300 条
            cache = dict(list(cache.items())[-300:])
        save_json(SHORT_FILE, cache)
    return text, cost


def explains_for_feishu(cfg, items, progress=None, full=False):
    """推飞书前准备深入浅出：默认是精简版；full=True 时是完整讲解。写不出来就不附，新闻照推。"""
    if not cfg.get("feishu_with_explain", True) and not full:
        return {}
    key = get_api_key()
    if not key:
        return {}
    out = {}
    for n in items:
        try:
            if full:
                text, cost = explain_news(cfg, n, key, progress=progress)
            else:
                text, cost = short_explain(cfg, n, key, progress=progress)
            if text:
                out[n["link"]] = text
                log("飞书附带深入浅出{}：{}（约 ¥{:.4f}）".format("全文" if full else "精简版", n.get("title", ""), cost))
        except Exception as ex:
            log("飞书附带深入浅出失败（新闻照推）：{}".format(ex))
    return out


def link_ok_for_feishu(cfg, link):
    """推到飞书前先试着打开一次链接：打不开、要验证、或是飞书里打不开的网站，都不推。"""
    from urllib.parse import urlparse
    skip = cfg.get("feishu_skip_domains", DEFAULT_FEISHU_SKIP)

    def blocked(url):
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in skip)

    if not link or blocked(link):
        return False, "飞书里打不开这个网站"
    try:
        r = requests.get(link, headers=UA, timeout=12, allow_redirects=True)
        if r.status_code >= 400:
            return False, "链接打不开（{}）".format(r.status_code)
        if blocked(r.url):
            return False, "跳转到了飞书里打不开的网站"
        head = r.text[:20000]
        for mk in BLOCK_MARKERS:
            if mk in head:
                return False, "网页需要验证或要求在别的 App 打开"
        return True, ""
    except requests.exceptions.Timeout:
        return True, ""          # 只是网站慢，飞书里一般能打开，照常推
    except Exception as ex:
        return False, "链接打不开（{}）".format(type(ex).__name__)


def md_to_post_lines(md, max_chars=6000):
    """把深入浅出的 Markdown 转成飞书富文本的行：标题加粗、链接可点、去掉 Markdown 符号。"""
    # 只去掉最末尾那段「来源 · 原文链接 · 生成时间」小字；文章中间的分隔线要保留
    head, sep, tail = md.rpartition("\n---\n")
    if sep and tail.strip().startswith("*来源"):
        md = head
    md = md.strip()
    if len(md) > max_chars:
        md = md[:max_chars].rsplit("\n", 1)[0] + "\n……（篇幅太长，后面省略了）"
    out, blank = [], False
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            if not blank and out:
                out.append([{"tag": "text", "text": " "}])
            blank = True
            continue
        blank = False
        if re.fullmatch(r"[-*_]{3,}", line):       # 文章里的分隔线
            out.append([{"tag": "text", "text": "────────"}])
            continue
        bold = line.startswith("#")
        line = re.sub(r"^#+\s*", "", line)
        line = re.sub(r"^[-*]\s+", "• ", line)
        line = line.replace("**", "").replace("__", "")
        segs, pos = [], 0
        for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", line):
            if m.start() > pos:
                segs.append({"tag": "text", "text": line[pos:m.start()]})
            segs.append({"tag": "a", "text": m.group(1), "href": m.group(2)})
            pos = m.end()
        if pos < len(line):
            segs.append({"tag": "text", "text": line[pos:]})
        if bold:                     # 飞书群机器人不支持加粗参数，标题用【】标出来
            segs = [{"tag": "text", "text": "【"}] + segs + [{"tag": "text", "text": "】"}]
        segs = [sg for sg in segs if sg.get("text")]
        out.append(segs or [{"tag": "text", "text": " "}])
    return out


def _feishu_post(c, title, lines):
    """发一条飞书富文本消息，成功返回 True。"""
    body = {"msg_type": "post", "content": {"post": {"zh_cn": {"title": title, "content": lines}}}}
    secret = (c.get("secret") or "").strip()
    if secret:
        ts = str(int(time.time()))
        sign = base64.b64encode(hmac.new("{}\n{}".format(ts, secret).encode("utf-8"),
                                        digestmod=hashlib.sha256).digest()).decode()
        body["timestamp"], body["sign"] = ts, sign
    try:
        r = requests.post(c["webhook"].strip(), json=body, timeout=15)
        data = r.json()
        ok = data.get("code", data.get("StatusCode", -1)) == 0
        log("飞书推送{}：{}".format("成功" if ok else "失败", title if ok else data))
        return ok
    except Exception as ex:
        log("飞书推送失败：{}".format(ex))
        return False


def split_lines(lines, max_bytes=9000):
    """把很多行按大小切成几段，尽量在空行（段落之间）断开。"""
    chunks, cur, size = [], [], 0
    for ln in lines:
        b = len(json.dumps(ln, ensure_ascii=False).encode("utf-8"))
        is_gap = all(sg.get("text", "").strip() == "" for sg in ln)
        if cur and size + b > max_bytes:
            chunks.append(cur)
            cur, size = [], 0
            if is_gap:
                continue
        cur.append(ln)
        size += b
    if cur:
        chunks.append(cur)
    # 去掉每段开头结尾的空行
    clean = []
    for ch in chunks:
        while ch and all(sg.get("text", "").strip() == "" for sg in ch[0]):
            ch = ch[1:]
        while ch and all(sg.get("text", "").strip() == "" for sg in ch[-1]):
            ch = ch[:-1]
        if ch:
            clean.append(ch)
    return clean


def send_feishu(news, title=None, explains=None):
    """推到飞书（默认）：一条消息里放新闻标题、摘要，以及深入浅出精简版。成功返回 True。"""
    c = feishu_conf()
    if not c:
        return False
    explains = explains or {}
    lines = []
    for i, n in enumerate(news, 1):
        label = n["title"] if len(news) == 1 else "{}. {}".format(i, n["title"])
        lines.append([{"tag": "a", "text": label, "href": n["link"]}])
        desc = n.get("summary") or ""
        lines.append([{"tag": "text", "text": "{}（{}）".format(desc, n["source"]) if desc else "（{}）".format(n["source"])}])
        ex = explains.get(n["link"])
        if ex:
            lines.append([{"tag": "text", "text": " "}])
            lines.append([{"tag": "text", "text": "📖 深入浅出 · 精简版"}])
            for para in [p.strip() for p in ex.split("\n") if p.strip()]:
                lines.append([{"tag": "text", "text": para}])
            lines.append([{"tag": "text", "text": "👉 想看完整讲解：在火柴人的新闻卡片上右键 →「推送全文到飞书」"}])
        lines.append([{"tag": "text", "text": " "}])
    return _feishu_post(c, title or "🤖 AI 新闻速递 · {} 条".format(len(news)), lines)


def send_feishu_full(item, explain_md):
    """把一条新闻的深入浅出全文推到飞书，按段落分成几条发。全部发出去返回 True。"""
    c = feishu_conf()
    if not c or not explain_md:
        return False
    head = [[{"tag": "text", "text": "原文："}, {"tag": "a", "text": item["title"], "href": item["link"]}],
            [{"tag": "text", "text": " "}]]
    chunks = split_lines(head + md_to_post_lines(explain_md, 20000))
    total = len(chunks)
    for k, ch in enumerate(chunks, 1):
        if k > 1:
            time.sleep(0.4)                       # 飞书群机器人有频率限制，慢一点发
        t = "📖 深入浅出全文：{}".format(item["title"][:24])
        if total > 1:
            t += "（{}/{}）".format(k, total)
        if not _feishu_post(c, t, ch):
            log("深入浅出全文第 {}/{} 段没发出去".format(k, total))
            return False
    return True


# ------------------------------------------------------------------ 深入浅出
def load_explain_method():
    """读取项目文件夹里的「深入浅出.md」（写作方法，可以自己改）。"""
    try:
        return (APP_DIR / "深入浅出.md").read_text(encoding="utf-8")
    except Exception:
        return "请用深入浅出的方式，写一篇 2000 字左右的中文文章，把这条新闻讲清楚。不要编造数据。"


def fetch_article_text(url, limit=8000):
    """抓原文正文（尽力而为：慢网站多等一会儿，失败重试一次；还不行就返回空）。"""
    try:
        r = None
        for attempt in range(2):
            try:
                r = requests.get(url, headers=UA, timeout=30)
                break
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == 1:
                    raise
                time.sleep(2)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
            tag.decompose()
        node = soup.find("article") or soup.find("main") or soup.body or soup
        text = re.sub(r"\n\s*\n+", "\n", node.get_text("\n", strip=True))
        return text[:limit]
    except Exception as ex:
        log("抓原文失败（{}）：{}".format(url, ex))
        return ""


def safe_filename(title, limit=40):
    t = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", title).strip()
    t = re.sub(r"\s+", " ", t)
    return (t[:limit].rstrip() or "未命名")


def explain_cache_path(item):
    """这条新闻的讲解文件：深入浅出/日期 标题.md（讲过的从索引里找）。"""
    index = load_json(EXPLAIN_INDEX, {})
    name = index.get(item["link"])
    if name and (EXPLAIN_DIR / name).exists():
        return EXPLAIN_DIR / name
    date = item.get("date") or today_str()
    return EXPLAIN_DIR / "{} {}.md".format(date, safe_filename(item["title"]))


def remember_explain(item, path):
    index = load_json(EXPLAIN_INDEX, {})
    index[item["link"]] = path.name
    save_json(EXPLAIN_INDEX, index)


# ------------------------------------------------------------------ 定期清理
CLEAN_MARK = DATA_DIR / "last_cleanup.txt"


def trim_log(path, max_bytes=1_000_000, keep_bytes=300_000):
    """日志超过约 1MB，就只保留最后约 300KB。"""
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return False
        with open(path, "rb") as f:
            f.seek(-keep_bytes, 2)
            tail = f.read()
        tail = tail[tail.find(b"\n") + 1:]          # 从完整的一行开始
        with open(path, "wb") as f:
            f.write("==== {} 日志太长，已自动清理旧内容 ====\n".format(
                datetime.now().strftime("%Y-%m-%d %H:%M")).encode("utf-8"))
            f.write(tail)
        return True
    except Exception as ex:
        print("清理日志失败：", ex)
        return False


def cleanup_due():
    """每周一清理一次。周一电脑没开的话，之后第一次运行时补上（距上次满 7 天）。"""
    try:
        last = CLEAN_MARK.read_text().strip() if CLEAN_MARK.exists() else ""
    except Exception:
        last = ""
    today = today_str()
    if last == today:
        return False
    if not last:
        return datetime.now().weekday() == 0
    try:
        days = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
    except Exception:
        return True
    return datetime.now().weekday() == 0 or days >= 7


def cleanup(cfg, force=False):
    """每周一做一次：清理日志、过期的深入浅出、旧的用量记录。"""
    today = today_str()
    if not force and not cleanup_due():
        return
    done = []
    for path in (APP_DIR / "运行日志.txt", LOG_FILE):
        if trim_log(path):
            done.append(path.name)

    keep_days = int(cfg.get("explain_keep_days", 30))
    removed = 0
    if keep_days > 0 and EXPLAIN_DIR.exists():
        cutoff = time.time() - keep_days * 86400
        for f in EXPLAIN_DIR.glob("*.md"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except Exception:
                pass
    # 索引里去掉已经不存在的文件
    index = load_json(EXPLAIN_INDEX, {})
    alive = {k: v for k, v in index.items() if (EXPLAIN_DIR / v).exists()}
    if alive != index:
        save_json(EXPLAIN_INDEX, alive)

    # 用量记录只留最近 400 天
    usage = load_json(USAGE_FILE, {})
    if len(usage) > 400:
        save_json(USAGE_FILE, {k: usage[k] for k in sorted(usage)[-400:]})

    try:
        CLEAN_MARK.write_text(today)
    except Exception:
        pass
    if done or removed:
        log("定期清理：{}{}".format("精简了 " + "、".join(done) + "；" if done else "",
                                  "删除了 {} 篇超过 {} 天的深入浅出".format(removed, keep_days) if removed else ""))


def migrate_old_explains():
    """把旧版存在隐藏文件夹里的讲解，搬到项目的「深入浅出」文件夹并改成可读的名字。"""
    try:
        if not OLD_EXPLAIN_DIR.exists():
            return
        olds = list(OLD_EXPLAIN_DIR.glob("*.md"))
        if not olds:
            return
        EXPLAIN_DIR.mkdir(exist_ok=True)
        by_hash = {}
        for n in load_json(HISTORY_FILE, []):
            if n.get("link"):
                by_hash[hashlib.md5(n["link"].encode("utf-8")).hexdigest()] = n
        for f in olds:
            n = by_hash.get(f.stem)
            if n:
                item = dict(n)
                item.setdefault("date", news_date(n) or today_str())
                dest = explain_cache_path(item)
                remember_explain(item, dest)
            else:
                dest = EXPLAIN_DIR / "早期讲解 {}.md".format(f.stem[:8])
            if not dest.exists():
                dest.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            f.unlink()
        log("已把 {} 篇旧讲解搬到「深入浅出」文件夹".format(len(olds)))
    except Exception as ex:
        log("搬旧讲解失败：{}".format(ex))


def web_search(query, n=5):
    """用 DuckDuckGo 搜索（不需要 Key），返回前几条结果。"""
    from urllib.parse import parse_qs, unquote, urlparse
    r = requests.post("https://html.duckduckgo.com/html/", data={"q": query}, headers=UA, timeout=15)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    out = []
    for res in soup.select(".result"):
        a = res.select_one(".result__a")
        if not a or not a.get("href"):
            continue
        href = a["href"]
        if "uddg=" in href:   # DuckDuckGo 的跳转链接，取出真实地址
            href = unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
        sn = res.select_one(".result__snippet")
        out.append({"title": a.get_text(" ", strip=True), "url": href,
                    "snippet": sn.get_text(" ", strip=True) if sn else ""})
        if len(out) >= n:
            break
    return out


EXPLAIN_TOOLS = [
    {"type": "function", "function": {
        "name": "web_search",
        "description": "在网上搜索背景资料。只在原文信息不够、或有需要查证的概念/数据时使用。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词，简短具体"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "open_url",
        "description": "打开一个网页，读取正文（最多约 4000 字）。用来细读搜索结果里最相关的一两条。",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "网页地址"}}, "required": ["url"]}}},
]

AGENT_RULES = """

# 查资料（你可以自己决定要不要用）
你有两个工具：web_search（搜索）和 open_url（打开网页读正文）。
- 原文已经够讲清楚，就**不要查**，直接写。
- 只有原文信息不足、有不懂的概念、或需要查证某个数字时才查。
- **原文没抓到时，一定要查**：搜同一件事的其他报道（别家媒体、官方博客、论文、发布方公告），不要只盯着原文链接。某个网页打不开就换下一个，别在一处耗光次数。
- 搜索结果的摘要本身也是信息，可以先看摘要判断哪个最值得打开。
- 最多一共调用 {max_calls} 次工具，用完就必须直接写文章。先想清楚最想查什么再查。
- 查到的外部信息也要遵守诚实要求：只用你在搜索结果/网页里真实看到的内容。
- 如果用了查到的资料，在文章最后加一节「参考资料」，列出用到的网页标题和链接。"""


def run_tool(name, args, used_urls, left=0):
    try:
        if name == "web_search":
            res = web_search(args.get("query", ""))
            if not res:
                return "没有搜到结果。"
            return "\n".join("{}. {}\n   {}\n   {}".format(i + 1, r["title"], r["url"], r["snippet"][:200])
                             for i, r in enumerate(res))
        if name == "open_url":
            url = args.get("url", "")
            text = fetch_article_text(url, limit=4000)
            if text:
                used_urls.append(url)
            if text:
                return text
            return ("这个网页打不开（可能太慢或拒绝访问）。不要再打开它了。你还剩 {} 次工具调用："
                    "换个关键词搜索，或者打开搜索结果里其他媒体、官方网站对同一件事的报道。").format(left)
        return "没有这个工具。"
    except Exception as ex:
        return "工具出错：{}".format(ex)


def explain_news(cfg, item, key, progress=None):
    """返回 (文章 markdown, 这次花费)。讲过的直接读缓存，不重复花钱。
    会自己决定要不要上网查资料，但有三道闸：每篇最多查几次、每篇 token 上限、每天花费上限。"""
    path = explain_cache_path(item)
    if path.exists():
        return path.read_text(encoding="utf-8"), 0.0
    from openai import OpenAI
    check_daily_budget(cfg)
    say = progress or (lambda t: None)
    say("读原文")
    article = fetch_article_text(item["link"])
    user = ("请把这条新闻给我讲清楚。\n\n标题：{}\n来源：{}\n摘要：{}\n链接：{}\n\n原文节选（可能不完整）：\n{}"
            .format(item["title"], item["source"], item.get("summary", ""), item["link"],
                    article or "（没能抓到原文，请只基于标题和摘要讲你能确定的部分，必要时可以查资料）"))
    use_agent = cfg.get("explain_agent", True)
    max_calls = int(cfg.get("explain_max_tool_calls", 5))
    max_tokens = int(cfg.get("explain_max_tokens", 60000))
    system = load_explain_method() + (AGENT_RULES.format(max_calls=max_calls) if use_agent else "")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=300)

    cost, tokens, calls, used_urls = 0.0, 0, 0, []
    resp = None
    for _round in range(max_calls + 2):            # 轮数也有上限，防止死循环
        allow_tools = use_agent and calls < max_calls and tokens < max_tokens
        kwargs = {"model": cfg["model"], "messages": messages}
        if use_agent:
            kwargs["tools"] = EXPLAIN_TOOLS
            kwargs["tool_choice"] = "auto" if allow_tools else "none"
        say("写讲解" if not allow_tools else "思考")
        resp = client.chat.completions.create(**kwargs)
        u = getattr(resp, "usage", None)
        try:
            cost += record_usage(cfg, u)
        except Exception as ex:
            log("记录用量失败：{}".format(ex))
        tokens += (getattr(u, "total_tokens", 0) or 0) if u else 0
        msg = resp.choices[0].message
        tcs = getattr(msg, "tool_calls", None) or []
        if not tcs:
            break
        # AI 想用工具：记下它的请求，执行（超额的直接拒绝）
        am = {"role": "assistant", "content": msg.content or "",
              "tool_calls": [{"id": t.id, "type": "function",
                              "function": {"name": t.function.name, "arguments": t.function.arguments}}
                             for t in tcs]}
        rc = getattr(msg, "reasoning_content", None)
        if rc:
            am["reasoning_content"] = rc
        messages.append(am)
        for t in tcs:
            try:
                args = json.loads(t.function.arguments or "{}")
            except Exception:
                args = {}
            if calls >= max_calls or tokens >= max_tokens:
                result = "已经达到查资料的上限，不能再查了，请直接用现有信息写文章。"
            else:
                calls += 1
                what = args.get("query") or args.get("url", "")
                say("{} {}/{}：{}".format("搜索" if t.function.name == "web_search" else "读网页",
                                         calls, max_calls, what[:30]))
                log("深入浅出查资料 {}/{}：{} {}".format(calls, max_calls, t.function.name, what))
                result = run_tool(t.function.name, args, used_urls, left=max_calls - calls)
            messages.append({"role": "tool", "tool_call_id": t.id, "content": result})
    log("深入浅出：查资料 {} 次，约 {} tokens，¥{:.4f}".format(calls, tokens, cost))
    text = ((resp.choices[0].message.content if resp else "") or "").strip()
    if text:
        text += "\n\n---\n\n*来源：{} · [原文链接]({}) · 讲解生成于 {}{}*\n".format(
            item["source"], item["link"], datetime.now().strftime("%Y-%m-%d %H:%M"),
            " · 查了 {} 次资料".format(calls) if calls else " · 没有额外查资料")
        EXPLAIN_DIR.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        remember_explain(item, path)
    return text, cost


class Explainer(QObject):
    done = pyqtSignal(object, str, str)   # item, 文章, 错误信息
    progress = pyqtSignal(str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def start(self, item):
        threading.Thread(target=self._run, args=(item,), daemon=True).start()

    def _run(self, item):
        try:
            key = get_api_key()
            if not key:
                raise RuntimeError("还没设置 DeepSeek API Key")
            text, cost = explain_news(self.cfg, item, key, progress=self.progress.emit)
            if not text:
                raise RuntimeError("DeepSeek 没有返回内容")
            log("深入浅出完成：{}（约 ¥{:.4f}）".format(item["title"], cost))
            self.done.emit(item, text, "")
        except Exception as ex:
            log("深入浅出失败：{}".format(ex))
            self.done.emit(item, "", str(ex))


class ReaderWindow(QWidget):
    """深入浅出的阅读窗口：普通窗口，可以拖动、缩放、关闭。"""

    def __init__(self, item, text):
        super().__init__(None, Qt.WindowType.Window)
        self.setWindowTitle("深入浅出 · " + item["title"])
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(620, 760)
        self.setStyleSheet("QWidget{background:#fbfaf7;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QFrame()
        bar.setObjectName("bar")
        bar.setStyleSheet("#bar{background:#ffffff;border-bottom:1px solid #ebe8e1;}"
                          "QLabel{background:transparent;border:none;}")
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(22, 14, 22, 12)
        src = QLabel("深入浅出 · 来源：{}　<a href='{}' style='color:#3949ab;text-decoration:none;'>打开原文 →</a>"
                     .format(item["source"], item["link"]))
        src.setOpenExternalLinks(True)
        src.setStyleSheet("color:#8a8f98;font-size:12px;")
        bl.addWidget(src)
        lay.addWidget(bar)

        view = QTextBrowser()
        view.setOpenExternalLinks(True)
        view.setFrameShape(QFrame.Shape.NoFrame)
        view.document().setDefaultStyleSheet(
            "body{color:#24292f;} h1{font-size:22px;} h2{font-size:18px;} "
            "p{line-height:170%;margin:0 0 12px 0;} li{line-height:170%;} strong{color:#111;}")
        view.document().setDocumentMargin(26)
        f = view.font()
        f.setPointSize(15)
        view.setFont(f)
        view.setStyleSheet("QTextBrowser{background:#fbfaf7;border:none;}")
        view.setMarkdown(text)
        lay.addWidget(view)

        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.center().x() - self.width() // 2, scr.top() + 60)


class StatusBus(QObject):
    """后台线程通知小人改状态用（Qt 界面只能在主线程改）。"""
    busy = pyqtSignal(str)          # 开始忙：小人去黑板写、亮黄灯
    step = pyqtSignal(str)          # 进度（鼠标停在小人上能看到）
    done = pyqtSignal(bool, str)    # 结束：成功亮蓝灯 / 失败亮紫灯


class Worker(QObject):
    done = pyqtSignal(list, str)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            news, msg = self.job()
        except Exception as ex:
            log("出错：{}".format(ex))
            news, msg = [], "出错了：{}".format(ex)
        self.done.emit(news, msg)

    def job(self):
        seen_list = load_json(SEEN_FILE, None)
        first_run = seen_list is None
        seen_list = seen_list or []
        seen = set(seen_list)

        items = fetch_all(self.cfg["sources"])
        if not items:
            return [], "一条都没抓到，可能是网络有问题"

        new, ids = [], set()
        for x in items:
            if x["id"] not in seen and x["id"] not in ids:
                ids.add(x["id"])
                new.append(x)

        if first_run:
            # 第一次运行：只看今天发布的，其余当作看过，免得一下刷屏
            cands = [x for x in new if x["ts"] and is_today_ts(x["ts"])]
        else:
            # 只推今天发布的新闻，前一天的不要
            cands = [x for x in new if x["ts"] is None or is_today_ts(x["ts"])]
        cands.sort(key=lambda x: x["ts"] or time.time(), reverse=True)
        cands = cands[:40]

        def mark_seen():
            seen_list.extend(x["id"] for x in new)
            save_json(SEEN_FILE, seen_list[-5000:])

        if not cands:
            mark_seen()
            return [], "暂时没有新新闻"

        key = get_api_key()
        if key:
            news = summarize(self.cfg, cands, key)  # 失败会抛异常，下次重试
        else:
            n = int(self.cfg.get("max_news_per_push", 3))
            news = [make_news(c, c["title"], "（还没设置 DeepSeek API Key，先显示原标题）")
                    for c in cands[:n]]
        mark_seen()
        log("本轮：新条目 {} 条，推送 {} 条".format(len(new), len(news)))
        return news, "" if news else "有新内容，但都不太值得推"


# ------------------------------------------------------------------ 气泡卡片
TEXT_DARK = "#1f2430"
TEXT_GRAY = "#6b7280"


def reason_line(it):
    if it.get("explore"):
        icon = "🔭"
    elif it.get("major"):
        icon = "⭐"
    else:
        icon = "💡"
    text = it.get("reason") or ""
    if not text and not it.get("topic"):
        return ""
    tag = "　#" + it["topic"] if it.get("topic") else ""
    return "{} {}{}".format(icon, text, tag).strip()


class SourcesWindow(QWidget):
    """右键「管理信息源」：开关、删除、粘贴链接添加。改完马上生效，不用重启。"""
    added = pyqtSignal(object, object, str)   # 源设置, 试抓到的条目, 错误信息（或 AI 的说明）
    progress = pyqtSignal(str)

    def __init__(self, pet):
        super().__init__(None, Qt.WindowType.Window)
        self.pet = pet
        self.busy = False
        self.setWindowTitle("火柴人 · 信息源")
        self.resize(560, 600)
        self.setStyleSheet(
            "QWidget{background:#fbfaf7;color:#1f2430;font-size:13px;}"
            "QLineEdit{background:#fff;border:1px solid #dcd8cf;border-radius:8px;padding:8px 10px;}"
            "QLineEdit:focus{border-color:#7c8cf8;}"
            "QPushButton{background:#fff;border:1px solid #dcd8cf;border-radius:8px;padding:7px 14px;}"
            "QPushButton:hover{background:#f3f1ec;}"
            "QPushButton#add{background:#3949ab;color:#fff;border:none;}"
            "QPushButton#add:hover{background:#2f3d93;}"
            "QPushButton#add:disabled{background:#9aa3d6;}"
            "QPushButton#del{border:none;color:#9ca3af;padding:4px 8px;background:transparent;}"
            "QPushButton#del:hover{color:#e5484d;}"
            "QScrollArea{border:none;}"
            "QCheckBox::indicator{width:16px;height:16px;border:1.5px solid #c9c4b8;border-radius:4px;background:#fff;}"
            "QCheckBox::indicator:checked{background:#3949ab;border-color:#3949ab;}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 18)
        lay.setSpacing(10)

        tip = QLabel("粘贴网站或 RSS 链接；没有链接就直接写名字（比如「数字生命卡兹克」），AI 会自己上网去找。"
                     "都会先试抓一次，确认能用才加上。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#6b7280;font-size:12px;")
        lay.addWidget(tip)
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("链接，或者直接说名字：如 数字生命卡兹克")
        self.input.returnPressed.connect(self.add)
        row.addWidget(self.input, 1)
        self.add_btn = QPushButton("添加")
        self.add_btn.setObjectName("add")
        self.add_btn.clicked.connect(self.add)
        row.addWidget(self.add_btn)
        lay.addLayout(row)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("font-size:12px;color:#6b7280;")
        self.status.hide()
        lay.addWidget(self.status)

        self.count_lbl = QLabel("")
        self.count_lbl.setStyleSheet("color:#6b7280;font-size:12px;margin-top:6px;")
        lay.addWidget(self.count_lbl)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        lay.addWidget(self.scroll, 1)
        foot = QLabel("取消勾选 = 暂停这个源（设置留着，随时再开）。改动立刻保存到 config.json，下一轮抓新闻就生效。")
        foot.setWordWrap(True)
        foot.setStyleSheet("color:#9ca3af;font-size:11px;")
        lay.addWidget(foot)

        self.added.connect(self.on_added)
        self.find_cost = 0.0
        self.progress.connect(lambda t: self.say("🔎 AI 正在找「{}」：{}　（这次已花 ¥{:.3f}）".format(
            self.want, t, usage_summary()[0]["cost"] - self.find_cost)))
        self.refresh()
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.center().x() - self.width() // 2, scr.top() + 80)

    @property
    def sources(self):
        return self.pet.cfg.setdefault("sources", [])

    def refresh(self):
        box = QWidget()
        v = QVBoxLayout(box)
        v.setContentsMargins(0, 0, 12, 0)
        v.setSpacing(6)
        on = sum(1 for s in self.sources if s.get("enabled", True))
        self.count_lbl.setText("现在的信息源：{} 个，开着 {} 个".format(len(self.sources), on))
        for i, s in enumerate(self.sources):
            card = QFrame()
            card.setObjectName("card")
            card.setStyleSheet("#card{background:#fff;border:1px solid #ebe8e1;border-radius:10px;}"
                               "QLabel,QCheckBox{background:transparent;}")
            h = QHBoxLayout(card)
            h.setContentsMargins(12, 8, 6, 8)
            cb = QCheckBox()
            cb.setChecked(s.get("enabled", True))
            cb.toggled.connect(lambda val, idx=i: self.toggle(idx, val))
            h.addWidget(cb)
            col = QVBoxLayout()
            col.setSpacing(2)
            kind = "RSS" if s.get("type", "rss") == "rss" else "网页"
            name = QLabel("<b>{}</b>　<span style='color:#9ca3af;font-size:11px;'>{}</span>".format(s["name"], kind))
            col.addWidget(name)
            st = SOURCE_STATS.get(s["url"])
            if not s.get("enabled", True):
                info, color = "已暂停", "#9ca3af"
            elif st is None:
                info, color = "还没抓过", "#9ca3af"
            elif st["ok"]:
                info, color = "上次拿到 {} 条".format(st["n"]), ("#3f9b5c" if st["n"] else "#b7791f")
            else:
                info, color = "上次抓取失败", "#e5484d"
            u = s["url"] if len(s["url"]) <= 52 else s["url"][:50] + "…"
            sub = QLabel("<span style='color:{};'>{}</span>　<span style='color:#9ca3af;'>{}</span>".format(color, info, u))
            sub.setStyleSheet("font-size:11px;")
            sub.setToolTip(s["url"] + ("\n" + st.get("err", "") if st and not st["ok"] else ""))
            col.addWidget(sub)
            h.addLayout(col, 1)
            d = QPushButton("删除")
            d.setObjectName("del")
            d.clicked.connect(lambda _=False, idx=i: self.delete(idx))
            h.addWidget(d)
            v.addWidget(card)
        v.addStretch(1)
        self.scroll.setWidget(box)

    def save(self):
        try:
            save_sources(self.sources)
            return True
        except Exception as ex:
            self.say("保存失败：{}".format(ex), "#e5484d")
            return False

    def say(self, text, color="#6b7280"):
        self.status.setText(text)
        self.status.setStyleSheet("font-size:12px;color:{};".format(color))
        self.status.show()

    def toggle(self, idx, val):
        s = self.sources[idx]
        if val:
            s.pop("enabled", None)
        else:
            s["enabled"] = False
        if self.save():
            self.say("{}「{}」".format("已打开" if val else "已暂停", s["name"]))
            log("信息源：{} {}".format("打开" if val else "暂停", s["name"]))
        self.refresh()

    def delete(self, idx):
        s = self.sources[idx]
        ans = QMessageBox.question(self, "删除信息源", "确定删除「{}」吗？\n{}".format(s["name"], s["url"]))
        if ans != QMessageBox.StandardButton.Yes:
            return
        self.sources.pop(idx)
        if self.save():
            self.say("已删除「{}」".format(s["name"]))
            log("信息源：删除 {} {}".format(s["name"], s["url"]))
        self.refresh()

    def add(self):
        url = self.input.text().strip()
        if not url or self.busy:
            return
        norm = lambda x: re.sub(r"^https?://(www\.)?", "", x).rstrip("/").lower()
        for s in self.sources:
            if norm(s["url"]) == norm(url):
                self.say("「{}」已经在列表里了".format(s["name"]), "#b7791f")
                return
        is_link = bool(re.match(r"^(https?://)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?(/\S*)?$", url))
        if not is_link:
            key = get_api_key()
            if not key:
                self.say("让 AI 找源需要 DeepSeek API Key，先右键「设置 DeepSeek API Key」，或者直接贴链接。", "#b7791f")
                return
        self.busy = True
        self.want = url
        self.add_btn.setEnabled(False)
        self.input.setEnabled(False)
        if is_link:
            self.add_btn.setText("检查中…")
            self.say("正在看这个网站怎么抓（找 RSS → 试常见地址 → 读网页），大概 5–30 秒…")
        else:
            self.add_btn.setText("AI 找源中…")
            self.find_cost = usage_summary()[0]["cost"]
            self.say("🔎 AI 正在上网找「{}」，可能要一两分钟…".format(url))
        cfg = self.pet.cfg

        def run():
            try:
                if is_link:
                    src, items = detect_source(url)
                    note = ""
                else:
                    src, items, note = find_source_agent(cfg, url, key, progress=self.progress.emit)
                src["_note"] = note
                self.added.emit(src, items, "")
            except Exception as ex:
                self.added.emit(None, None, str(ex))
        threading.Thread(target=run, daemon=True).start()

    def on_added(self, src, items, err):
        self.busy = False
        self.add_btn.setEnabled(True)
        self.input.setEnabled(True)
        self.add_btn.setText("添加")
        note = src.pop("_note", "") if src else ""
        if err:
            self.say("没加上：" + err, "#e5484d")
            return
        norm = lambda x: re.sub(r"^https?://(www\.)?", "", x).rstrip("/").lower()
        for s in self.sources:
            if norm(s["url"]) == norm(src["url"]):
                self.say("这个网站的 RSS「{}」已经在列表里了".format(s["name"]), "#b7791f")
                return
        names = {s["name"] for s in self.sources}
        base, k = src["name"], 2
        while src["name"] in names:
            src["name"] = "{}{}".format(base, k)
            k += 1
        self.sources.append(src)
        if not self.save():
            self.sources.pop()
            return
        SOURCE_STATS[src["url"]] = {"n": len(items), "ok": True, "t": time.time()}
        mark_items_seen(items)
        log("信息源：添加 {}（{}）{}".format(src["name"], src["type"], src["url"]))
        sample = "、".join("《{}》".format(x["title"][:24]) for x in items[:2])
        self.say("✅ 加好了：{}（{}），试抓到 {} 条，比如 {}。下一轮抓新闻就会用上。{}".format(
            src["name"], "RSS" if src["type"] == "rss" else "网页", len(items), sample,
            ("\nAI 说明：" + note) if note else ""), "#3f9b5c")
        self.input.clear()
        self.refresh()


class Bubble(QWidget):
    def __init__(self, pet):
        super().__init__(None)
        self.pet = pet
        self.items, self.idx = [], 0
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setFixedWidth(330)

        card = QFrame(self)
        self.card = card
        card.setObjectName("card")
        card.setStyleSheet(
            "#card{background:rgba(255,255,255,248);border-radius:14px;"
            "border:1px solid rgba(0,0,0,35);}"
            "QLabel{background:transparent;}"
            "QPushButton{background:#eef2ff;color:#3949ab;border:none;border-radius:8px;"
            "padding:4px 10px;font-size:12px;}"
            "QPushButton:hover{background:#dfe6ff;}"
            "QPushButton:disabled{background:#f3f4f6;color:#c0c4cc;}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(14, 12, 14, 10)
        lay.setSpacing(6)

        self.header = QLabel()
        self.header.setStyleSheet("color:{};font-size:11px;".format(TEXT_GRAY))
        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setStyleSheet("color:{};font-size:15px;font-weight:600;".format(TEXT_DARK))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color:{};font-size:13px;".format(TEXT_DARK))
        self.meta = QLabel()
        self.meta.setStyleSheet("color:#3949ab;font-size:11px;")
        # 推荐理由（为什么推这条），让你看得见它在怎么挑
        self.reason = QLabel()
        self.reason.setWordWrap(True)
        self.reason.setStyleSheet("color:#8a6d1f;font-size:11px;")
        # 顶部一行：标题 + 右上角小叉
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.header)
        top.addStretch(1)
        # 今天的 token 用量和花费（灰色小字，放在小叉左边）
        self.usage_lbl = QLabel()
        self.usage_lbl.setStyleSheet("color:#9ca3af;font-size:11px;")
        self.usage_lbl.setToolTip("今天调用 DeepSeek 消耗的 token 和估算花费")
        top.addWidget(self.usage_lbl)
        self.x_btn = QPushButton("✕")
        self.x_btn.setFixedSize(22, 22)
        self.x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.x_btn.setToolTip("收起")
        self.x_btn.setStyleSheet(
            "QPushButton{background:transparent;color:#9ca3af;border:none;border-radius:11px;"
            "padding:0;font-size:13px;}"
            "QPushButton:hover{background:#f1f2f4;color:#374151;}")
        self.x_btn.clicked.connect(self.hide)
        top.addWidget(self.x_btn)
        lay.addLayout(top)
        for w in (self.title, self.summary, self.reason, self.meta):
            lay.addWidget(w)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.prev_btn = QPushButton("上一条")
        self.next_btn = QPushButton("下一条")
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn.clicked.connect(lambda: self.step(1))
        btns.addWidget(self.prev_btn)
        btns.addStretch(1)
        btns.addWidget(self.next_btn)
        lay.addLayout(btns)

        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self.auto_hide)

    # 显示一组新闻
    def show_items(self, items, header):
        self.items, self.idx, self.header_text = list(items), 0, header
        self.render()

    # 显示一句话（没有新闻时）
    def show_message(self, text, header="火柴人", small=False, ms=8000):
        """small=True：用小字显示多行内容（比如用量统计）。"""
        self.items, self.idx = [], 0
        self.header.setText(header)
        self.usage_lbl.hide()
        if small:
            self.title.hide()
            self.summary.setText(text)
            self.summary.show()
        else:
            self.title.setText(text)
            self.title.show()
            self.summary.hide()
        self.meta.hide()
        self.reason.hide()
        self.prev_btn.hide()
        self.next_btn.hide()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.pop(ms)

    def update_usage(self):
        try:
            t = usage_summary()[0]
            n = t["in"] + t["out"]
            n_txt = "{:.1f}k".format(n / 1000) if n >= 1000 else str(n)
            self.usage_lbl.setText("今日 {} tokens · ¥{:.3f}".format(n_txt, t["cost"]))
            self.usage_lbl.show()
        except Exception:
            self.usage_lbl.hide()

    def render(self):
        it = self.items[self.idx]
        self.update_usage()
        n = len(self.items)
        self.header.setText("📰 {}  {}/{}".format(self.header_text, self.idx + 1, n))
        self.title.setText(it["title"])
        self.title.show()
        self.summary.setText(it.get("summary", ""))
        self.summary.setVisible(bool(it.get("summary")))
        self.meta.setText("{} · {}　双击卡片看原文 →".format(it["source"], it.get("time", "")))
        self.meta.show()
        self.reason.setText(reason_line(it))
        self.reason.setVisible(bool(self.reason.text()))
        self.prev_btn.setVisible(n > 1)
        self.next_btn.setVisible(n > 1)
        self.prev_btn.setEnabled(self.idx > 0)
        self.next_btn.setEnabled(self.idx < n - 1)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pop(int(self.pet.cfg.get("bubble_seconds", 40)) * 1000)

    def step(self, d):
        if self.items:
            self.idx = max(0, min(len(self.items) - 1, self.idx + d))
            self.render()

    def fit(self):
        """按文字实际换行后的高度，重新算卡片高度，保证字全部显示。"""
        inner = self.card.layout()
        m = inner.contentsMargins()
        text_w = self.width() - m.left() - m.right()
        h = m.top() + m.bottom()
        shown = 0
        for i in range(inner.count()):
            item = inner.itemAt(i)
            w = item.widget()
            if w is not None:
                if w.isHidden():
                    continue
                if isinstance(w, QLabel) and w.wordWrap():
                    h += w.heightForWidth(text_w)
                else:
                    h += w.sizeHint().height()
            else:  # 子布局（顶部一行 / 按钮行）：里面的东西全隐藏了就不算高度
                sub = item.layout()
                kids = [sub.itemAt(j).widget() for j in range(sub.count())]
                vis = [k for k in kids if k is not None and not k.isHidden()]
                if not vis:
                    continue
                h += max(k.sizeHint().height() for k in vis)
            shown += 1
        h += inner.spacing() * max(shown - 1, 0)
        self.setFixedHeight(h + 8)

    def pop(self, ms):
        self.fit()
        self.reposition()
        self.show()
        self.raise_()
        self.hide_timer.start(ms)

    def auto_hide(self):
        if self.underMouse():
            self.hide_timer.start(5000)
        else:
            self.hide()

    def reposition(self):
        p = self.pet
        scr = (p.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        x = p.x() - self.width() - 4
        if x < scr.left():
            x = p.x() + p.width() + 4
        y = p.y() + p.height() - self.height() - 10
        y = max(scr.top(), min(y, scr.bottom() - self.height()))
        self.move(x, y)

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.items:
            record_pref(self.pet.cfg, self.items[self.idx], "open")
            webbrowser.open(self.items[self.idx]["link"])

    def contextMenuEvent(self, e):
        if not self.items:
            return
        m = QMenu(self)
        ex = QAction("深入浅出", self)
        ex.triggered.connect(lambda: (record_pref(self.pet.cfg, self.items[self.idx], "explain"),
                                      self.pet.explain(dict(self.items[self.idx]))))
        m.addAction(ex)
        a = QAction("推送到飞书", self)
        a.triggered.connect(self.push_current_to_feishu)
        m.addAction(a)
        af = QAction("推送全文到飞书（深入浅出完整版）", self)
        af.triggered.connect(lambda: self.push_current_to_feishu(full=True))
        m.addAction(af)
        o = QAction("打开原文", self)
        o.triggered.connect(lambda: (record_pref(self.pet.cfg, self.items[self.idx], "open"),
                                     webbrowser.open(self.items[self.idx]["link"])))
        m.addAction(o)
        if self.items[self.idx].get("topic"):
            m.addSeparator()
            d = QAction("不感兴趣（少推「{}」）".format(self.items[self.idx]["topic"]), self)
            d.triggered.connect(self.dislike_current)
            m.addAction(d)
        self.hide_timer.stop()
        m.exec(e.globalPos())
        self.hide_timer.start(15000)

    def dislike_current(self):
        record_pref(self.pet.cfg, self.items[self.idx], "dislike")
        self.flash_header("👌 记住了，以后少推这类")

    def push_current_to_feishu(self, full=False):
        """把当前这条新闻手动推到飞书（不占每天的自动名额）。full=True：推深入浅出全文。"""
        if self.items:
            record_pref(self.pet.cfg, self.items[self.idx], "feishu")
        if not feishu_conf():
            self.flash_header("⚠️ 还没设置飞书推送（右键小人 → 设置飞书推送）")
            return
        item = dict(self.items[self.idx])
        self.flash_header("⏳ 正在推送到飞书…", keep=True)
        self.push_result = None

        self.push_why = ""
        self.push_step = "check"

        bus = self.pet.bus

        def run():
            bus.busy.emit("推送到飞书：" + item.get("title", ""))
            bus.step.emit("检查链接")
            ok, why = link_ok_for_feishu(self.pet.cfg, item.get("link", ""))
            if not ok:
                self.push_why = why
                log("飞书跳过：{}（{}）".format(item.get("title", ""), why))
                self.push_result = False
                bus.done.emit(False, "没推到飞书：" + why)
                return
            self.push_step = "write"
            bus.step.emit("写深入浅出全文" if full else "写深入浅出精简版")
            explains = explains_for_feishu(self.pet.cfg, [item], progress=bus.step.emit, full=full)
            self.push_step = "send"
            bus.step.emit("发送到飞书")
            if full:
                ex = explains.get(item["link"])
                if not ex:
                    self.push_why = "全文没写出来（看看运行日志）"
                    self.push_result = False
                    bus.done.emit(False, "深入浅出全文没写出来")
                    return
                self.push_result = send_feishu_full(item, ex)
            else:
                self.push_result = send_feishu([item], title="📌 AI 新闻推荐", explains=explains)
            bus.done.emit(self.push_result, "已推送到飞书：{}".format(item.get("title", "")) if self.push_result
                          else "推送飞书失败，看看运行日志")
        threading.Thread(target=run, daemon=True).start()

        def check(tries=[0]):
            if self.push_result is None and tries[0] < 2400:      # 最多等 10 分钟（要先写深入浅出）
                tries[0] += 1
                if tries[0] % 8 == 0 and self.push_step == "write":
                    self.flash_header("📖 先写深入浅出全文，再推到飞书（约 1 分钟）…" if full
                                      else "📖 先写精简版讲解，再推到飞书…", keep=True)
                QTimer.singleShot(250, check)
                return
            if self.push_result:
                self.flash_header("✅ 全文已推送到飞书" if full else "✅ 已推送到飞书")
            elif self.push_why:
                self.flash_header("🚫 没推：" + self.push_why)
            else:
                self.flash_header("❌ 推送失败，看看运行日志")
        QTimer.singleShot(250, check)

    def flash_header(self, text, keep=False):
        """在卡片左上角临时显示一句提示，3 秒后恢复。"""
        self.header.setText(text)
        if not keep and self.items:
            idx_items = (self.idx, len(self.items))

            def back():
                if self.items and (self.idx, len(self.items)) == idx_items:
                    self.header.setText("📰 {}  {}/{}".format(self.header_text, self.idx + 1, len(self.items)))
            QTimer.singleShot(3000, back)


# ------------------------------------------------------------------ 桌宠本体
class Pet(QWidget):
    W, H = 110, 120

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.resize(self.W, self.H)
        scr = QGuiApplication.primaryScreen().availableGeometry()
        self.move(scr.right() - self.W - 40, scr.bottom() - self.H - 40)

        self.phase = 0.0
        self.blink = 0
        self.fetching = False
        self.manual = False
        self.last_fetch = 0.0
        self.quiet_until = 0.0
        self.queue = []
        self.flash_until = 0
        self.flash_color = QColor("#5aa9ff")
        self.last_msg = ""
        self.feishu_pending = []
        self.feishu_busy = False
        self.history = only_recent(load_json(HISTORY_FILE, []), self.history_days())
        self.press_pos = None
        self.moved = False

        self.bubble = Bubble(self)
        self.worker = Worker(cfg)
        self.worker.done.connect(self.on_done)
        self.explaining = None      # 正在讲解的新闻标题
        self.readers = []
        self.explainer = Explainer(cfg)
        self.explainer.done.connect(self.on_explained)
        self.explain_step = ""
        self.explainer.progress.connect(self.on_explain_progress)
        self.bus = StatusBus()
        self.bus.busy.connect(self.on_busy)
        self.bus.step.connect(self.on_explain_progress)
        self.bus.done.connect(self.on_busy_done)

        self.anim = QTimer(self)
        self.anim.timeout.connect(self.tick)
        self.anim.start(33)
        self.brain = QTimer(self)
        self.brain.timeout.connect(self.think)
        self.brain.start(30 * 1000)
        QTimer.singleShot(2500, lambda: self.fetch_now(manual=False))
        self.update_tip()

    # ---------- 决策 ----------
    def is_quiet(self):
        if time.time() < self.quiet_until:
            return True
        q = self.cfg.get("quiet_hours")
        if q and len(q) == 2:
            h = datetime.now().hour
            a, b = q
            return (a <= h < b) if a <= b else (h >= a or h < b)
        return False

    def in_quiet_hours(self):
        q = self.cfg.get("quiet_hours")
        if q and len(q) == 2:
            h = datetime.now().hour
            a, b = q
            return (a <= h < b) if a <= b else (h >= a or h < b)
        return False

    def flush_feishu(self):
        """自动推送到飞书：每天最多 feishu_daily_limit 条（默认 1 条，推 DeepSeek 排第一的）。"""
        if self.cfg.get("feishu_auto_push", "desktop") == "cloud":
            self.feishu_pending = []   # 每日推送交给 GitHub 云端做了，桌面只留手动推送
            return
        if not self.feishu_pending or self.feishu_busy or not feishu_conf() or self.in_quiet_hours():
            return
        limit = int(self.cfg.get("feishu_daily_limit", 1))
        left = limit - feishu_sent_today()
        if left <= 0:
            self.feishu_pending = []   # 今天的名额用完了，其余只在桌面看
            return
        pending, self.feishu_pending = self.feishu_pending, []
        self.feishu_busy = True

        def run():
            # 按 DeepSeek 的排序，挑前几条「飞书里能打开」的推
            items = []
            for n in pending:
                if len(items) >= left:
                    break
                ok, why = link_ok_for_feishu(self.cfg, n.get("link", ""))
                if ok:
                    items.append(n)
                else:
                    log("飞书跳过：{}（{}）".format(n.get("title", ""), why))
            if items:
                self.bus.busy.emit("自动推送今日新闻到飞书")
                self.bus.step.emit("写深入浅出")
                explains = explains_for_feishu(self.cfg, items, progress=self.bus.step.emit)
                self.bus.step.emit("发送到飞书")
                if send_feishu(items, title="🤖 今日 AI 新闻", explains=explains):
                    add_feishu_sent(len(items))
                    self.bus.done.emit(True, "已自动推送到飞书：" + items[0].get("title", ""))
                else:
                    self.feishu_pending = items + self.feishu_pending  # 失败了下次再试
                    self.bus.done.emit(False, "自动推送飞书失败，稍后重试")
            self.feishu_busy = False
        threading.Thread(target=run, daemon=True).start()

    def think(self):
        cleanup(self.cfg)          # 每周一才会真正执行
        self.history = only_recent(self.history, self.history_days())
        self.queue = only_today(self.queue)
        self.feishu_pending = only_today(self.feishu_pending)
        self.flush_feishu()
        now = time.time()
        idle = user_idle_seconds()
        interval = float(self.cfg.get("check_every_minutes", 30)) * 60
        need_idle = float(self.cfg.get("idle_minutes_before_fetch", 3)) * 60
        # 到点了，并且你正好走开了（或者已经拖太久了），就去抓
        if not self.fetching and now - self.last_fetch >= interval:
            if idle >= need_idle or now - self.last_fetch >= interval * 2:
                self.fetch_now(manual=False)
        # 有攒着的新闻，且你回来了，就推给你
        if self.queue and not self.bubble.isVisible() and idle < 60 and not self.is_quiet():
            self.push_queue()

    def fetch_now(self, manual=True):
        if self.fetching:
            return
        self.fetching = True
        self.manual = manual
        self.flash_until = 0
        self.fish_since = time.time()           # 动画：从黑洞里掏报纸
        log("开始抓新闻")
        self.worker.start()

    def on_done(self, news, msg):
        self.fetching = False
        self.last_fetch = time.time()
        # 动画：把报纸摔飞
        self.fish_end = time.time()
        if news:
            self.queue.extend(news)
            self.feishu_pending.extend(news)
            self.flush_feishu()
            if user_idle_seconds() < 60 and not self.is_quiet():
                self.push_queue()
        elif msg:
            # 不弹卡片：出问题亮紫灯；没有新新闻就直接回到绿灯
            bad = ("出错" in msg) or ("没抓到" in msg)
            if bad:
                self.flash_color = QColor("#a78bfa")
                self.flash_until = time.time() + 8
            self.last_msg = msg
        self.manual = False
        self.update_tip()

    def push_queue(self):
        items, self.queue = self.queue, []
        self.history = only_recent(items + self.history, self.history_days())[:150]
        save_json(HISTORY_FILE, self.history)
        self.bubble.show_items(items, "新的 AI 新闻")
        self.update_tip()

    def update_tip(self):
        if self.queue:
            self.setToolTip("有 {} 条新闻，点我看".format(len(self.queue)))
        else:
            tip = "点我看今天的新闻，右键有更多选项"
            if getattr(self, "explaining", None):
                tip = "正在深入浅出：{}\n当前：{}\n{}".format(self.explaining, getattr(self, "explain_step", ""), tip)
            if self.last_msg:
                tip = "上次抓取：{}\n{}".format(self.last_msg, tip)
            self.setToolTip(tip)

    # ---------- 交互 ----------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.press_pos = e.globalPosition().toPoint() - self.pos()
            self.moved = False

    def mouseMoveEvent(self, e):
        if self.press_pos is not None and e.buttons() & Qt.MouseButton.LeftButton:
            new = e.globalPosition().toPoint() - self.press_pos
            if (new - self.pos()).manhattanLength() > 3:
                self.moved = True
            self.move(new)
            if self.bubble.isVisible():
                self.bubble.reposition()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            if not self.moved:
                self.on_click()
            self.press_pos = None

    def on_click(self):
        if self.bubble.isVisible():
            self.bubble.hide()
        elif self.queue:
            self.push_queue()
        elif on_day(self.history, 0):
            self.bubble.show_items(on_day(self.history, 0)[:30], "今天的新闻")
        else:
            self.bubble.show_message("今天还没有新闻～右键选「立刻抓一次」，或者「看新闻」翻翻前几天的")

    def history_days(self):
        return max(1, min(int(self.cfg.get("history_days", 3)), 7))

    def show_day(self, days_ago):
        items = on_day(self.history, days_ago)
        name = ["今天", "昨天", "前天"][days_ago] if days_ago < 3 else "{}天前".format(days_ago)
        if items:
            self.bubble.show_items(items[:30], "{}的新闻（{}）".format(name, day_str(days_ago)[5:]))
        else:
            self.bubble.show_message("{}没有推过新闻".format(name))

    def contextMenuEvent(self, e):
        m = QMenu(self)
        a1 = QAction("立刻抓一次新闻", self)
        a1.triggered.connect(lambda: self.fetch_now(manual=True))
        a2 = QAction("看今天的新闻（{} 条）".format(len(on_day(self.history, 0))), self)
        a2.triggered.connect(lambda: self.show_day(0))
        if time.time() < self.quiet_until:
            a3 = QAction("取消免打扰", self)
            a3.triggered.connect(self.cancel_quiet)
        else:
            a3 = QAction("免打扰 1 小时", self)
            a3.triggered.connect(self.quiet_hour)
        au = QAction("Token 用量和花费", self)
        au.triggered.connect(self.show_usage)
        a4 = QAction("设置 DeepSeek API Key", self)
        a4.triggered.connect(self.ask_key)
        af = QAction("设置飞书推送", self)
        af.triggered.connect(self.ask_feishu)
        at = QAction("给飞书发条测试消息", self)
        at.triggered.connect(self.test_feishu)
        ae = QAction("打开「深入浅出」文件夹", self)
        ae.triggered.connect(lambda: (EXPLAIN_DIR.mkdir(exist_ok=True), subprocess.Popen(["open", str(EXPLAIN_DIR)])))
        asrc = QAction("管理信息源（{} 个）".format(len(self.cfg.get("sources", []))), self)
        asrc.triggered.connect(self.manage_sources)
        a5 = QAction("打开配置文件夹", self)
        a5.triggered.connect(lambda: subprocess.Popen(["open", str(APP_DIR)]))
        ar = QAction("重启火柴人", self)
        ar.triggered.connect(self.restart)
        a6 = QAction("退出", self)
        a6.triggered.connect(QApplication.quit)
        for a in (a1, a2, a3, au, asrc, a4, af, at, ae, a5):
            m.addAction(a)
        older = QMenu("看前几天的新闻", m)
        for d in range(1, self.history_days()):
            name = ["今天", "昨天", "前天"][d] if d < 3 else "{}天前".format(d)
            act = QAction("{}（{}，{} 条）".format(name, day_str(d)[5:], len(on_day(self.history, d))), self)
            act.triggered.connect(lambda _=False, dd=d: self.show_day(dd))
            older.addAction(act)
        m.insertMenu(a3, older)
        m.addSeparator()
        # 呼吸灯说明（子菜单，鼠标移上去就能看到）
        pm = m.addMenu("我的偏好")
        pv = QAction("查看学到的偏好", self)
        pv.triggered.connect(self.show_prefs)
        pm.addAction(pv)
        paused = load_prefs().get("paused")
        pp = QAction("恢复学习" if paused else "暂停学习", self)
        pp.triggered.connect(self.toggle_learning)
        pm.addAction(pp)
        pc = QAction("清空偏好，重新开始", self)
        pc.triggered.connect(self.clear_prefs)
        pm.addAction(pc)
        lights = m.addMenu("呼吸灯")
        for color, text in (("#4cd17a", "绿灯：待命"),
                            ("#ffbe28", "黄灯闪烁：正在抓新闻 / 写深入浅出 / 推送飞书"),
                            ("#4cd17a", "绿灯呼吸：推送飞书成功"),
                            ("#a78bfa", "紫灯呼吸：抓的时候出了问题（鼠标停在小人上看原因）"),
                            ("#ff5a5f", "红灯 + 数字：有新闻没看")):
            lights.addAction(QAction(dot_icon(color), text, self))
        m.addSeparator()
        m.addAction(ar)
        m.addAction(a6)
        m.exec(e.globalPos())

    def manage_sources(self):
        w = getattr(self, "sources_win", None)
        if w is None or not w.isVisible():
            self.sources_win = w = SourcesWindow(self)
        else:
            w.refresh()
        w.show()
        w.raise_()
        w.activateWindow()

    def show_prefs(self):
        prefs = load_prefs()
        sc = topic_scores(self.cfg)
        lines = []
        if prefs.get("paused") or not self.cfg.get("learning", True):
            lines.append("⏸ 学习已暂停，推荐只按 config 里的 interests 来")
        if not sc:
            lines.append("还没学到什么。双击打开、深入浅出、推到飞书会算「喜欢」，右键「不感兴趣」算「不喜欢」。")
        else:
            for t, v in sorted(sc.items(), key=lambda x: -x[1]):
                bar = "▮" * min(int(abs(v) + 0.5), 8) or "·"
                lines.append("{} {}　{}".format("👍" if v > 0 else "👎", t, bar))
            if sum(abs(v) for v in sc.values()) < 4:
                lines.append("（行为还太少，暂时不影响推荐）")
        lines.append("保险：重大新闻照推 · 每轮留 {} 个圈外推荐 · 偏好 {} 天减半".format(
            self.cfg.get("explore_slots", 1), self.cfg.get("pref_half_life_days", 14)))
        self.bubble.show_message("\n".join(lines), header="🧭 学到的偏好", small=True, ms=20000)

    def toggle_learning(self):
        prefs = load_prefs()
        prefs["paused"] = not prefs.get("paused")
        save_json(PREFS_FILE, prefs)
        self.bubble.show_message("已暂停学习，推荐只按你写的 interests 来" if prefs["paused"]
                                 else "已恢复学习", header="🧭 我的偏好")

    def clear_prefs(self):
        save_json(PREFS_FILE, {"events": [], "paused": load_prefs().get("paused", False)})
        self.bubble.show_message("偏好已清空，从头开始学", header="🧭 我的偏好")

    def explain(self, item):
        if self.explaining:
            self.bubble.flash_header("⏳ 还在讲上一条，稍等一下")
            return
        cached = explain_cache_path(item).exists()
        self.explaining = item["title"]
        self.bubble.flash_header("📖 打开讲解…" if cached else "📖 正在读原文、写讲解（约 1 分钟）…")
        self.update_tip()
        self.explainer.start(item)

    def on_busy(self, label):
        self.explaining = label
        self.explain_step = ""
        self.flash_until = 0
        self.update_tip()

    def on_busy_done(self, ok, msg):
        self.explaining = None
        self.explain_step = ""
        self.flash_color = QColor("#4cd17a") if ok else QColor("#a78bfa")   # 成功：绿灯呼吸；失败：紫灯
        self.flash_until = time.time() + 8
        self.last_msg = msg
        self.update_tip()

    def on_explain_progress(self, step):
        self.explain_step = step
        self.update_tip()

    def on_explained(self, item, text, err):
        self.explaining = None
        self.update_tip()
        if err:
            self.flash_color = QColor("#a78bfa")
            self.flash_until = time.time() + 8
            self.last_msg = "深入浅出失败：" + err
            self.update_tip()
            return
        alive = []
        for r in self.readers:                 # 关掉的窗口已经被销毁了，跳过它们
            try:
                if r.isVisible():
                    alive.append(r)
            except RuntimeError:
                pass
        w = ReaderWindow(item, text)
        self.readers = alive + [w]
        w.show()
        w.raise_()
        w.activateWindow()

    def restart(self):
        """启动一个新的自己，然后退出当前这个（用来加载改过的代码）。"""
        try:
            log_path = APP_DIR / "运行日志.txt"
            out = open(log_path, "a", encoding="utf-8")
            out.write("==== {} 右键重启 ====\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            out.flush()
            subprocess.Popen([sys.executable, str(APP_DIR / "pet.py")], cwd=str(APP_DIR),
                             stdout=out, stderr=out, start_new_session=True)
        except Exception as ex:
            self.bubble.show_message("重启失败：{}".format(ex))
            return
        QApplication.quit()

    def show_usage(self):
        today, month, total = usage_summary()

        def line(name, t):
            return "{}　¥{:.3f}　{} 次 · {:,} tokens".format(
                name, t["cost"], t["calls"], t["in"] + t["out"])

        text = "\n".join([line("今天", today), line("本月", month), line("累计", total)])
        self.usage_text = text
        self.bubble.show_message(text + "\n余额　查询中…", header="📊 DeepSeek 用量", small=True, ms=20000)
        key = get_api_key()
        if not key:
            self.bubble.show_message(text + "\n余额　还没设置 API Key", header="📊 DeepSeek 用量", small=True, ms=20000)
            return
        self.balance_result = None

        def run():
            self.balance_result = fetch_balance(key, self.cfg["base_url"]) or "查询失败"
        threading.Thread(target=run, daemon=True).start()

        def done(tries=[0]):
            if self.balance_result is None and tries[0] < 20:
                tries[0] += 1
                QTimer.singleShot(300, done)
                return
            self.bubble.show_message(self.usage_text + "\n余额　{}".format(self.balance_result or "查询超时"),
                                     header="📊 DeepSeek 用量", small=True, ms=20000)
        QTimer.singleShot(300, done)

    def quiet_hour(self):
        self.quiet_until = time.time() + 3600
        self.bubble.show_message("好的，一小时内我不打扰你 🤫")

    def cancel_quiet(self):
        self.quiet_until = 0
        self.bubble.show_message("我回来啦")

    def ask_key(self):
        key, ok = QInputDialog.getText(
            None, "设置 DeepSeek API Key",
            "粘贴你的 DeepSeek API Key（sk- 开头）\n只保存在你这台电脑上：",
            QLineEdit.EchoMode.Password)
        if ok and key.strip():
            save_api_key(key)
            self.bubble.show_message("Key 存好了！右键「立刻抓一次」试试吧")

    def ask_feishu(self):
        old = load_json(FEISHU_FILE, {})
        url, ok = QInputDialog.getText(
            None, "设置飞书推送",
            "粘贴飞书群「自定义机器人」的 webhook 地址\n（https://open.feishu.cn/open-apis/bot/v2/hook/ 开头；留空=关闭飞书推送）：",
            QLineEdit.EchoMode.Normal, old.get("webhook", ""))
        if not ok:
            return
        url = url.strip()
        if not url:
            save_json(FEISHU_FILE, {})
            self.bubble.show_message("已关闭飞书推送")
            return
        secret, ok2 = QInputDialog.getText(
            None, "设置飞书推送",
            "如果机器人开启了「签名校验」，粘贴签名密钥；没开就直接点 OK：",
            QLineEdit.EchoMode.Password, old.get("secret", ""))
        save_json(FEISHU_FILE, {"webhook": url, "secret": secret.strip() if ok2 else ""})
        try:
            os.chmod(FEISHU_FILE, 0o600)
        except Exception:
            pass
        self.test_feishu()

    def test_feishu(self):
        if not feishu_conf():
            self.bubble.show_message("还没设置飞书推送，右键选「设置飞书推送」")
            return
        demo = [{"title": "测试消息：飞书推送已接通 🎉", "summary": "以后抓到值得看的 AI 新闻，会同时推到这个群里。",
                 "link": "https://www.feishu.cn", "source": "火柴人"}]

        def run():
            ok = send_feishu(demo, title="🤖 火柴人 · 测试")
            self.feishu_test_result = ok
        self.feishu_test_result = None
        threading.Thread(target=run, daemon=True).start()
        self.bubble.show_message("正在给飞书发测试消息…")
        QTimer.singleShot(4000, lambda: self.bubble.show_message(
            "飞书收到了吗？收到就说明接通了 ✅" if self.feishu_test_result
            else "发送失败了，打开「新闻」文件夹里的 运行日志.txt 看看原因"))

    # ---------- 画画 ----------
    def tick(self):
        busy = bool(self.explaining) and not self.fetching
        if busy and not getattr(self, "_was_teach", False):
            self.teach_since = time.time()            # 开始：掏黑板
        elif not busy and getattr(self, "_was_teach", False):
            self.teach_end = time.time()              # 结束：收黑板
        self._was_teach = busy
        self.phase += 0.06
        if self.blink > 0:
            self.blink -= 1
        elif random.random() < 0.008:
            self.blink = 5
        self.update()

    CATCH_LEN = 1.2      # 抓完后摔报纸的动画时长（秒）

    def draw_fetch(self, p, t, ground, mode, ink):
        """抓新闻：小黑洞 → 手伸进去掏出一份报纸 → 拿着看 → 抓完了把报纸摔飞 → 回到平时。返回头的位置。"""
        W = self.W
        now = time.time()
        pen = QPen(ink, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)

        def limb(origin, length, angle_deg):
            a = math.radians(angle_deg)
            return QPointF(origin.x() + length * math.sin(a), origin.y() + length * math.cos(a))

        def ease(x):
            x = max(0.0, min(1.0, x))
            return 1 - (1 - x) ** 2

        def lerp(a, b, f):
            return QPointF(a.x() + (b.x() - a.x()) * f, a.y() + (b.y() - a.y()) * f)

        # ---- 阶段 ----
        if mode == "fish":
            el = now - getattr(self, "fish_since", now)
            if el < 0.3:
                stage, k = "open", el / 0.3
            elif el < 0.6:
                stage, k = "reach", (el - 0.3) / 0.3
            elif el < 0.95:
                stage, k = "pull", (el - 0.6) / 0.35
            else:
                stage, k = "read", 1.0
            w = ease(el / 0.3)
        else:
            c = min(1.0, (now - self.fish_end) / self.CATCH_LEN)
            stage, k = "slam", c
            w = 1 - ease((c - 0.45) / 0.5) if c > 0.45 else 1.0

        # ---- 就是桌面上那个小人：同一个位置、同样的起伏和站姿 ----
        cx = W / 2
        bob = math.sin(t) * 1.5
        hip = QPointF(cx, ground - 34 + bob)
        neck = QPointF(cx, hip.y() - 30)
        head_c = QPointF(cx, neck.y() - 14)
        shoulder = QPointF(cx, neck.y() + 5)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 35))
        sw_ = 34 + bob
        p.drawEllipse(QRectF(cx - sw_ / 2, ground - 3, sw_, 7))
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        swing = 12 + math.sin(t) * 2
        for sgn in (1, -1):
            knee = limb(hip, 17, swing * sgn)
            p.drawLine(hip, knee)
            p.drawLine(knee, limb(knee, 17, swing * sgn))
        p.drawLine(neck, hip)
        sway = math.sin(t) * 6
        idle_arms = []
        for up_, lo_ in ((25 + sway, 15 + sway), (-25 - sway, -15 - sway)):
            e_ = limb(shoulder, 14, up_)
            idle_arms.append((shoulder, e_, limb(e_, 14, lo_)))

        # ---- 小黑洞（右边，手的高度） ----
        hole = QRectF(shoulder.x() + 23, shoulder.y() - 9, 15, 24)
        hole_open = mode == "fish" and not (stage == "read")
        if mode == "fish":
            el = now - getattr(self, "fish_since", now)
            h_ripple, h_frame, h_fade = min(1.0, el / 0.2), ease((el - 0.05) / 0.25), 1 - ease((el - 0.78) / 0.17)

        def outside(r):
            pth = QPainterPath()
            pth.setFillRule(Qt.FillRule.OddEvenFill)
            pth.addRect(QRectF(-50, -50, W + 100, self.H + 100))
            pth.addEllipse(r)
            return pth

        # ---- 右手 ----
        a0 = idle_arms[0][2]
        reach = QPointF(hole.center().x() + 3, hole.center().y())
        read = QPointF(shoulder.x() + 11, shoulder.y() + 8 + math.sin(t * 1.3) * 0.8)   # 拿着报纸在身前看
        paper_off = QPointF(-9, -3)                                   # 报纸中心相对手的位置
        released, paper = False, None
        if stage == "open":
            hand = a0
        elif stage == "reach":
            hand = lerp(a0, reach, ease(k))
        elif stage == "pull":
            hand = lerp(reach, read, ease(k))
        elif stage == "read":
            hand = read
        else:   # slam：往右上一抬 → 往左狠狠一甩，报纸飞出去
            wind = QPointF(read.x() + 7, read.y() - 2)
            throw_end = QPointF(shoulder.x() - 15, shoulder.y() + 3)
            if k < 0.18:
                hand = lerp(read, wind, ease(k / 0.18))
            else:
                hand = lerp(wind, throw_end, ease((k - 0.18) / 0.14))
            released = k >= 0.27

        # ---- 两只手臂：动作姿势和平时的姿势按 w 混合 ----
        mid = lerp(shoulder, hand, 0.5)
        elbow = QPointF(mid.x() + 3, mid.y() + 6)
        arms = [(shoulder, elbow, hand), idle_arms[1]]
        in_hole = stage in ("reach", "pull") and hole_open
        for i, (s_, e_, h_) in enumerate(arms):
            s0, e0, h0 = idle_arms[i]
            a_, b_, c_ = lerp(s0, s_, w), lerp(e0, e_, w), lerp(h0, h_, w)
            p.save()
            if i == 0 and in_hole:
                p.setClipPath(outside(hole))                          # 伸进黑洞的部分看不见
            p.drawLine(a_, b_)
            p.drawLine(b_, c_)
            p.restore()
            if i == 0:
                hand = c_

        # ---- 报纸 ----
        if stage in ("pull", "read") or (stage == "slam" and not released):
            paper, rot, sc = QPointF(hand.x() + paper_off.x(), hand.y() + paper_off.y()), 0.0, 1.0
            if stage == "pull":
                sc = 0.6 + 0.4 * ease(k)
        elif stage == "slam":
            f = (k - 0.27) / 0.4                                    # 摔出去之后飞走
            start = QPointF(shoulder.x() - 15 + paper_off.x(), shoulder.y() + 3 + paper_off.y())
            paper = QPointF(start.x() - 150 * f, start.y() - 18 * math.sin(min(1.0, f) * math.pi) + 10 * f)
            rot, sc = -600 * f, 1.0
            if f > 1:
                paper = None
        if paper is not None:
            p.save()
            if stage == "pull" and hole_open:
                p.setClipPath(outside(hole))                          # 从黑洞里被拽出来
            p.translate(paper)
            p.rotate(rot + (math.sin(t * 1.7) * 2 if stage == "read" else 0))
            p.scale(1.55 * sc, 1.55 * sc)
            p.setPen(QPen(QColor("#9ca3af"), 0.7))
            p.setBrush(QColor("#fafaf7"))
            p.drawRect(QRectF(-7, -5, 14, 10))
            p.setPen(QPen(QColor("#6b7280"), 0.8))
            p.drawLine(QPointF(-5, -2.6), QPointF(1, -2.6))
            for yy in (0, 2.4):
                p.drawLine(QPointF(-5, yy), QPointF(5, yy))
            p.restore()

        # ---- 黑洞画在手和报纸上面，盖住被吞掉的边缘 ----
        if hole_open and h_fade > 0:
            self.draw_door(p, hole, ink, ripple=h_ripple, frame=h_frame, fade=h_fade, wobble=stage == "reach")

        # ---- 头和脸：看黑洞 / 看报纸 ----
        p.setPen(pen)
        p.setBrush(QColor("white"))
        p.drawEllipse(head_c, 12, 12)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        if stage in ("open", "reach", "pull"):
            look, lookdown = 2.5 * w, 0.0
        elif stage == "read":
            look, lookdown = 1.0, 1.5
        else:
            look, lookdown = -2.0 * w * (1 if k < 0.6 else 0), 0.0
        for ex in (head_c.x() - 4.5 + look, head_c.x() + 4.5 + look):
            if self.blink:
                p.drawRect(QRectF(ex - 2.5, head_c.y() - 1.5 + lookdown, 5, 1.6))
            else:
                p.drawEllipse(QPointF(ex, head_c.y() - 1.5 + lookdown), 1.8, 2.4)
        p.setPen(QPen(ink, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(QRectF(head_c.x() - 4 + look, head_c.y() + 1 + lookdown * 0.5, 8, 6), 200 * 16, 140 * 16)
        return head_c

    def draw_door(self, p, r, ink, ripple, frame, fade, wobble=False):
        """虚空黑洞：空气先泛起透明波纹，然后撕开一个等人高的大黑洞，里面缓缓打旋；用完收拢消失。"""
        if fade <= 0:
            return
        from PyQt6.QtGui import QRadialGradient
        p.save()
        c = r.center()
        now = time.time()
        # 透明波纹（像空气被搅动）
        if ripple < 1 or wobble:
            for j in range(3):
                ph = ((ripple if not wobble else (now * 2.5) % 1) + j / 3) % 1
                a = int(110 * (1 - ph) * fade)
                p.setPen(QPen(QColor(110, 130, 160, a), 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(c, r.width() / 2 * (0.3 + 0.9 * ph), r.height() / 2 * (0.3 + 0.8 * ph))
        # 黑洞：先竖着撕开一道缝，再张开；结束时收拢成一个点
        if frame > 0:
            hh = r.height() / 2 * min(1.0, frame * 1.6) * (0.25 + 0.75 * fade)
            ww = r.width() / 2 * frame * fade
            if ww > 0.3 and hh > 0.3:
                g = QRadialGradient(c, 1.0)
                g.setCoordinateMode(QRadialGradient.CoordinateMode.ObjectBoundingMode)
                g.setCenter(0.5, 0.5)
                g.setFocalPoint(0.5, 0.5)
                g.setRadius(0.5)
                g.setColorAt(0.0, QColor(0, 0, 0, 255))
                g.setColorAt(0.62, QColor(8, 8, 14, 250))
                g.setColorAt(0.85, QColor(40, 30, 60, 170))
                g.setColorAt(1.0, QColor(90, 80, 120, 0))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(g)
                p.drawEllipse(c, ww * 1.15, hh * 1.08)
                # 里面缓缓打旋的暗纹
                p.setBrush(Qt.BrushStyle.NoBrush)
                for j in range(3):
                    sc = 0.35 + 0.22 * j
                    p.setPen(QPen(QColor(120, 110, 160, int(70 * fade)), 1))
                    start = int(((now * (60 + 25 * j)) + j * 120) % 360 * 16)
                    p.drawArc(QRectF(c.x() - ww * sc, c.y() - hh * sc, 2 * ww * sc, 2 * hh * sc), start, 110 * 16)
        p.restore()

    def draw_bang(self, p, c, k):
        """踢中时的黄色爆炸星 + 「砰」。"""
        p.save()
        r = 6 + 9 * k
        pts = []
        for i in range(16):
            rr = r if i % 2 == 0 else r * 0.5
            ang = i * math.pi / 8
            pts.append(QPointF(c.x() + math.cos(ang) * rr, c.y() + math.sin(ang) * rr))
        p.setOpacity(max(0.0, 1 - k * 0.8))
        p.setPen(QPen(QColor("#f59e0b"), 1.5))
        p.setBrush(QColor("#fde047"))
        p.drawPolygon(QPolygonF(pts))
        f = QFont()
        f.setBold(True)
        f.setPixelSize(int(7 + 4 * k))
        p.setFont(f)
        p.setPen(QColor("#b45309"))
        p.drawText(QRectF(c.x() - 12, c.y() - 7, 24, 14), Qt.AlignmentFlag.AlignCenter, "砰")
        p.restore()

    def draw_blackboard(self, p, t, ground, k=1.0, write=True, full=False, fly=0.0):
        """小黑板：木框 + 墨绿板面 + 支架，粉笔字一笔一笔出现，写满了擦掉重来。
        k：0→1 表示从虚空里被拉出来的进度（会缩放、移动、淡入）；write：要不要写字；full：直接显示写满的样子。"""
        board = QRectF(5, ground - 76, 46, 34)
        p.save()
        # 左边窗口外就是虚空：黑板整块从那里被拽出来 / 被踹回去，大小不变
        if k < 1.0:
            p.translate(-(1 - k) * 62, 0)
        if fly > 0:
            f = 1 - (1 - fly) ** 2                       # 被踹：一开始最快
            p.translate(-f * 62, 0)
        # 支架
        p.setPen(QPen(QColor("#8b5a2b"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(board.left() + 8, board.bottom()), QPointF(board.left() + 3, ground))
        p.drawLine(QPointF(board.right() - 8, board.bottom()), QPointF(board.right() - 3, ground))
        # 木框 + 板面
        p.setPen(QPen(QColor("#8b5a2b"), 3))
        p.setBrush(QColor("#2f5d4a"))
        p.drawRoundedRect(board, 3, 3)
        # 粉笔字：按时间一笔一笔出现，约 8 秒写满一板
        strokes = [
            [(9, 7), (13, 4), (17, 8), (21, 4), (25, 8)],          # 波浪字
            [(28, 6), (36, 6)],
            [(9, 14), (19, 14)], [(22, 14), (34, 14)],              # 两行"字"
            [(9, 20), (15, 20)], [(18, 20), (30, 20)],
            [(9, 28), (14, 24), (19, 26), (24, 21), (29, 23)],     # 小折线图
            [(31, 22), (34, 21), (33, 24)],                          # 箭头
            [(36, 14), (38, 11), (40, 14), (38, 17), (36, 14)],    # 小圈
        ]
        cycle = 9.0
        if full:
            prog = len(strokes)
        elif not write:
            prog = 0
        else:
            tt = max(0.0, time.time() - getattr(self, "teach_since", 0) - 1.3) * 1.8   # 掏出黑板后才开始写
            prog = tt % cycle / (cycle - 1.0) * len(strokes)      # 最后一秒写满停一下
        p.setPen(QPen(QColor(255, 255, 240, 220), 1.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.setBrush(Qt.BrushStyle.NoBrush)
        for i, st in enumerate(strokes):
            if i >= prog:
                break
            frac = min(1.0, prog - i)
            pts = [QPointF(board.left() + x, board.top() + y) for x, y in st]
            n = max(1, int((len(pts) - 1) * frac + 0.999))
            for a, b in zip(pts[:n], pts[1:n + 1]):
                p.drawLine(a, b)
        # 粉笔槽
        p.setPen(QPen(QColor("#8b5a2b"), 2))
        p.drawLine(QPointF(board.left() + 2, board.bottom() + 2), QPointF(board.right() - 2, board.bottom() + 2))
        p.restore()

    def paintEvent(self, _):
        """画一个小火柴人。待命：轻轻晃；抓新闻：原地小跑；有新闻：举手挥舞；深入浅出：在小黑板上写写画画。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = QColor("#1f2430")
        t = self.phase
        cx, ground = self.W / 2, self.H - 12

        if self.fetching:
            mode, speed = "fish", 1.0
        elif self.explaining:
            el = time.time() - getattr(self, "teach_since", 0)
            if el < 1.3:                       # 从虚空里掏黑板
                mode, speed = "pull", 1.0
                kk = el / 1.3
                kb = max(0.0, min(1.0, (kk - 0.15) / 0.85))
                kb = 1 - (1 - kb) ** 3          # 缓出：先快后慢
                cx = self.W * (0.5 + 0.19 * min(1.0, kk / 0.3))   # 先往右让一步
            else:
                mode, speed = "teach", 1.0
                cx = self.W * 0.69          # 人往右站，左边放小黑板
        elif time.time() - getattr(self, "teach_end", 0) < 1.1:   # 写完：一脚把黑板踹飞
            mode, speed = "stow", 1.0
            a = (time.time() - self.teach_end) / 1.1        # 0→1
            # 腿：0~0.3 往后蓄力，0.3~0.42 猛踢，0.42~0.6 停住，之后收回
            if a < 0.3:
                kick = 11 + 30 * (a / 0.3)
            elif a < 0.42:
                kick = 41 - 128 * ((a - 0.3) / 0.12)
            elif a < 0.6:
                kick = -87
            else:
                kick = -87 + 76 * ((a - 0.6) / 0.4)
            fly = min(1.0, max(0.0, (a - 0.38) / 0.35))     # 黑板被踢中后飞进虚空的进度
            back = max(0.0, (a - 0.6) / 0.4)
            cx = self.W * (0.69 - 0.19 * back)
        elif time.time() - getattr(self, "fish_end", 0) < self.CATCH_LEN:   # 抓完：把报纸摔飞
            mode, speed = "catch", 1.0
        elif self.queue:
            mode, speed = "wave", 2.2
        else:
            mode, speed = "idle", 1.0

        if mode in ("fish", "catch"):
            head_c = self.draw_fetch(p, t, ground, mode, ink)
        else:
            # 身体上下起伏
            if mode == "run":
                bob = -abs(math.sin(t * speed)) * 5
            elif mode == "wave":
                bob = -abs(math.sin(t * 1.6)) * 7   # 小跳
            else:
                bob = math.sin(t) * 1.5             # 讲解时也和平时一样轻轻起伏，不会像换了个人

            if mode == "teach":
                self.draw_blackboard(p, t, ground)
            elif mode == "pull":
                if kb > 0:
                    self.draw_blackboard(p, t, ground, k=kb, write=False)
            elif mode == "stow":
                if fly < 1.0:
                    self.draw_blackboard(p, t, ground, full=True, fly=fly)
                if 0.38 <= a < 0.55:                               # 踢中的一瞬间：砰！
                    self.draw_bang(p, QPointF(46, ground - 30), (a - 0.38) / 0.17)

            hip = QPointF(cx, ground - 34 + bob)
            neck = QPointF(cx, hip.y() - 30)
            head_c = QPointF(cx, neck.y() - 14)

            # 影子（跳起来时变小）
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 35))
            sw = 34 + bob
            p.drawEllipse(QRectF(cx - sw / 2, ground - 3, sw, 7))

            pen = QPen(ink, 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)

            def limb(origin, length, angle_deg):
                a = math.radians(angle_deg)
                return QPointF(origin.x() + length * math.sin(a), origin.y() + length * math.cos(a))

            # 腿
            if mode == "run":
                swing = math.sin(t * speed) * 35
            elif mode == "wave":
                swing = 8
            else:
                swing = 12 + math.sin(t) * 2
            for sgn in (1, -1):
                ang = swing * sgn
                if mode == "stow" and sgn == -1:
                    ang = kick + (-swing + 11) * back           # 踢腿的那条腿，收回时回到平时站姿
                knee = limb(hip, 17, ang)
                foot = limb(knee, 17, ang - (abs(ang) * 0.6 if mode == "run" else 0))
                p.drawLine(hip, knee)
                p.drawLine(knee, foot)

            # 身体
            p.drawLine(neck, hip)

            # 手臂
            shoulder = QPointF(cx, neck.y() + 5)
            if mode == "run":
                a1 = math.sin(t * speed) * 50          # 两只手前后反着摆
                a2 = -a1
                arms = [(a1, a1 + 70), (a2, a2 + 70)]
            elif mode == "wave":
                wave = math.sin(t * 6) * 25
                arms = [(112 + wave * 0.2, 165 + wave), (-20, -10)]   # 右手高举挥动，左手放下
            elif mode == "pull":
                # 左手伸向旁边的虚空，把黑板拽出来
                arms = [(-100 + 15 * (1 - kb), -95 + 10 * math.sin(t * 8) * (1 - kb)), (35, -40)]
            elif mode == "stow":
                # 踢腿时两手张开保持平衡，收腿后慢慢放下
                spread = 1 - back
                arms = [(-25 - 70 * spread, -15 - 90 * spread), (25 + 80 * spread, 15 + 100 * spread)]
            elif mode == "teach":
                # 左手拿粉笔在黑板上写：小幅来回 + 上下移动；右手叉腰
                wx = math.sin(t * 7) * 9 + math.sin(t * 1.3) * 6
                wy = math.sin(t * 4.3) * 10
                arms = [(-112 + wy, -75 + wx), (35, -40)]
            else:
                arms = None
            sway = math.sin(t) * 6
            idle_r, idle_l = (25 + sway, 15 + sway), (-25 - sway, -15 - sway)
            if arms is None:
                arms = [idle_r, idle_l]
            else:
                # wb：动作的力度。掏黑板开始时从平时垂手的姿势慢慢抬起来；踹完收腿时慢慢放回平时的样子
                if mode == "pull":
                    wb = min(1.0, kk / 0.25)
                    wb = 1 - (1 - wb) ** 2
                elif mode == "stow":
                    wb = 1 - back
                    if a < 0.12:                       # 刚从写字切到踢：手先从写字的姿势张开
                        wx = math.sin(t * 7) * 9 + math.sin(t * 1.3) * 6
                        wy = math.sin(t * 4.3) * 10
                        f0 = a / 0.12
                        tw = [(-112 + wy, -75 + wx), (35, -40)]
                        arms = [(tw[i][0] + (arms[i][0] - tw[i][0]) * f0, tw[i][1] + (arms[i][1] - tw[i][1]) * f0)
                                for i in range(2)]
                else:
                    wb = 1.0
                mixed = []
                for upper, lower in arms:
                    bu, bl = idle_r if upper >= 0 else idle_l
                    mixed.append((bu + (upper - bu) * wb, bl + (lower - bl) * wb))
                arms = mixed
            for k, (upper, lower) in enumerate(arms):
                elbow = limb(shoulder, 14, upper)
                hand = limb(elbow, 14, lower)
                p.drawLine(shoulder, elbow)
                p.drawLine(elbow, hand)
                if mode == "teach" and k == 0:
                    # 手里的小粉笔
                    p.setPen(QPen(QColor("#fff7d6"), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
                    p.drawLine(hand, QPointF(hand.x() - 4, hand.y() - 2))
                    p.setPen(pen)

            # 头
            p.setBrush(QColor("white"))
            p.drawEllipse(head_c, 12, 12)
            # 眼睛（会眨）
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(ink)
            look = -2.5 if mode in ("teach", "pull", "stow") else 0      # 看向黑板
            if mode == "pull":
                look *= min(1.0, kk / 0.25)
            elif mode == "stow":
                look *= 1 - back
            for ex in (head_c.x() - 4.5 + look, head_c.x() + 4.5 + look):
                if self.blink:
                    p.drawRect(QRectF(ex - 2.5, head_c.y() - 1.5, 5, 1.6))
                else:
                    p.drawEllipse(QPointF(ex, head_c.y() - 1.5), 1.8, 2.4)
            # 嘴
            p.setPen(QPen(ink, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            if mode == "wave" or (mode == "pull" and kk > 0.15) or (mode == "stow" and 0.3 <= a < 0.7):
                p.setBrush(ink)
                p.drawEllipse(QPointF(head_c.x() + (look if mode == "pull" else 0), head_c.y() + 5), 2.2, 2.6)
            else:
                p.drawArc(QRectF(head_c.x() - 4, head_c.y() + 1, 8, 6), 200 * 16, 140 * 16)

        # 头顶的状态小灯
        p.setPen(Qt.PenStyle.NoPen)
        if self.fetching or self.explaining:
            glow = QColor(255, 190, 40, 150 + int(100 * abs(math.sin(t * 3))))
        elif self.queue:
            glow = QColor("#ff5a5f")
        elif time.time() < self.flash_until:
            glow = QColor(self.flash_color)
            glow.setAlpha(110 + int(145 * abs(math.sin(t * 2))))   # 呼吸
        else:
            glow = QColor("#4cd17a")
        p.setBrush(glow)
        p.drawEllipse(QPointF(head_c.x() - 16, head_c.y() - 14), 4.5, 4.5)

        # 有新闻：红色数字角标
        if self.queue:
            r = QRectF(head_c.x() + 11, max(head_c.y() - 22, 1), 22, 22)
            p.setBrush(QColor("#ff5a5f"))
            p.drawEllipse(r)
            p.setPen(QColor("white"))
            f = QFont()
            f.setBold(True)
            f.setPointSize(11)
            p.setFont(f)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(min(len(self.queue), 9)))
        p.end()


def dot_icon(color):
    """右键菜单里用的彩色小圆点图标。"""
    from PyQt6.QtGui import QIcon, QPixmap
    pm = QPixmap(16, 16)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(3, 3, 10, 10))
    p.end()
    return QIcon(pm)


def hide_dock_icon():
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass


def main():
    cfg = load_config()
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    hide_dock_icon()
    migrate_old_explains()
    cleanup(cfg)
    pet = Pet(cfg)
    pet.show()
    if not get_api_key():
        QTimer.singleShot(800, pet.ask_key)
    log("火柴人启动了，数据保存在 {}".format(DATA_DIR))
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
