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
from PyQt6.QtGui import QAction, QBrush, QColor, QCursor, QFont, QGuiApplication, QPainter, QPen
from PyQt6.QtWidgets import (
    QApplication, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMenu, QPushButton, QTextBrowser, QVBoxLayout, QWidget,
)

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path.home() / ".ai_news_pet"
DATA_DIR.mkdir(exist_ok=True)
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


def fetch_one(src):
    try:
        items = fetch_page(src) if src.get("type") == "page" else fetch_rss(src)
        log("{}：拿到 {} 条".format(src["name"], len(items)))
        return items
    except Exception as ex:
        log("{}：抓取失败（{}）".format(src["name"], ex))
        return []


def fetch_all(sources):
    sources = [s for s in sources if s.get("enabled", True)]
    out = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for items in pool.map(fetch_one, sources):
            out.extend(items)
    return out


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


def summarize(cfg, cands, key):
    from openai import OpenAI

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


def send_feishu(news, title=None):
    """通过飞书群的自定义机器人发一条富文本消息。成功返回 True。"""
    c = feishu_conf()
    if not c:
        return False
    lines = []
    for i, n in enumerate(news, 1):
        label = n["title"] if len(news) == 1 else "{}. {}".format(i, n["title"])
        lines.append([{"tag": "a", "text": label, "href": n["link"]}])
        desc = n.get("summary") or ""
        lines.append([{"tag": "text", "text": "{}（{}）".format(desc, n["source"]) if desc else "（{}）".format(n["source"])}])
        lines.append([{"tag": "text", "text": " "}])
    body = {"msg_type": "post", "content": {"post": {"zh_cn": {
        "title": title or "🤖 AI 新闻速递 · {} 条".format(len(news)), "content": lines}}}}
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
        log("飞书推送{}：{}".format("成功" if ok else "失败", "" if ok else data))
        return ok
    except Exception as ex:
        log("飞书推送失败：{}".format(ex))
        return False


# ------------------------------------------------------------------ 深入浅出
def load_explain_method():
    """读取项目文件夹里的「深入浅出.md」（写作方法，可以自己改）。"""
    try:
        return (APP_DIR / "深入浅出.md").read_text(encoding="utf-8")
    except Exception:
        return "请用深入浅出的方式，写一篇 2000 字左右的中文文章，把这条新闻讲清楚。不要编造数据。"


def fetch_article_text(url, limit=8000):
    """抓原文正文（尽力而为，失败就返回空）。"""
    try:
        r = requests.get(url, headers=UA, timeout=20)
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


def explain_news(cfg, item, key):
    """返回 (文章 markdown, 这次花费)。讲过的直接读缓存，不重复花钱。"""
    path = explain_cache_path(item)
    if path.exists():
        return path.read_text(encoding="utf-8"), 0.0
    from openai import OpenAI
    article = fetch_article_text(item["link"])
    user = ("请把这条新闻给我讲清楚。\n\n标题：{}\n来源：{}\n摘要：{}\n链接：{}\n\n原文节选（可能不完整）：\n{}"
            .format(item["title"], item["source"], item.get("summary", ""), item["link"],
                    article or "（没能抓到原文，请只基于标题和摘要讲你能确定的部分）"))
    client = OpenAI(api_key=key, base_url=cfg["base_url"], timeout=300)
    resp = client.chat.completions.create(
        model=cfg["model"],
        messages=[{"role": "system", "content": load_explain_method()},
                  {"role": "user", "content": user}],
    )
    cost = 0.0
    try:
        cost = record_usage(cfg, getattr(resp, "usage", None))
    except Exception as ex:
        log("记录用量失败：{}".format(ex))
    text = (resp.choices[0].message.content or "").strip()
    if text:
        text += "\n\n---\n\n*来源：{} · [原文链接]({}) · 讲解生成于 {}*\n".format(
            item["source"], item["link"], datetime.now().strftime("%Y-%m-%d %H:%M"))
        EXPLAIN_DIR.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        remember_explain(item, path)
    return text, cost


