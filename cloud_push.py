#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
火柴人 · 云端每日推送（在 GitHub Actions 上跑，电脑关机也能推）

每天定时：抓新闻 → DeepSeek 挑选和写摘要 → 写深入浅出精简版 → 推 1 条到飞书。
抓新闻、挑新闻、写讲解、推飞书都直接用 pet.py 里的同一套代码，和桌面火柴人效果一致。

需要的 GitHub Secrets（仓库 Settings → Secrets and variables → Actions）：
  DEEPSEEK_API_KEY   DeepSeek 的 Key
  FEISHU_WEBHOOK     飞书群机器人的 webhook 地址
  FEISHU_SECRET      飞书机器人的签名密钥（没开签名校验就不用填）
"""

import os
import sys
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("AI_NEWS_PET_DATA", str(HERE / "state"))


def _stub_pyqt():
    """云端没有屏幕，也用不到界面：给 PyQt6 放一个空壳，这样能直接 import pet.py 里的功能。"""
    class _Meta(type):
        def __getattr__(cls, name):
            return _Dummy

    class _Dummy(metaclass=_Meta):
        def __init__(self, *a, **k):
            pass

        def __call__(self, *a, **k):
            return _Dummy()

        def __getattr__(self, name):
            return _Dummy()

    for name in ("PyQt6", "PyQt6.QtCore", "PyQt6.QtGui", "PyQt6.QtWidgets"):
        m = types.ModuleType(name)
        m.__getattr__ = lambda n: _Dummy
        sys.modules[name] = m


_stub_pyqt()
sys.path.insert(0, str(HERE))
import pet  # noqa: E402


def main():
    cfg = pet.load_config()
    key = pet.get_api_key()
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not key or not webhook:
        pet.log("缺少 DEEPSEEK_API_KEY 或 FEISHU_WEBHOOK，请在 GitHub Secrets 里设置")
        return 1
    # webhook 只写在临时文件夹里，不进「记录」缓存
    import tempfile
    pet.FEISHU_FILE = Path(tempfile.mkdtemp()) / "feishu.json"
    pet.save_json(pet.FEISHU_FILE, {"webhook": webhook, "secret": os.environ.get("FEISHU_SECRET", "").strip()})

    limit = int(cfg.get("cloud_daily_limit", 1))
    left = limit - pet.feishu_sent_today()
    if left <= 0:
        pet.log("今天已经推过 {} 条了，不再推".format(limit))
        return 0

    # 1) 抓新闻
    items = pet.fetch_all(cfg["sources"])
    if not items:
        pet.log("一条都没抓到，可能是网络问题")
        return 1
    seen_list = pet.load_json(pet.SEEN_FILE, None)
    first_run = seen_list is None
    seen_list = seen_list or []
    seen = set(seen_list)
    new, ids = [], set()
    for x in items:
        if x["id"] not in seen and x["id"] not in ids:
            ids.add(x["id"])
            new.append(x)

    # 2) 只看最近 24 小时发布的（每天跑一次，正好覆盖一天）；网页源没有发布时间，第一次运行时当旧闻跳过
    since = time.time() - 24 * 3600
    cands = [x for x in new if (x["ts"] and x["ts"] >= since) or (x["ts"] is None and not first_run)]
    cands.sort(key=lambda x: x["ts"] or time.time(), reverse=True)
    cands = cands[:40]
    pet.log("云端：抓到 {} 条，新的 {} 条，候选 {} 条".format(len(items), len(new), len(cands)))

    def mark_seen():
        seen_list.extend(x["id"] for x in new)
        pet.save_json(pet.SEEN_FILE, seen_list[-5000:])

    if not cands:
        mark_seen()
        pet.log("最近 24 小时没有新新闻")
        return 0

    # 3) DeepSeek 挑选、排序、写摘要（失败就不记「看过」，下次重试）
    news = pet.summarize(cfg, cands, key)
    if not news:
        mark_seen()
        pet.log("有新内容，但都不太值得推")
        return 0

    # 4) 按排序挑前几条「飞书里能打开」的
    picks = []
    for n in news:
        if len(picks) >= left:
            break
        ok, why = pet.link_ok_for_feishu(cfg, n.get("link", ""))
        if ok:
            picks.append(n)
        else:
            pet.log("飞书跳过：{}（{}）".format(n.get("title", ""), why))
    if not picks:
        mark_seen()
        pet.log("挑出的新闻在飞书里都打不开，今天不推")
        return 0

    # 5) 写精简版讲解并推送
    explains = pet.explains_for_feishu(cfg, picks)
    if not pet.send_feishu(picks, title="🤖 今日 AI 新闻", explains=explains):
        pet.log("推送飞书失败")
        return 1
    pet.add_feishu_sent(len(picks))
    mark_seen()
    pet.log("已推送到飞书：" + "；".join(n["title"] for n in picks))
    today = pet.usage_summary()[0]
    pet.log("今天云端花费约 ¥{:.4f}".format(today["cost"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
