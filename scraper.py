#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
货车行情抓取脚本
============================================================
由 GitHub Actions 定时运行（也支持本地手动运行）。
职责：从配置的公开行情源抓取最新报价，提取价格相关文本，
      写入 market-data.json 的 signals 字段（最新市场报价摘录）。

设计说明：
- 网页结构多变，通用自动解析"车型->价格"不可靠，容易把新车价/指导价
  误当成二手车成交价。因此 data（结构化定价基准）以人工校准为准，
  脚本只负责更新 signals（供决策参考的最新市场原文报价）。
- 每个源可配置 CSS/正则提取规则；抓取失败的源自动跳过，保留旧数据，
  不影响整体。若某源连续失败，会在日志中提示。

本地运行：  python3 scraper.py
依赖：      仅标准库（urllib / re / json / datetime），无需 pip install
"""

import json
import re
import ssl
import urllib.request
from datetime import datetime, timezone, timedelta

DATA_FILE = "market-data.json"
MAX_SIGNALS_PER_SOURCE = 3          # 每个源最多保留几条摘录
MAX_TOTAL_SIGNALS = 40              # 最多保留多少条
TIMEOUT = 20

# ------------------------------------------------------------
# 行情源配置：按需增删。text_re 用于在正文里定位含价格的句子。
# ------------------------------------------------------------
SOURCES = [
    {
        "name": "天天拍车·解放J6二手价",
        "url": "https://m.ttpai.cn/zixun/zhishi-900927",
        "keys": ["二手", "成交", "万"],
    },
    {
        "name": "卡车之家·经销商报价",
        "url": "https://dealerm.360che.com/52375/news_7635600.html",
        "keys": ["万", "纯电动", "载货车"],
    },
    {
        "name": "四川/成都新能源二手行情",
        "url": "https://www.sinoasphalt.com/shangxun/Article-qUHuM2-923281.html",
        "keys": ["二手", "万", "kWh"],
    },
    {
        "name": "新能源二手货车行情",
        "url": "http://www.156cv.com/product/83006.html",
        "keys": ["二手", "残值", "万"],
    },
    {
        "name": "二手货车估价行情",
        "url": "https://www.ttpai.cn/zixun/by-930303",
        "keys": ["二手", "折旧", "残值"],
    },
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def fetch(url):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
        raw = r.read()
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def html_to_text(html):
    # 去脚本/样式
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    for a, b in [("&nbsp;", " "), ("&amp;", "&"), ("&gt;", ">"), ("&lt;", "<"),
                 ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text):
    parts = re.split(r"(?<=[。！？；])|\s{2,}", text)
    return [p.strip() for p in parts if p.strip()]


def extract_price_sentences(text, keys):
    """提取既包含价格数字、又命中关键词的句子；过滤明显新车广告"""
    out = []
    price_re = re.compile(r"\d+(?:\.\d+)?\s*(?:万元|万|元/月|元每月|元/天)")
    # 噪声：纯新车促销、询价引导、联系方式
    noise_re = re.compile(r"询底价|获取最低价|促销|优惠|立减|首付|月供|电话|地址|抢购|指导价|厂商|促销价")
    for sent in split_sentences(text):
        if not (8 <= len(sent) <= 160):
            continue
        if not price_re.search(sent):
            continue
        if keys and not any(k in sent for k in keys):
            continue
        if noise_re.search(sent):
            continue
        out.append(sent)
    return out


def now_cn():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M")


def main():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        payload = json.load(f)

    old_signals = payload.get("signals", [])
    # 保留手动添加（无 scraped 标记）的条目
    kept = [s for s in old_signals if not s.get("scraped")]
    fresh = []
    stamp = now_cn()

    for src in SOURCES:
        try:
            html = fetch(src["url"])
            text = html_to_text(html)
            sentences = extract_price_sentences(text, src.get("keys"))
            if not sentences:
                print(f"[跳过] {src['name']}: 未提取到价格句子")
                continue
            # 优先取信息量适中的句子
            sentences = sorted(sentences, key=lambda s: abs(len(s) - 55))[:MAX_SIGNALS_PER_SOURCE]
            for s in sentences:
                fresh.append({
                    "date": stamp[:7],
                    "src": src["name"],
                    "url": src["url"],
                    "text": s,
                    "scraped": True,
                })
            print(f"[成功] {src['name']}: {len(sentences)} 条")
        except Exception as e:
            print(f"[失败] {src['name']}: {type(e).__name__}: {e}")

    # 合并：抓取结果在前，保留的旧条目在后，去重并截断
    seen = set()
    merged = []
    for s in fresh + kept:
        key = (s.get("src", ""), s.get("text", ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(s)
    payload["signals"] = merged[:MAX_TOTAL_SIGNALS]

    payload["updatedAt"] = stamp
    payload["generatedBy"] = "GitHub Actions / scraper.py"
    # data 保持不变（人工校准的结构化定价基准）

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n完成：更新于 {stamp}，signals 共 {len(payload['signals'])} 条，"
          f"data {len(payload.get('data', []))} 条（保持不变）")


if __name__ == "__main__":
    main()