class Explainer(QObject):
    done = pyqtSignal(object, str, str)   # item, 文章, 错误信息

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
            text, cost = explain_news(self.cfg, item, key)
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

    def push_current_to_feishu(self):
        """把当前这条新闻手动推到飞书（不占每天的自动名额）。"""
        if self.items:
            record_pref(self.pet.cfg, self.items[self.idx], "feishu")
        if not feishu_conf():
            self.flash_header("⚠️ 还没设置飞书推送（右键小人 → 设置飞书推送）")
            return
        item = dict(self.items[self.idx])
        self.flash_header("⏳ 正在推送到飞书…", keep=True)
        self.push_result = None

        def run():
            self.push_result = send_feishu([item], title="📌 AI 新闻推荐")
        threading.Thread(target=run, daemon=True).start()

        def check(tries=[0]):
            if self.push_result is None and tries[0] < 40:
                tries[0] += 1
                QTimer.singleShot(250, check)
                return
            self.flash_header("✅ 已推送到飞书" if self.push_result else "❌ 推送失败，看看运行日志")
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
        self.history = only_today(load_json(HISTORY_FILE, []))
        self.press_pos = None
        self.moved = False

        self.bubble = Bubble(self)
        self.worker = Worker(cfg)
        self.worker.done.connect(self.on_done)
        self.explaining = None      # 正在讲解的新闻标题
        self.readers = []
        self.explainer = Explainer(cfg)
        self.explainer.done.connect(self.on_explained)

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
        if not self.feishu_pending or self.feishu_busy or not feishu_conf() or self.in_quiet_hours():
            return
        limit = int(self.cfg.get("feishu_daily_limit", 1))
        left = limit - feishu_sent_today()
        if left <= 0:
            self.feishu_pending = []   # 今天的名额用完了，其余只在桌面看
            return
        items, self.feishu_pending = self.feishu_pending[:left], []
        self.feishu_busy = True

        def run():
            if send_feishu(items, title="🤖 今日 AI 新闻"):
                add_feishu_sent(len(items))
            else:
                self.feishu_pending = items + self.feishu_pending  # 失败了下次再试
            self.feishu_busy = False
        threading.Thread(target=run, daemon=True).start()

    def think(self):
        cleanup(self.cfg)          # 每周一才会真正执行
        self.history = only_today(self.history)
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
        log("开始抓新闻")
        self.worker.start()

    def on_done(self, news, msg):
        self.fetching = False
        self.last_fetch = time.time()
        if news:
            self.queue.extend(news)
            self.feishu_pending.extend(news)
            self.flush_feishu()
            if user_idle_seconds() < 60 and not self.is_quiet():
                self.push_queue()
        elif msg:
            # 不弹卡片，用头顶的灯表示结果：蓝色=没有新新闻，紫色=出问题了
            bad = ("出错" in msg) or ("没抓到" in msg)
            self.flash_color = QColor("#a78bfa") if bad else QColor("#5aa9ff")
            self.flash_until = time.time() + 8
            self.last_msg = msg
        self.manual = False
        self.update_tip()

    def push_queue(self):
        items, self.queue = self.queue, []
        self.history = only_today(items + self.history)[:50]
        save_json(HISTORY_FILE, self.history)
        self.bubble.show_items(items, "新的 AI 新闻")
        self.update_tip()

    def update_tip(self):
        if self.queue:
            self.setToolTip("有 {} 条新闻，点我看".format(len(self.queue)))
        else:
            tip = "点我看今天的新闻，右键有更多选项"
            if getattr(self, "explaining", None):
                tip = "正在深入浅出：{}\n{}".format(self.explaining, tip)
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
        elif self.history:
            self.bubble.show_items(self.history[:30], "今天的新闻")
        else:
            self.bubble.show_message("今天还没有新闻～右键选「立刻抓一次」试试")

    def contextMenuEvent(self, e):
        m = QMenu(self)
        a1 = QAction("立刻抓一次新闻", self)
        a1.triggered.connect(lambda: self.fetch_now(manual=True))
        a2 = QAction("看今天的新闻", self)
        a2.triggered.connect(lambda: self.bubble.show_items(self.history[:30], "今天的新闻")
                             if self.history else self.bubble.show_message("今天还没有新闻"))
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
        a5 = QAction("打开配置文件夹", self)
        a5.triggered.connect(lambda: subprocess.Popen(["open", str(APP_DIR)]))
        ar = QAction("重启火柴人", self)
        ar.triggered.connect(self.restart)
        a6 = QAction("退出", self)
        a6.triggered.connect(QApplication.quit)
        for a in (a1, a2, a3, au, a4, af, at, ae, a5):
            m.addAction(a)
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
                            ("#ffbe28", "黄灯闪烁：正在抓新闻 / 正在写深入浅出"),
                            ("#5aa9ff", "蓝灯呼吸：抓完了，没有新新闻"),
                            ("#a78bfa", "紫灯呼吸：抓的时候出了问题（鼠标停在小人上看原因）"),
                            ("#ff5a5f", "红灯 + 数字：有新闻没看")):
            lights.addAction(QAction(dot_icon(color), text, self))
        m.addSeparator()
        m.addAction(ar)
        m.addAction(a6)
        m.exec(e.globalPos())

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

    def on_explained(self, item, text, err):
        self.explaining = None
        self.update_tip()
        if err:
            self.flash_color = QColor("#a78bfa")
            self.flash_until = time.time() + 8
            self.last_msg = "深入浅出失败：" + err
            self.update_tip()
            return
        w = ReaderWindow(item, text)
        self.readers = [r for r in self.readers if r.isVisible()] + [w]
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
        self.phase += 0.06
        if self.blink > 0:
            self.blink -= 1
        elif random.random() < 0.008:
            self.blink = 5
        self.update()

    def paintEvent(self, _):
        """画一个小火柴人。待命：轻轻晃；抓新闻：原地小跑；有新闻：举手挥舞。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink = QColor("#1f2430")
        t = self.phase
        cx, ground = self.W / 2, self.H - 12

        if self.fetching or self.explaining:
            mode, speed = "run", 3.2
        elif self.queue:
            mode, speed = "wave", 2.2
        else:
            mode, speed = "idle", 1.0

        # 身体上下起伏
        if mode == "run":
            bob = -abs(math.sin(t * speed)) * 5
        elif mode == "wave":
            bob = -abs(math.sin(t * 1.6)) * 7   # 小跳
        else:
            bob = math.sin(t) * 1.5

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
        else:
            sway = math.sin(t) * 6
            arms = [(25 + sway, 15 + sway), (-25 - sway, -15 - sway)]
        for upper, lower in arms:
            elbow = limb(shoulder, 14, upper)
            hand = limb(elbow, 14, lower)
            p.drawLine(shoulder, elbow)
            p.drawLine(elbow, hand)

        # 头
        p.setBrush(QColor("white"))
        p.drawEllipse(head_c, 12, 12)
        # 眼睛（会眨）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        for ex in (head_c.x() - 4.5, head_c.x() + 4.5):
            if self.blink:
                p.drawRect(QRectF(ex - 2.5, head_c.y() - 1.5, 5, 1.6))
            else:
                p.drawEllipse(QPointF(ex, head_c.y() - 1.5), 1.8, 2.4)
        # 嘴
        p.setPen(QPen(ink, 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.setBrush(Qt.BrushStyle.NoBrush)
        if mode == "wave":
            p.setBrush(ink)
            p.drawEllipse(QPointF(head_c.x(), head_c.y() + 5), 2.2, 2.6)
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
