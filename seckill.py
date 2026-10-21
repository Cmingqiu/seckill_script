#!/usr/bin/env python
# -*- coding: utf-8 -*-
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "playwright>=1.45",
#   "requests>=2.31",
# ]
# ///
"""腾讯云「上云精选·限时秒杀」自动抢购脚本。

设计要点
--------
1. 登录态：用 Playwright 的 persistent context（独立 user-data-dir），第一次
   `python seckill.py --login` 手动登录一次，之后长期复用，不必每次登录。
2. 定时：按 config.toml 里的 fire_times（默认 10:00 / 15:00）常驻等待；
   HTTP Date 头校准本地时钟，误差只影响「何时进入高频轮询」，不影响命中。
3. 定位：目标页面 SSR 里的「立即抢购」全部来自顶部导航，秒杀卡片按钮是
   客户端渲染的，所以按「卡片锚点文本 + 按钮文案」在运行时定位，不写死 class。
4. 点击：用 CDP 的鼠标坐标点击（page.mouse.click），比 locator.click 少一轮
   actionability 检查；坐标来自 getBoundingClientRect，视口坐标一一对应。
5. 兜底：页面文案被框架重建导致标记丢失时，自动重新扫描；点击后判定是否
   跳转到购买页 / 变成「已抢光」，未成功则重试。

脚本只负责「点进购买流程」，不做也不应该做自动支付。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests

try:  # winsound 只在 Windows 有
    import winsound
except ImportError:  # pragma: no cover
    winsound = None  # type: ignore[assignment]

from playwright.sync_api import (  # noqa: E402
    Browser,
    BrowserContext,
    Error as PWError,
    Page,
    Playwright,
    sync_playwright,
)

BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("seckill")

# 实测：秒杀卡片的按钮是 div.uno3-buy-card__btn，卡片容器是 .uno3-buy-card
BTN_SELECTOR = ".uno3-buy-card__btn"


# --------------------------------------------------------------------------- #
# 页面内执行的 JS
# --------------------------------------------------------------------------- #

# 定位：先在页面里找出「商品卡片」容器（实测秒杀区是 .uno3-buy-card），再用锚点
# 文本校验是不是目标那张卡，最后在卡片内部找按钮。
#
# 为什么不按按钮文案反查卡片：目标卡片处于「已抢光 / 添加提醒」时按钮文案根本不在
# buy_texts 里，反查会退化成「全页第一个『立即抢购』」——那往往是别的商品（实测会
# 点到「优选流量包」「COS」的按钮上）。
JS_SCAN = r"""
(cfg) => {
  const norm = (s) => (s || '').replace(/[\s\u00a0\u3000]+/g, '');
  const wanted = cfg.anchors.map(norm);
  const buy = cfg.buyTexts.map(norm);
  const statusAll = cfg.statusTexts.map(norm);
  const all = buy.concat(statusAll);

  document.querySelectorAll('[data-sk-card],[data-sk-cidx]').forEach((e) => {
    e.removeAttribute('data-sk-card');
    e.removeAttribute('data-sk-cidx');
  });

  const out = {
    url: location.href,
    visibility: document.visibilityState,
    viewport: { w: innerWidth, h: innerHeight },
    scanned: 0,
    cardCount: 0,
    statusSeen: [],
    cards: [],
    others: [],
  };
  const seenStatus = new Set();

  document.querySelectorAll(cfg.cardSelector).forEach((card) => {
    out.scanned += 1;
    const r = card.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    const cst = getComputedStyle(card);
    if (cst.display === 'none' || cst.visibility === 'hidden' || cst.opacity === '0') return;

    const text = norm(card.innerText);
    if (!wanted.every((w) => text.includes(w))) {
      // 没命中锚点的卡片也留几张贴在视口里的，方便 --probe 时人工校准 card_anchors
      if (out.others.length < 8 && r.bottom > 0 && r.y < innerHeight) {
        const btn = card.querySelector(cfg.btnSelector);
        out.others.push({
          cls: String(card.className || '').slice(0, 80),
          rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
          btn: btn ? norm(btn.innerText).slice(0, 16) : null,
          text: (card.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120),
        });
      }
      return;
    }

    let btn = null;
    const tw = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = tw.nextNode())) {
      const t = norm(node.nodeValue);
      if (!t || t.length > 12) continue;

      if (statusAll.includes(t) && !buy.includes(t)) seenStatus.add(t);
      if (!all.includes(t)) continue;

      const el = node.parentElement;
      if (!el || !card.contains(el)) continue;
      const er = el.getBoundingClientRect();
      if (er.width <= 0 || er.height <= 0) continue;
      const est = getComputedStyle(el);
      if (est.display === 'none' || est.visibility === 'hidden' || est.opacity === '0') continue;

      const isBuy = buy.includes(t);
      if (btn && !(isBuy && !btn.isBuy)) continue;  // 只在「升级为可抢购按钮」时才替换

      // 找真正可点的那个元素：整张卡片往往也是 cursor:pointer，所以不能只看
      // cursor；优先认 BUTTON/A 标签和 class 里带 btn 的元素，并用「面积明显小于
      // 卡片」兜住 cursor:pointer 的误判。
      const cardArea = r.width * r.height;
      let click = el;
      for (let i = 0; i < 5 && click && click !== card; i++) {
        const s2 = getComputedStyle(click);
        const c2r = click.getBoundingClientRect();
        const btnLike = click.tagName === 'BUTTON' || click.tagName === 'A'
          || /btn/i.test(String(click.className || ''))
          || (s2.cursor === 'pointer' && c2r.width * c2r.height < cardArea * 0.5);
        if (btnLike) break;
        click = click.parentElement;
      }
      if (click === card) click = el;  // 兜底：上溯到卡片就退回文本元素本身
      const cr = click.getBoundingClientRect();
      const c2 = getComputedStyle(click);
      btn = {
        text: t,
        isBuy: isBuy,
        x: cr.x + cr.width / 2,
        y: cr.y + cr.height / 2,
        w: cr.width,
        h: cr.height,
        tag: click.tagName,
        cls: String(click.className || '').slice(0, 120),
        pointerEvents: c2.pointerEvents,
        disabled: c2.pointerEvents === 'none'
          || click.disabled === true
          || click.getAttribute('aria-disabled') === 'true',
      };
    }

    out.cardCount += 1;
    card.setAttribute('data-sk-cidx', String(out.cardCount));
    const rawBtnEl = card.querySelector(cfg.btnSelector);
    out.cards.push({
      cidx: out.cardCount,
      cls: String(card.className || '').slice(0, 120),
      rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
      inViewport: r.bottom > 0 && r.y < innerHeight && r.x + r.width > 0 && r.x < innerWidth,
      text: (card.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 120),
      rawBtn: rawBtnEl ? norm(rawBtnEl.innerText).slice(0, 20) : null,
      btn: btn,
    });
  });

  out.statusSeen = Array.from(seenStatus);
  return out;
}
"""

# 把选中的候选卡片标记出来，之后高频轮询只在这个容器里找文本。
JS_MARK = r"""
(cidx) => {
  const el = document.querySelector('[data-sk-cidx="' + cidx + '"]');
  if (!el) return false;
  document.querySelectorAll('[data-sk-card]').forEach((e) => e.removeAttribute('data-sk-card'));
  el.setAttribute('data-sk-card', '1');
  return true;
}
"""

# 把目标卡片滚进视口（顶部有吸顶导航，用 center 更稳）
JS_SCROLL = r"""
() => {
  const el = document.querySelector('[data-sk-card="1"]');
  if (!el) return false;
  el.scrollIntoView({ block: 'center', inline: 'center' });
  return true;
}
"""

# 高频轮询：只在卡片容器里找「可抢购」按钮，返回文案 / 中心坐标 / 是否可点 / 是否被遮挡。
JS_TICK = r"""
(cfg) => {
  const norm = (s) => (s || '').replace(/[\s\u00a0\u3000]+/g, '');
  const buy = cfg.buyTexts;
  const statusAll = cfg.statusTexts;
  const scope = document.querySelector('[data-sk-card="1"]');
  if (!scope) return { state: 'lost', status: [] };

  const status = [];
  const tw = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = tw.nextNode())) {
    const t = norm(node.nodeValue);
    if (!t || t.length > 12) continue;

    if (statusAll.includes(t) && !status.includes(t)) status.push(t);
    if (!buy.includes(t)) continue;

    const el = node.parentElement;
    if (!el || !scope.contains(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;

    let click = el;
    const scopeArea = (() => { const sr = scope.getBoundingClientRect(); return sr.width * sr.height; })();
    for (let i = 0; i < 5 && click && click !== scope; i++) {
      const s2 = getComputedStyle(click);
      const c2r = click.getBoundingClientRect();
      const btnLike = click.tagName === 'BUTTON' || click.tagName === 'A'
        || /btn/i.test(String(click.className || ''))
        || (s2.cursor === 'pointer' && c2r.width * c2r.height < scopeArea * 0.5);
      if (btnLike) break;
      click = click.parentElement;
    }
    if (click === scope) click = el;
    const cr = click.getBoundingClientRect();
    const cst = getComputedStyle(click);
    const cx = cr.x + cr.width / 2;
    const cy = cr.y + cr.height / 2;
    if (cy < 0 || cy > innerHeight || cx < 0 || cx > innerWidth) {
      return { state: 'offscreen', text: t, status: status };
    }
    const hitEl = document.elementFromPoint(cx, cy);
    const obstructed = !(hitEl && (hitEl === scope || scope.contains(hitEl)));
    return {
      state: 'ready',
      text: t,
      x: cx,
      y: cy,
      w: cr.width,
      h: cr.height,
      obstructed: obstructed,
      // 盖章物是谁：日志里打出来，下一次就不用再靠猜
      hit: obstructed && hitEl
        ? hitEl.tagName + (hitEl.id ? '#' + hitEl.id : '')
          + (hitEl.className ? '.' + String(hitEl.className).trim().split(/\s+/).slice(0, 2).join('.') : '')
        : null,
      pointerEvents: cst.pointerEvents,
      disabled: cst.pointerEvents === 'none'
        || click.disabled === true
        || click.getAttribute('aria-disabled') === 'true',
      status: status,
    };
  }
  return { state: 'idle', status: status };
}
"""

# 页面内的「抢购哨兵」：开抢前后在目标卡片上每帧检查，一旦出现「立即抢购」，
# 就在页面内当帧 el.click()，并把时间线记进 window.__skWatch。
#
# 为什么必须放进页面：Python 侧发现一次要付两份固定开销 ——
#   tick（CDP Runtime.evaluate 往返，实测 p50 4.2ms）+ 判断 + 坐标点击（move/down/up
#   三次往返，实测 34ms）。而页面内的 MutationObserver / rAF 在文案落进 DOM 的同一
#   微任务/同一帧就能动作。2026-10-20 15:00 场日志里「第 1 次命中 @ +828ms」，
#   脚本侧只占其中约 40ms，剩下是页面自己的状态/渲染延迟；哨兵把能削的那 40ms
#   削到 1 帧以内，同时把「文案切换时刻 / 帧间隔峰值 / 点击时刻」全部记下来，
#   下一场就能用日志回答「页面到底什么时候才真的可点」。
JS_ARM = r"""
(cfg) => {
  const W = window.__skWatch = window.__skWatch || {};
  const t0 = Date.now();
  Object.assign(W, {
    ok: true,
    stop: false,
    armedWall: t0,
    armedPerf: performance.now(),
    fireAtWall: cfg.fireAtWall,
    earliestWall: cfg.earliestWall,
    blindAtWall: cfg.blindAtWall,
    hits: [],
    frames: 0,
    maxGapMs: 0,
    lastPerf: 0,
    texts: [],
    lastText: null,
    mo: null,
    moScope: null,
  });

  const norm = (s) => (s || '').replace(/[\s\u00a0\u3000]+/g, '');
  const buy = cfg.buyTexts;
  const btnSel = cfg.btnSelector;
  const scopeOf = () => document.querySelector('[data-sk-card="1"]');

  // 与 JS_TICK 相同的判定：只在目标卡片里找可抢购按钮，返回可点的那个元素
  const find = () => {
    const scope = scopeOf();
    if (!scope) return { state: 'lost' };
    const sr = scope.getBoundingClientRect();
    const scopeArea = sr.width * sr.height;
    const tw = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    let node;
    while ((node = tw.nextNode())) {
      const t = norm(node.nodeValue);
      if (!t || t.length > 12 || !buy.includes(t)) continue;
      const el = node.parentElement;
      if (!el || !scope.contains(el)) continue;
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') continue;
      let click = el;
      for (let i = 0; i < 5 && click && click !== scope; i++) {
        const s2 = getComputedStyle(click);
        const c2r = click.getBoundingClientRect();
        const btnLike = click.tagName === 'BUTTON' || click.tagName === 'A'
          || /btn/i.test(String(click.className || ''))
          || (s2.cursor === 'pointer' && c2r.width * c2r.height < scopeArea * 0.5);
        if (btnLike) break;
        click = click.parentElement;
      }
      if (click === scope) click = el;
      const cr = click.getBoundingClientRect();
      return {
        state: 'ready',
        el: click,
        text: t,
        x: cr.x + cr.width / 2,
        y: cr.y + cr.height / 2,
        w: cr.width,
        h: cr.height,
      };
    }
    return { state: 'idle' };
  };

  // 记录按钮文案变化（开抢前后各是什么、何时切）
  const noteText = () => {
    const scope = scopeOf();
    if (!scope) return;
    const b = scope.querySelector(btnSel);
    const t = norm(b ? b.textContent : '').slice(0, 16);
    if (t && t !== W.lastText) {
      W.lastText = t;
      W.texts.push({ wall: Date.now(), perf: performance.now(), text: t });
      if (W.texts.length > 40) W.texts.shift();
    }
  };

  // 只点第一次：后续重试交给 Python 侧（需要 _settle 判定结果，不能盲连点）
  const fire = () => {
    if (W.stop || W.hits.length) return;
    // 时间闸门：页面在开抢前可能短暂显示「立即抢购」（实测过的假信号，
    // 秒杀接口数据回来后会变成真实状态）。allow_early_ms 之外一律不点。
    if (Date.now() < W.earliestWall) return;
    const hit = find();
    if (hit.state !== 'ready') return;
    const wall = Date.now();
    const perf = performance.now();
    let err = null;
    try { hit.el.click(); } catch (e) { err = String(e).slice(0, 120); }
    W.hits.push({
      wall: wall, perf: perf, x: hit.x, y: hit.y,
      at: wall - W.fireAtWall, err: err, text: hit.text,
    });
  };

  const frame = () => {
    if (W.stop) return;
    const p = performance.now();
    if (W.lastPerf) {
      const gap = p - W.lastPerf;
      if (gap > W.maxGapMs) W.maxGapMs = gap;
    }
    W.lastPerf = p;
    W.frames += 1;
    noteText();
    fire();
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);

  // MutationObserver 比 rAF 早最多一帧：文案落进 DOM 的微任务里就能点。
  // 它也是窗口被遮挡/最小化时的唯一可靠路径 —— 实测那种情况下 Chrome 会暂停
  // rAF（哨兵帧数停在 0），但 DOM 变更多久该来还是多久来。
  // 卡片被框架重建时标记会丢，每 500ms 重新挂一次。
  const attach = () => {
    const scope = scopeOf();
    if (!scope || !window.MutationObserver) return;
    if (W.mo && W.moScope === scope) return;
    try { if (W.mo) W.mo.disconnect(); } catch (e) {}
    W.mo = new MutationObserver(() => { noteText(); fire(); });
    W.mo.observe(scope, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ['class', 'disabled', 'aria-disabled'],
    });
    W.moScope = scope;
  };
  attach();
  if (W.attachTimer) clearInterval(W.attachTimer);
  W.attachTimer = setInterval(attach, 500);

  return { ok: true, armedWall: W.armedWall, fireAtWall: W.fireAtWall };
}
"""

# 读哨兵状态：只回传白名单字段（DOM 节点不能过 CDP 序列化）
JS_WATCH_STATE = r"""
() => {
  const W = window.__skWatch;
  if (!W || !W.ok) return null;
  return {
    fireAtWall: W.fireAtWall,
    armedWall: W.armedWall,
    nowWall: Date.now(),
    hits: W.hits || [],
    frames: W.frames || 0,
    maxGapMs: W.maxGapMs || 0,
    texts: (W.texts || []).slice(-8),
    lost: !document.querySelector('[data-sk-card="1"]'),
  };
}
"""

# 停掉哨兵：清理 rAF 循环里的 stop 标记、MutationObserver 与重挂定时器
JS_WATCH_STOP = r"""
() => {
  const W = window.__skWatch;
  if (!W) return false;
  W.stop = true;
  try { if (W.attachTimer) clearInterval(W.attachTimer); } catch (e) {}
  try { if (W.mo) W.mo.disconnect(); } catch (e) {}
  W.mo = null;
  W.moScope = null;
  return true;
}
"""

# 兜底点击：直接对按钮元素派发原生 click，绕过浮层遮挡与坐标漂移。
#
# 为什么需要：坐标点击（CDP 鼠标事件）打到的是**最顶层元素**，卡片上只要压着
# 一个浮层（实测：开抢瞬间点一次，页面会弹出提示层，之后 11 秒里 tick 一直报
# obstructed），坐标点击就全打在浮层上，一次都进不了按钮。JS 直达点击不受
# elementFromPoint 影响，是那种情况下唯一的出路。
JS_CLICK_BUY = r"""
(bt) => {
  const norm = (s) => (s || '').replace(/[\s\u00a0\u3000]+/g, '');
  const scope = document.querySelector('[data-sk-card="1"]');
  if (!scope) return { ok: false, why: '卡片容器丢失' };
  const tw = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = tw.nextNode())) {
    const t = norm(node.nodeValue);
    if (!t || t.length > 12 || !bt.includes(t)) continue;
    const el = node.parentElement;
    if (!el || !scope.contains(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    el.click();
    return { ok: true, text: t };
  }
  return { ok: false, why: '卡片内没有可点的购买按钮' };
}
"""

# 抓当前可见的提示/弹层文案。点击被业务拒绝时，原因通常只在这类容器里闪几秒，
# 不抓下来就永远只能看到「按钮仍是立即抢购」这种无信息量的结果。
JS_TIPS = r"""
() => {
  const sel = '[class*="toast"],[class*="message"],[class*="tips"],[class*="dialog"],'
            + '[class*="modal"],[class*="popup"],[class*="alert"],[class*="notify"]';
  const out = [];
  document.querySelectorAll(sel).forEach((e) => {
    const st = getComputedStyle(e);
    if (st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) return;
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return;
    if (r.bottom <= 0 || r.top >= innerHeight) return;
    const t = (e.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t || t.length > 120) return;
    out.push(String(e.className || '').split(/\s+/)[0] + ': ' + t);
  });
  return Array.from(new Set(out)).slice(0, 6);
}
"""

# 页面整体状态：URL、是否出现验证码/异常提示
JS_PAGE_STATE = r"""
() => {
  const txt = (document.body ? document.body.innerText : '') || '';
  return {
    url: location.href,
    visibility: document.visibilityState,
    captcha: /拖动滑块|完成验证|安全验证|请完成验证|行为验证|验证码/.test(txt),
    soldout: /已抢光|已抢完|已售罄|今日已抢光/.test(txt),
  };
}
"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


@dataclass
class Config:
    url: str
    card_selector: str
    card_anchors: list[str]
    buy_texts: list[str]
    ready_texts: list[str]
    soldout_texts: list[str]
    fire_times: list[str]
    preload_seconds: int
    hot_seconds: int
    warm_interval_ms: int
    hot_interval_ms: int
    allow_early_ms: int
    give_up_seconds: int
    click_settle_ms: int
    max_click_attempts: int
    mask_wait_ms: int
    blind_click_ms: int
    channel: str
    headless: bool
    user_data_dir: Path
    window_size: str
    keep_open_after: bool
    keep_open_seconds: int
    chrome_mode: str
    cdp_ports: list[int]
    chrome_user_data_dir: str
    reuse_existing_tab: bool
    log_dir: Path
    log_level: str
    screenshot: bool
    # 运行时填充
    fire_specs: list[tuple[int, int, int]] = field(default_factory=list)

    @staticmethod
    def load(path: Path) -> "Config":
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        page = raw.get("page", {})
        target = raw.get("target", {})
        timing = raw.get("timing", {})
        browser = raw.get("browser", {})
        chrome = raw.get("chrome", {})
        logc = raw.get("log", {})

        cfg = Config(
            url=page["url"],
            card_selector=str(target.get("card_selector", ".uno3-buy-card")),
            card_anchors=list(target.get("card_anchors", [])),
            buy_texts=list(target.get("buy_texts", ["立即抢购"])),
            ready_texts=list(target.get("ready_texts", ["添加提醒"])),
            soldout_texts=list(target.get("soldout_texts", ["已抢光"])),
            fire_times=list(timing.get("fire_times", ["10:00:00", "15:00:00"])),
            preload_seconds=int(timing.get("preload_seconds", 180)),
            hot_seconds=int(timing.get("hot_seconds", 20)),
            warm_interval_ms=int(timing.get("warm_interval_ms", 500)),
            hot_interval_ms=int(timing.get("hot_interval_ms", 30)),
            allow_early_ms=int(timing.get("allow_early_ms", 300)),
            give_up_seconds=int(timing.get("give_up_seconds", 90)),
            click_settle_ms=int(timing.get("click_settle_ms", 250)),
            max_click_attempts=int(timing.get("max_click_attempts", 60)),
            mask_wait_ms=int(timing.get("mask_wait_ms", 1500)),
            blind_click_ms=int(timing.get("blind_click_ms", 100)),
            channel=str(browser.get("channel", "chrome")),
            headless=bool(browser.get("headless", False)),
            user_data_dir=(BASE_DIR / str(browser.get("user_data_dir", "./profile"))).resolve(),
            window_size=str(browser.get("window_size", "1600,1000")),
            keep_open_after=bool(browser.get("keep_open_after", True)),
            keep_open_seconds=int(browser.get("keep_open_seconds", 1800)),
            chrome_mode=str(chrome.get("mode", "launch")).lower(),
            cdp_ports=[int(p) for p in chrome.get("cdp_ports", [9222])],
            chrome_user_data_dir=str(chrome.get("chrome_user_data_dir", "")),
            reuse_existing_tab=bool(chrome.get("reuse_existing_tab", True)),
            log_dir=(BASE_DIR / str(logc.get("dir", "./logs"))).resolve(),
            log_level=str(logc.get("level", "INFO")).upper(),
            screenshot=bool(logc.get("screenshot", True)),
        )
        for t in cfg.fire_times:
            hh, mm, ss = (t.split(":") + ["0", "0"])[:3]
            cfg.fire_specs.append((int(hh), int(mm), int(ss)))
        return cfg


# --------------------------------------------------------------------------- #
# 日常 Chrome 的 CDP 附加
#
# Chrome 136 起，默认 user-data-dir 上 `--remote-debugging-port` 不再生效
# （官方为防止 cookie 被外部程序直接读走而收紧）。官方给的替代入口是
# chrome://inspect/#remote-debugging 里的
# 「Allow remote debugging for this browser instance」开关。
# 打开后 Chrome 会在 user-data-dir 下写 DevToolsActivePort（第一行是端口）。
# 另外实测：Chrome 155 对 <User Data>\Default\Network\Cookies 加了独占锁，
# 任何共享模式下外部都打不开（err=32），所以"复制 cookie 文件"这条路是死的。
# --------------------------------------------------------------------------- #


def default_chrome_user_data() -> Path | None:
    for rel in (r"AppData\Local\Google\Chrome\User Data",
                r"AppData\Local\Google\Chrome Beta\User Data"):
        p = Path.home() / rel
        if p.exists():
            return p
    return None


def http_json(url: str, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def cdp_endpoints(cfg: Config) -> list[str]:
    """按优先级列出候选 CDP 端点。

    `chrome://inspect` 开关打开的那种实例（Chrome 146+ 新特性模式）**不提供**
    `/json/version`（实测 404），所以不能用 HTTP 预检来判断端点是否可用——那样会
    把好端点误判成不可用。权威入口是 Chrome 写在 user-data-dir 下的
    DevToolsActivePort：第一行端口，第二行 browser 级 WebSocket 路径。
    因此这里 ws:// 排最前，http:// 只作为老式实例的兜底。
    """
    out: list[str] = []
    ud = Path(cfg.chrome_user_data_dir) if cfg.chrome_user_data_dir else default_chrome_user_data()
    if ud:
        f = ud / "DevToolsActivePort"
        if f.exists():
            try:
                lines = [x.strip() for x in
                         f.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()]
                if lines and lines[0].isdigit():
                    port = lines[0]
                    if len(lines) > 1 and lines[1].startswith("/"):
                        out.append(f"ws://127.0.0.1:{port}{lines[1]}")
                    out.append(f"http://127.0.0.1:{port}")
            except Exception as exc:
                log.debug("读 DevToolsActivePort 失败：%s", exc)
    for port in cfg.cdp_ports:
        ep = f"http://127.0.0.1:{port}"
        if ep not in out:
            out.append(ep)
    return out


def cdp_unavailable_hint(cfg: Config) -> str:
    ud = cfg.chrome_user_data_dir or str(default_chrome_user_data() or
                                          r"%LOCALAPPDATA%\Google\Chrome\User Data")
    return (
        "没能附加到 Chrome。请确认：\n"
        "  1) 日常 Chrome 正在运行，且已登录腾讯云\n"
        "  2) 已在 chrome://inspect/#remote-debugging 勾选\n"
        "     「Allow remote debugging for this browser instance」\n"
        f"     （脚本会读 {ud}\\DevToolsActivePort，其次试端口 {cfg.cdp_ports}）\n"
        "  说明：Chrome 136+ 禁止用 --remote-debugging-port 挂默认 profile，必须走这个开关；\n"
        "        该模式下不提供 /json/version，脚本用 DevToolsActivePort 里的 ws:// 直连。"
    )


# --------------------------------------------------------------------------- #
# 时钟校准
# --------------------------------------------------------------------------- #


class Clock:
    """估算本地时钟相对腾讯服务器的偏差。

    刻意**不**用 HTTP Date 头做毫秒级校准：Date 只精确到整秒，服务器真实时刻落在
    [Date, Date+1s)，观测区间宽度必然 ≥1s。把它当偏移量会把本来已被 w32time/NTP
    对齐到几毫秒的系统时钟推偏几百毫秒 —— 2026-10-20 就出现过同一台机器两次校准
    分别得到 +0.02s 和 -0.23s 的情况，这 250ms 全是取整噪声。

    做法：收集各样本的观测区间取交集，只有整个区间都明显偏离 0 才采用中点修正；
    否则 offset 保持 0，把不确定度写进日志。
    """

    def __init__(self, url: str, tolerance_s: float = 1.2) -> None:
        self.offset = 0.0
        self.uncertainty_ms = 0.0
        self._url = url
        self.tolerance_s = tolerance_s

    def sync(self, samples: int = 3) -> float:
        lo_all: list[float] = []
        hi_all: list[float] = []
        for _ in range(max(1, samples)):
            try:
                t0 = time.time()
                r = requests.head("https://cloud.tencent.com/", timeout=5,
                                  headers={"User-Agent": "Mozilla/5.0"})
                t1 = time.time()
                date_hdr = r.headers.get("Date")
                if not date_hdr:
                    continue
                server_ts = parsedate_to_datetime(date_hdr).timestamp()
            except Exception as exc:  # 网络失败就用本地时间
                log.debug("对时失败：%s", exc)
                continue
            # 服务器真实时刻 ∈ [server_ts, server_ts+1)，本地真实时刻 ∈ [t0, t1]
            lo_all.append(server_ts - t1)
            hi_all.append(server_ts + 1.0 - t0)
        if not lo_all:
            self.offset = 0.0
            self.uncertainty_ms = 0.0
            return 0.0
        lo, hi = max(lo_all), min(hi_all)
        if lo > hi:  # 各样本交集为空（网络抖动），退回全包区间
            lo, hi = min(lo_all), max(hi_all)
        mid = (lo + hi) / 2
        self.uncertainty_ms = max(0.0, (hi - lo) / 2 * 1000)
        self.offset = mid if abs(mid) > self.tolerance_s else 0.0
        return self.offset

    def now(self) -> float:
        return time.time() + self.offset

    def stamp(self, ts: float | None = None) -> str:
        return datetime.fromtimestamp(self.now() if ts is None else ts).strftime("%H:%M:%S.%f")[:-3]


# --------------------------------------------------------------------------- #
# 抢购主体
# --------------------------------------------------------------------------- #


class Seckiller:
    def __init__(self, cfg: Config, clock: Clock, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.clock = clock
        self.dry_run = dry_run
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.attached = False  # True = 附加到用户日常 Chrome，绝不关闭它
        self.shots = 0
        # 预热时记下的按钮中心坐标：开抢后哨兵没赶上时，用它做「抢跑点击」兜底
        self._btn_xy: tuple[float, float] | None = None
        self._hovered = False

    # ---------------- 生命周期 ---------------- #

    def start(self) -> None:
        if self._pw is None:
            self._pw = sync_playwright().start()
        if self.cfg.chrome_mode == "cdp":
            self._start_cdp()
        else:
            self._start_launch()

    def _start_cdp(self) -> None:
        """附加到用户日常 Chrome，直接复用它的登录态。

        Chrome 136 起，默认 user-data-dir 上不能再靠 `--remote-debugging-port`
        启动调试端口；需要在 chrome://inspect/#remote-debugging 里手动打开
        「Allow remote debugging for this browser instance」开关。
        """
        assert self._pw is not None
        endpoint = self._connect_cdp()
        if not endpoint:
            raise RuntimeError(cdp_unavailable_hint(self.cfg))

        if not self.browser.contexts:
            raise RuntimeError("已连上 Chrome，但拿不到浏览器上下文（Chrome 可能刚启动）")
        self.context = self.browser.contexts[0]
        self.attached = True

        page: Page | None = None
        if self.cfg.reuse_existing_tab:
            for p in self.context.pages:
                try:
                    if "cloud.tencent.com" in (p.url or ""):
                        page = p
                        break
                except Exception:
                    continue
        if page is None:
            page = self.context.new_page()
            log.info("没有已打开的腾讯云标签页，新开一个")
        else:
            log.info("复用已打开的标签页：%s", (page.url or "")[:90])
        self.page = page

        ck = {c["name"] for c in self.context.cookies("https://cloud.tencent.com")}
        log.info("已附加到日常 Chrome（%s），上下文标签页 %d 个，腾讯云 cookie %d 个",
                 endpoint, len(self.context.pages), len(ck))

    def _connect_cdp(self) -> str | None:
        """依次尝试候选端点，返回连上的那个。

        超时给足：Playwright 连上后会 auto-attach 到浏览器里**所有** target，日常
        Chrome 常开着几十个标签页/扩展，逐个 enable 各 domain 可能要十几秒
        （实测 15s 会超时、25s 才稳定连上），所以这里给 90 秒。
        """
        assert self._pw is not None
        endpoints = cdp_endpoints(self.cfg)
        if not endpoints:
            return None
        log.info("候选调试端点：%s", endpoints)
        last_err: Exception | None = None
        # 跑两轮：DevToolsActivePort 里的 ws 路径会随 Chrome 重启失效，
        # 第二轮重新读文件，能拿到新的 uuid。
        for attempt in (1, 2):
            for ep in endpoints:
                t0 = time.time()
                try:
                    self.browser = self._pw.chromium.connect_over_cdp(ep, timeout=90000)
                    log.info("已连上 %s（耗时 %.1fs）", ep, time.time() - t0)
                    return ep
                except PWError as exc:
                    last_err = exc
                    log.debug("端点 %s 连接失败（%.1fs）：%s", ep, time.time() - t0, str(exc)[:160])
            if attempt == 1:
                endpoints = cdp_endpoints(self.cfg)
        if last_err:
            log.error("所有候选端点都失败，最后一个错误：%s", str(last_err)[:300])
        return None

    def _start_launch(self) -> None:
        self.cfg.user_data_dir.mkdir(parents=True, exist_ok=True)
        assert self._pw is not None
        args = [
            "--disable-blink-features=AutomationControlled",
            # 防止窗口被遮挡/后台时定时器与渲染被节流，这是秒杀脚本最常踩的坑
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=CalculateNativeWinOcclusion",
            f"--window-size={self.cfg.window_size}",
            "--window-position=0,0",
            "--lang=zh-CN",
        ]
        kwargs: dict[str, Any] = dict(
            headless=self.cfg.headless,
            args=args,
            no_viewport=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            accept_downloads=True,
        )
        if self.cfg.channel:
            kwargs["channel"] = self.cfg.channel
        try:
            self.context = self._pw.chromium.launch_persistent_context(
                str(self.cfg.user_data_dir), **kwargs
            )
        except PWError as exc:
            if self.cfg.channel:
                log.warning("用系统 Chrome 启动失败（%s），回退到 Playwright 自带 Chromium", exc)
                kwargs.pop("channel", None)
                self.context = self._pw.chromium.launch_persistent_context(
                    str(self.cfg.user_data_dir), **kwargs
                )
            else:
                raise

        self.context.set_default_timeout(15000)
        self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        log.info("浏览器已启动（channel=%s, headless=%s, profile=%s）",
                 self.cfg.channel or "chromium", self.cfg.headless, self.cfg.user_data_dir)

    def start_playwright(self) -> None:
        if self._pw is None:
            self._pw = sync_playwright().start()

    def check_cdp(self) -> int:
        """诊断：附加到 Chrome 调试端点，并打印它带的登录态。"""
        self.start_playwright()
        print(f"候选端点：{cdp_endpoints(self.cfg)}")
        endpoint = self._connect_cdp()
        if not endpoint or not self.browser:
            print("\n" + cdp_unavailable_hint(self.cfg))
            if self._pw:
                self._pw.stop()
            return 2
        print(f"\n[OK] 已连上：{endpoint}")
        print(f"     Browser version: {self.browser.version}")

        ctxs = self.browser.contexts
        print(f"[OK] 浏览器上下文 {len(ctxs)} 个")
        if ctxs:
            ctx = ctxs[0]
            pages = ctx.pages
            print(f"     标签页 {len(pages)} 个：")
            for p in pages[:15]:
                try:
                    print("       -", (p.url or "")[:110])
                except Exception:
                    print("       - (读取失败)")
            if len(pages) > 15:
                print(f"       … 还有 {len(pages) - 15} 个")
            ck = {c["name"]: c["value"] for c in ctx.cookies("https://cloud.tencent.com")}
            print(f"\n     cloud.tencent.com cookie {len(ck)} 个：{sorted(ck)}")
            logged = bool(ck.get("skey") or ck.get("uin") or ck.get("qcloud_uid"))
            print(f"     登录态判定：{'已登录' if logged else '未登录（请确认这个 Chrome 里已登录腾讯云）'}")
        print("\n诊断结束（未改动任何东西，也不会关闭你的浏览器）")
        if self._pw:
            self._pw.stop()
        return 0

    def stop(self) -> None:
        try:
            if self.page and not self.page.is_closed():
                self.stop_watcher()
        except Exception:
            pass
        if self.attached:
            # 附加模式：只断开 CDP，绝不能关掉用户正在用的 Chrome
            log.info("断开与日常 Chrome 的连接（浏览器保持运行）")
        else:
            try:
                if self.context:
                    self.context.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def _ensure_page(self) -> Page:
        if self.page is None or self.page.is_closed():
            assert self.context is not None
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self.page

    # ---------------- 登录 ---------------- #

    def cookies(self) -> dict[str, str]:
        assert self.context is not None
        return {c["name"]: c["value"] for c in self.context.cookies("https://cloud.tencent.com")}

    def is_logged_in(self) -> bool:
        ck = self.cookies()
        return bool(ck.get("skey") or ck.get("uin") or ck.get("qcloud_uid"))

    def login_flow(self) -> int:
        """打开登录页，等用户手动登录完成后回车。"""
        assert self.context is not None
        page = self._ensure_page()
        if self.is_logged_in():
            log.info("检测到已有登录态，无需重复登录")
        page.goto("https://cloud.tencent.com/login", wait_until="domcontentloaded", timeout=60000)
        page.bring_to_front()
        print("\n>>> 请在打开的浏览器里完成登录（支持微信/QQ/账号密码）。")
        print(">>> 登录成功后回到本终端按回车。\n")
        try:
            input()
        except EOFError:
            page.wait_for_timeout(180000)
        if self.is_logged_in():
            log.info("登录态已保存到 %s", self.cfg.user_data_dir)
            return 0
        log.error("没有检测到登录 cookie（skey/uin），请重试 --login")
        return 2

    # ---------------- 页面操作 ---------------- #

    def open_activity(self) -> None:
        page = self._ensure_page()
        log.info("打开活动页……")
        page.goto(self.cfg.url, wait_until="domcontentloaded", timeout=60000)
        # 强制重载：goto 到**完全相同**的 URL（含 #MS）时 Chrome 不一定重新加载页面，
        # 于是上一轮留下的 DOM 改动/标记会残留（实测：verify 改过的按钮文案在下一轮
        # 依然存在）。预热阶段离开抢还有几分钟，重载很安全，且能保证起点是干净初始态。
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
        except PWError as exc:
            log.debug("reload 失败（不影响后续流程）：%s", exc)
        page.bring_to_front()
        page.wait_for_timeout(3000)
        self.dismiss_popups()
        page.evaluate("() => { const el = document.querySelector('#MS'); if (el) el.scrollIntoView({block: 'start'}); }")
        page.wait_for_timeout(800)
        state = page.evaluate(JS_PAGE_STATE)
        log.info("页面状态：url=%s visibility=%s", state["url"], state["visibility"])
        if state["visibility"] != "visible":
            log.warning("页面不在前台，浏览器可能节流定时器，建议不要最小化窗口")

    def dismiss_popups(self) -> None:
        """关掉活动页常见的浮层（活动规则/优惠券/客服），避免遮挡按钮。

        只点明确的浮层关闭按钮，不做 `[class*="close"]` 这种宽泛匹配——那会误点
        页面上其它正常模块的关闭按钮，把页面点乱。
        """
        page = self._ensure_page()
        js = """() => {
          const sels = ['.uno3-rule-close', '.uno3-dialog__close', '.qc-base-modal__close',
                        '.uno3-rule-panel-close', '[class*="modal"] [class*="close"]'];
          let n = 0;
          for (const s of sels) {
            document.querySelectorAll(s).forEach((e) => {
              const r = e.getBoundingClientRect();
              if (r.width > 0 && r.height > 0) { e.click(); n += 1; }
            });
          }
          // 抢购点击失败后页面会弹「抱歉，当前商品已售罄」确认框（实测 2026-10-19
          // 10:00 场，遮罩就是 .uno3-dialog-mask）。不点掉它，遮罩会一直压在按钮上，
          // 之后每轮都只能等 1.5s 超时再靠 JS 直达点击，90 秒里白烧掉 21 次点击。
          document.querySelectorAll('.uno3-confirm-dialog, .uno3-dialog').forEach((d) => {
            for (const c of d.querySelectorAll('button, [class*="btn"]')) {
              const t = (c.innerText || '').trim();
              if (/^(确认|确定|我知道了|知道了|好的)$/.test(t)) { c.click(); n += 1; break; }
            }
          });
          return n;
        }"""
        try:
            closed = page.evaluate(js)
            if closed:
                log.info("关掉了 %d 个浮层", closed)
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    # ---------------- 定位 ---------------- #

    def scan(self) -> dict[str, Any]:
        page = self._ensure_page()
        return page.evaluate(JS_SCAN, {
            "cardSelector": self.cfg.card_selector,
            "btnSelector": BTN_SELECTOR,
            "anchors": self.cfg.card_anchors,
            "buyTexts": self.cfg.buy_texts,
            "statusTexts": self.cfg.ready_texts + self.cfg.soldout_texts,
        })

    def locate(self, max_wait_s: float = 60.0) -> dict[str, Any] | None:
        """扫描并挑出目标卡片；返回该卡片信息（含卡片内按钮的文案与坐标）。"""
        deadline = time.time() + max_wait_s
        last: dict[str, Any] | None = None
        while time.time() < deadline:
            data = self.scan()
            last = data
            picked = self._pick(data)
            if picked:
                self._mark(picked["cidx"])
                b = picked["btn"]
                log.info("已定位目标卡片：%s", picked["text"][:48])
                if b:
                    self._btn_xy = (b["x"], b["y"])
                    log.info("  卡片内按钮：text=%s cls=%s 坐标=(%.0f,%.0f) 可点=%s",
                             b["text"], b["cls"], b["x"], b["y"], not b["disabled"])
                else:
                    log.info("  该卡片当前没有识别到按钮（文案不在配置里），开抢后在卡片内重新查找")
                return picked
            time.sleep(0.5)
        self._dump_scan(last, "未能定位到目标卡片")
        return None

    def _pick(self, data: dict[str, Any]) -> dict[str, Any] | None:
        cards = list(data["cards"])
        if not cards:
            return None
        # 优先级：卡片内有可识别按钮 > 在视口内 > y 最小（秒杀区在页面顶部）。
        # 不强制要求有按钮——预热期按钮文案可能是「即将开抢」这类没配置过的文案，
        # 只要能定位并标记卡片，tick 阶段照样能在卡片里找到「立即抢购」。
        cards.sort(key=lambda c: (
            0 if c["btn"] else 1,
            0 if c["inViewport"] else 1,
            c["rect"]["y"],
            c["rect"]["x"],
        ))
        return cards[0]

    def _mark(self, cidx: int) -> bool:
        page = self._ensure_page()
        return bool(page.evaluate(JS_MARK, cidx))

    def _dump_scan(self, data: dict[str, Any] | None, why: str) -> None:
        if not data:
            log.error("%s（扫描无结果）", why)
            return
        log.error("%s。扫描卡片 %d 个，锚点文本 %s 命中 %d 张，页面状态文案 %s",
                  why, data["scanned"], self.cfg.card_anchors, data["cardCount"], data["statusSeen"])
        for c in data["cards"][:20]:
            b = c["btn"]
            log.error("  卡片: rect=%s inViewport=%s 实际按钮文案=%r 已识别=%s",
                      c["rect"], c["inViewport"], c.get("rawBtn"),
                      f"{b['text']}({b['cls'][:30]})" if b else "无")
        out = self.cfg.log_dir / "scan-dump.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log.error("完整扫描结果已写入 %s，可用它调整 config.toml 的 card_anchors", out)

    def probe(self) -> int:
        """诊断模式：打开页面并把候选卡片全部打印出来。"""
        self.start()
        try:
            self.open_activity()
            self._ensure_page().wait_for_timeout(3000)
            data = self.scan()
            print(json.dumps(data, ensure_ascii=False, indent=2))
            out = self.cfg.log_dir / "probe.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"\n扫描结果已写入：{out}")
            print(f"扫到卡片 {data['scanned']} 个；锚点 {self.cfg.card_anchors} 命中 {data['cardCount']} 张；"
                  f"当前状态文案 {data['statusSeen']}")
            if data.get("cards"):
                print(f"\n命中锚点的卡片 {data['cardCount']} 张（脚本的候选）：")
                for c in data["cards"]:
                    b = c["btn"]
                    print(f"  cidx={c['cidx']} rect={c['rect']} inViewport={c['inViewport']} "
                          f"实际按钮文案={c.get('rawBtn')!r} 已识别={b['text'] if b else '未识别'}")
                    print(f"      {c['text'][:80]}")
            if data.get("others"):
                print(f"\n视口内未命中锚点的卡片 {len(data['others'])} 张（用来人工校准 card_anchors）：")
                for c in data["others"]:
                    print(f"  rect={c['rect']} 按钮={c['btn']!r} | {c['text'][:70]}")
            picked = self._pick(data)
            if picked:
                print(f"\n会选中这张卡片：{picked['text'][:60]}")
                print(f"  按钮：{json.dumps(picked['btn'], ensure_ascii=False)}")
                self._mark(picked["cidx"])
            else:
                print("\n没有命中任何卡片，请调整 card_anchors")
        finally:
            self.stop()
        return 0

    # ---------------- 点击与判定 ---------------- #

    def tick(self) -> dict[str, Any]:
        page = self._ensure_page()
        return page.evaluate(JS_TICK, {
            "buyTexts": self.cfg.buy_texts,
            "statusTexts": self.cfg.ready_texts + self.cfg.soldout_texts,
        })

    # ---------------- 页面内哨兵 ---------------- #

    def arm_watcher(self, fire_at: float, blind_at: float) -> bool:
        """在页面里装「抢购哨兵」。

        fire_at / blind_at 传本地墙钟时间戳（秒），和页面里的 Date.now() 同一坐标系；
        调用方要用 `fire_at - clock.offset` 换算，不能直接传校准后的 fire_at。
        """
        page = self._ensure_page()
        try:
            res = page.evaluate(JS_ARM, {
                "buyTexts": self.cfg.buy_texts,
                "btnSelector": BTN_SELECTOR,
                "fireAtWall": int(fire_at * 1000),
                "earliestWall": int((fire_at - self.cfg.allow_early_ms / 1000.0) * 1000),
                "blindAtWall": int(blind_at * 1000),
            })
        except PWError as exc:
            log.warning("装页面内哨兵失败（%s），退回 Python 侧轮询", str(exc)[:120])
            return False
        ok = bool(res and res.get("ok"))
        if ok:
            log.info("页面内哨兵就位：出现「%s」会在页面内当帧点击（MutationObserver + rAF）",
                     "/".join(self.cfg.buy_texts))
        return ok

    def watch_state(self) -> dict[str, Any] | None:
        """读哨兵状态；哨兵不在（页面跳转/被清掉/尚未武装）返回 None。"""
        try:
            return self._ensure_page().evaluate(JS_WATCH_STATE)
        except PWError:
            return None

    def stop_watcher(self) -> None:
        try:
            self._ensure_page().evaluate(JS_WATCH_STOP)
        except PWError:
            pass

    @staticmethod
    def watcher_timeline(w: dict[str, Any]) -> str:
        """把哨兵记录的按钮文案变化排成「文本@相对开抢的毫秒数」。"""
        fire_ms = float(w.get("fireAtWall") or 0)
        parts = []
        for t in w.get("texts") or []:
            at = float(t.get("wall") or 0) - fire_ms
            parts.append(f"「{t.get('text')}」{at:+.0f}ms")
        return " → ".join(parts[-6:]) or "(无文案变化)"

    def scroll_to_card(self) -> None:
        try:
            self._ensure_page().evaluate(JS_SCROLL)
        except Exception:
            pass

    def click_at(self, x: float, y: float) -> None:
        page = self._ensure_page()
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.up()

    def hover_target(self) -> None:
        """把鼠标提前挪到按钮上。

        盲点兜底要按「开抢 +100ms」发出点击，而一次完整坐标点击是 move/down/up
        三次 CDP 往返（实测 34ms）。提前 hover 后只剩 down/up 两次，约省一半。
        """
        if not self._btn_xy:
            return
        try:
            self._ensure_page().mouse.move(*self._btn_xy)
            self._hovered = True
        except Exception:
            pass

    def press_here(self) -> float:
        """在鼠标当前位置按下并抬起，返回按下那一刻的校准时间戳。"""
        page = self._ensure_page()
        page.mouse.down()
        at = self.clock.now()
        page.mouse.up()
        return at

    def click_via_js(self) -> dict[str, Any]:
        """直接对按钮元素派发原生 click，绕过浮层遮挡。

        事件 isTrusted=false，页面若校验它会被忽略；但遮挡时坐标点击打在浮层上，
        本来也点不到按钮，所以这条路只会更好。实测（logs/diag4）只能确证坐标
        链路本身没问题，遮挡是点击之后才出现的，所以保留它作为主路径的兜底。
        """
        page = self._ensure_page()
        return page.evaluate(JS_CLICK_BUY, list(self.cfg.buy_texts))

    def wait_mask_gone(self, timeout_s: float) -> float | None:
        """等「抢购点击后盖上的那层遮罩」退掉，返回耗时秒数；超时返回 None。

        这层遮罩是请求进行中的正常表现：按钮文案此时还没变、坐标点上是遮罩本身。
        老版本把它当成「被浮层挡住」，于是继续点 —— 等于在请求还没回来时重复提交，
        既拿不到结果，又踩风控。正确做法是先等它退，再看按钮变成了什么。
        """
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            st = self.tick()
            if st.get("state") != "ready" or not st.get("obstructed"):
                return time.time() - t0
            time.sleep(0.06)
        return None

    def tips(self) -> list[str]:
        """当前可见的提示/弹层文案（点击被拒绝时原因往往只在这里闪几秒）。"""
        try:
            return list(self._ensure_page().evaluate(JS_TIPS) or [])
        except Exception:
            return []

    def shot(self, tag: str) -> None:
        if not self.cfg.screenshot:
            return
        page = self.page
        if not page or page.is_closed():
            return
        try:
            self.cfg.log_dir.mkdir(parents=True, exist_ok=True)
            name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{tag}.png"
            path = self.cfg.log_dir / name
            page.screenshot(path=str(path))
            log.info("已截图 %s", path)
        except Exception as exc:
            log.debug("截图失败：%s", exc)

    def beep(self) -> None:
        if winsound:
            try:
                for _ in range(3):
                    winsound.Beep(1200, 180)
            except Exception:
                pass

    # ---------------- 单场流程 ---------------- #

    def run_round(self, fire_at: float) -> bool:
        cfg = self.cfg
        page = self._ensure_page()

        # 1) 预热点：打开页面 + 确认登录 + 定位按钮
        self.open_activity()
        if not self.is_logged_in():
            log.error("未检测到登录态，请先运行：python seckill.py --login")
            self.shot("not-logged-in")
            return False

        target = self.locate()
        if not target:
            self.shot("target-not-found")
            return False
        self.shot("before-start")

        # 2) 等到高频窗口
        hot_at = fire_at - cfg.hot_seconds
        log.info("开抢时刻 %s，将于 %s 进入高频轮询（间隔 %dms）",
                 self.clock.stamp(fire_at), self.clock.stamp(hot_at), cfg.hot_interval_ms)
        warned_early_ready = False
        warm_interval = max(0.02, cfg.warm_interval_ms / 1000.0)
        while self.clock.now() < hot_at:
            remain = hot_at - self.clock.now()
            time.sleep(min(warm_interval, max(0.02, remain)))
            st = self.tick()
            if st["state"] == "lost":
                log.info("卡片容器丢失，重新定位……")
                again = self.locate(max_wait_s=10)
                if not again:
                    self.shot("relocate-failed")
                    return False
            elif st["state"] == "ready" and not warned_early_ready:
                # 实测坑：页面刚导航完的首帧里按钮可能短暂显示「立即抢购」，
                # 秒杀接口数据回来后才变成真实状态（添加提醒/取消提醒/已抢光）。
                # 这里只警告不点击——真正点击由下面 earliest 的时间闸门放行。
                warned_early_ready = True
                log.warning("预热期目标按钮已显示「%s」，距开抢还有约 %.0f 秒："
                            "可能是页面加载暂态，也可能该商品当前就在售。"
                            "脚本只会在开抢前 %dms 内才真正点击",
                            st["text"], hot_at - self.clock.now(), cfg.allow_early_ms)

        # 3) 高频轮询 → 命中即点
        hard_deadline = fire_at + cfg.give_up_seconds
        earliest = fire_at - cfg.allow_early_ms / 1000.0
        attempts = 0
        last_status = ""
        obstructed_hits = 0
        # 页面里的 Date.now() 是本地墙钟，fire_at 是校准后的时间，换算回墙钟再交给哨兵
        fire_at_wall = fire_at - self.clock.offset
        blind_at = fire_at + cfg.blind_click_ms / 1000.0
        # dry-run 绝不能装哨兵：它在页面里是真的会点下去的
        watched = (not self.dry_run) and self.arm_watcher(fire_at_wall, blind_at)
        blind_sent = False
        self._hovered = False
        while self.clock.now() < hard_deadline:
            if page.is_closed():
                log.error("页面已关闭，本场结束")
                return False

            # 临门一脚：提前把鼠标挪到按钮上，盲点兜底就只剩 down/up
            if not self._hovered and self._btn_xy and self.clock.now() >= fire_at - 0.3:
                self.hover_target()

            # 页面内哨兵优先：它命中时点击已经在页面里发出，这里只补真实鼠标事件 + 判定结果
            if watched:
                w = self.watch_state()
                if w is None:
                    watched = False
                else:
                    if w.get("lost"):
                        log.info("卡片容器丢失（可能被框架重建），重新定位……")
                        again = self.locate(max_wait_s=8)
                        if not again:
                            self.shot("relocate-failed")
                            return False
                        page.wait_for_timeout(20)
                        continue
                    if w.get("hits"):
                        watched = False
                        attempts += 1
                        done = self._handle_hit(w, attempts)
                        if done is not None:
                            return done
                        time.sleep(0.05)
                        continue
                    # 开抢后 N 毫秒还没命中「立即抢购」：按硬指标先发一次抢跑点击，
                    # 别让整场都耗在等页面把文案切过来（2026-10-20 15:00 场晚了约 0.8s）。
                    # 判定提前 60ms：按下要占两次 CDP 往返，不提前就来不及落在 +100ms 内。
                    if (cfg.blind_click_ms > 0 and not blind_sent
                            and self._btn_xy and self.clock.now() >= blind_at - 0.06):
                        blind_sent = True
                        st_b = self.tick()
                        if st_b.get("state") == "ready":
                            continue  # 哨兵马上就会点，不用抢跑
                        cur = (st_b.get("text") or "/".join(st_b.get("status") or [])
                               or f"状态 {st_b.get('state')}")
                        # 「取消提醒」是唯一会被抢跑点坏的文案：点了会真的把提醒退掉。
                        # 「添加提醒」最多多设一个提醒，可以接受。
                        if cur and cur in cfg.ready_texts and "取消" in cur:
                            log.warning(
                                "开抢 +%dms 哨兵仍未命中，按钮是「%s」（点了会取消提醒），跳过抢跑点击",
                                cfg.blind_click_ms, cur)
                        else:
                            if not self._hovered:
                                self.hover_target()
                            try:
                                if self._hovered:
                                    at = self.press_here()
                                else:  # 预移动失败：退回完整坐标点击
                                    self.click_at(*self._btn_xy)
                                    at = self.clock.now()
                            except Exception as exc:
                                log.warning("抢跑点击异常：%s", exc)
                            else:
                                log.warning(
                                    "开抢 +%dms 硬指标：哨兵仍未命中（按钮当前 %s），"
                                    "抢跑点击已发出 @ %s（开抢 %+dms）",
                                    cfg.blind_click_ms, cur,
                                    self.clock.stamp(at), (at - fire_at) * 1000)
                                page.wait_for_timeout(30)
                                self.dismiss_popups()

            st = self.tick()
            status_txt = "/".join(st.get("status") or [])

            if st["state"] == "lost":
                log.info("卡片容器丢失（可能被框架重建），重新定位……")
                again = self.locate(max_wait_s=8)
                if not again:
                    self.shot("relocate-failed")
                    return False
                continue

            if st["state"] == "offscreen":
                log.info("目标卡片滑出视口，滚回视口中间")
                self.scroll_to_card()
                page.wait_for_timeout(80)
                continue

            if st["state"] == "idle":
                if status_txt and status_txt != last_status:
                    last_status = status_txt
                    log.info("等待开抢，当前按钮文案：%s（%s）", status_txt, self.clock.stamp())
                # 临门一脚把间隔压到 8ms：盲点兜底要落在「开抢 +100ms」内，
                # 30ms 的轮询间隔光排队就能吃掉三分之一。
                time.sleep(0.008 if self.clock.now() >= fire_at - 0.5
                           else cfg.hot_interval_ms / 1000.0)
                continue

            # state == ready
            if self.clock.now() < earliest:
                time.sleep(0.005)
                continue
            if st.get("disabled"):
                log.info("按钮文案已是 %s 但当前不可点，继续等", st["text"])
                time.sleep(cfg.hot_interval_ms / 1000.0)
                continue
            # 哨兵通常已经抢在前面点过（它在页面内 0~1ms 就能动，tick 要 1~8ms）。
            # 先认领它的记录，别再补一次坐标点击 —— 那就是重复提交。
            if watched:
                w2 = self.watch_state()
                if w2 and w2.get("hits"):
                    watched = False
                    attempts += 1
                    done = self._handle_hit(w2, attempts)
                    if done is not None:
                        return done
                    time.sleep(0.05)
                    continue
            # 遮挡不再拦下点击。原来这里连续 6 轮「只关浮层不点击」，实测每轮约
            # 85ms、整整 11.6 秒只发出 20 次点击，窗口全耗在关浮层上；而且坐标点击
            # 打在浮层上本来就是白费。改法：前两次照样发坐标点击（elementFromPoint
            # 偶尔误判），之后一律 JS 直达点击。
            use_js = False
            if st.get("obstructed"):
                obstructed_hits += 1
                hit = st.get("hit") or "未知浮层"
                # 抢购点击后页面会盖一层遮罩（请求进行中）。先等它退，不要在这期间
                # 连点：那既拿不到结果，又是重复提交。等不到才认定是真·浮层，走 JS。
                waited = self.wait_mask_gone(cfg.mask_wait_ms / 1000.0)
                if waited is not None:
                    log.info("按钮被 %s 遮住，%.2fs 后遮罩退掉，重新判定", hit, waited)
                    continue
                if obstructed_hits == 1:
                    log.warning("按钮被 %s 遮住且 %.1fs 没退 —— 不像点击遮罩，改用 JS 直达点击",
                                hit, cfg.mask_wait_ms / 1000.0)
                    self.dismiss_popups()
                else:
                    log.warning("按钮仍被 %s 遮住，JS 直达点击", hit)
                use_js = True
            else:
                # 遮罩退干净了，回到坐标点击（isTrusted=true，比 JS 派发更可信）
                obstructed_hits = 0

            attempts += 1
            if use_js:
                log.warning("第 %d 次命中「%s」，JS 直达点击（绕过 %s）@ %s",
                            attempts, st["text"], st.get("hit") or "浮层", self.clock.stamp())
            else:
                log.warning("第 %d 次命中「%s」，点击 (%.0f, %.0f) @ %s",
                            attempts, st["text"], st["x"], st["y"], self.clock.stamp())
            if self.dry_run:
                log.warning("dry-run：命中「%s」，不实际点击", st["text"])
                self.shot("dry-run-hit")
                return True

            before_url = ""
            try:
                before_url = page.url
            except PWError:
                pass
            t_click = time.time()
            try:
                if use_js:
                    res = self.click_via_js()
                    if not res.get("ok"):
                        log.warning("JS 点击没找到按钮（%s），回退坐标点击", res.get("why"))
                        self.click_at(st["x"], st["y"])
                else:
                    self.click_at(st["x"], st["y"])
            except Exception as exc:
                log.warning("点击异常：%s", exc)
                time.sleep(0.1)
                continue
            log.info("点击已发出，耗时 %.0fms", (time.time() - t_click) * 1000)

            status, detail = self._settle(before_url)
            done = self._after_click(status, detail, attempts)
            if done is not None:
                return done
            time.sleep(0.05)

        self.shot("timeout")
        log.error("超过放弃时限（%ds）仍未抢到", cfg.give_up_seconds)
        return False

    def _handle_hit(self, w: dict[str, Any], attempts: int) -> bool | None:
        """认领哨兵的一次命中：停哨兵 → 补真实点击 + 判定 → 统一处理结果。"""
        self.stop_watcher()
        status, detail = self._watcher_click(w, attempts)
        return self._after_click(status, detail, attempts)

    def _watcher_click(self, w: dict[str, Any], attempts: int) -> tuple[str, str]:
        """处理哨兵的第一次命中：页面内的 click 已经发出，这里补真实事件并判定结果。

        返回值与 _settle 相同（success / captcha / soldout / pending / retry）。
        """
        cfg = self.cfg
        page = self._ensure_page()
        hit = (w.get("hits") or [{}])[0]
        at_ms = int(hit.get("at") or 0)
        log.warning("第 %d 次命中「%s」@ %s（开抢 %+dms）：页面内点击已发出",
                    attempts, hit.get("text") or cfg.buy_texts[0], self.clock.stamp(), at_ms)
        log.info("  页面内时间线：%s；帧数 %d%s，帧间隔峰值 %.0fms",
                 self.watcher_timeline(w), int(w.get("frames") or 0),
                 "（rAF 被浏览器节流，命中来自 MutationObserver）"
                 if not w.get("frames") else "",
                 w.get("maxGapMs") or 0)
        log.info("  哨兵原始记录：wall=%s x=%.0f y=%.0f err=%s",
                 hit.get("wall"), hit.get("x") or 0, hit.get("y") or 0,
                 hit.get("err") or "无")

        before_url = ""
        try:
            before_url = page.url
        except PWError:
            pass

        # 页面内派发的 click 是 isTrusted=false。腾讯这个按钮实测接受合成事件
        # （2026-10-19 的 JS 直达点击把按钮打进了加载态），但万一这次没被接受，
        # 文案会仍停在「立即抢购」——那就补一次真实鼠标点击（isTrusted=true）。
        try:
            page.wait_for_timeout(25)
            st = self.tick()
        except PWError as exc:
            return "success", f"页面已开始跳转（{str(exc)[:60]}）"
        if st.get("state") == "ready":
            x = st.get("x") or hit.get("x") or 0
            y = st.get("y") or hit.get("y") or 0
            log.info("  页面内点击后文案仍是「%s」，补真实坐标点击 (%.0f, %.0f)",
                     st.get("text"), x, y)
            try:
                self.click_at(x, y)
            except Exception as exc:
                log.warning("补充坐标点击异常：%s", exc)
        return self._settle(before_url)

    def _after_click(self, status: str, detail: str, attempts: int) -> bool | None:
        """统一处理一次点击后的结果。

        返回 True/False 表示本场结束（成功/失败），None 表示还能继续重试。
        """
        cfg = self.cfg
        page = self._ensure_page()
        if status == "success":
            self.shot("success")
            self.beep()
            log.warning("已进入购买流程（%s），请人工确认订单并付款 —— 脚本不会自动付款", detail)
            return True
        if status == "captcha":
            self.shot("captcha")
            self.beep()
            log.error("%s，需要人工处理后才能继续", detail)
            return False
        if status == "soldout":
            log.warning("%s，本场未抢到", detail)
            return False

        if status == "pending":
            # 按钮离开了「立即抢购」文案 = 点击被页面收下了，可能正在跳转。
            # 给它时间，不要在跳转过程中接着连点（那是风控最敏感的信号）。
            log.info("第 %d 次点击有响应（%s），等页面反应", attempts, detail)
            try:
                page.wait_for_timeout(700)
                st2 = self.tick()
                if st2.get("state") == "ready":
                    log.info("  按钮回到「%s」，继续重试", st2.get("text"))
                else:
                    log.info("  按钮未恢复（%s），继续观察", st2.get("state"))
            except PWError as exc:
                log.warning("  等待期间页面已跳转（%s）", str(exc)[:80])
        else:
            log.info("第 %d 次点击未见效果（%s）", attempts, detail)

        if attempts >= cfg.max_click_attempts:
            log.error("已尝试 %d 次点击仍未进入购买流程，放弃", attempts)
            self.shot("give-up")
            return False
        return None

    def _settle(self, before_url: str = "") -> tuple[str, str]:
        """点击后判定结果。

        返回 (状态, 说明)，状态取值：
          success  已进入购买流程
          captcha  出现安全验证，需要人工处理
          soldout  按钮已变为售罄
          pending  按钮离开了「立即抢购」文案 —— 点击被页面收下了，正在加载/跳转
          retry    什么都没发生，可以再点
        """
        cfg = self.cfg
        page = self._ensure_page()
        page.wait_for_timeout(cfg.click_settle_ms)

        # 点击成功会立刻导航，此时 evaluate 会因执行上下文销毁而抛错 —— 那正是
        # 「抢到了」的信号，不能让它冒泡出去把整场脚本掀翻（原来没接住这个异常）。
        try:
            state = page.evaluate(JS_PAGE_STATE)
        except PWError as exc:
            return "success", f"页面已开始跳转（{str(exc)[:60]}）"
        url = state["url"]
        if before_url and url != before_url and "act/pro" not in url:
            return "success", f"URL 已跳转 {url}"
        if any(k in url for k in ("buy", "purchase", "order", "deal")):
            return "success", f"URL 已跳转 {url}"
        if state["captcha"]:
            return "captcha", "页面出现安全验证（滑块/验证码）"

        try:
            st = self.tick()
        except PWError as exc:
            return "success", f"页面已开始跳转（{str(exc)[:60]}）"

        if st.get("state") == "ready" and st.get("obstructed"):
            # 点击后盖上的遮罩还在 = 请求还没回来，先别急着判「没效果」
            gone = self.wait_mask_gone(cfg.mask_wait_ms / 1000.0)
            log.info("  点击后遮罩%s",
                     f" {gone:.2f}s 后退去" if gone is not None else "迟迟不退")
            try:
                st = self.tick()
                url2 = page.url
            except PWError as exc:
                return "success", f"页面已开始跳转（{str(exc)[:60]}）"
            if before_url and url2 != before_url and "act/pro" not in url2:
                return "success", f"URL 已跳转 {url2}"

        tips = self.tips()
        if tips:
            # 业务拒绝（未开始/资格不符/已抢光）的原因基本只出现在这里
            joined = " | ".join(tips)
            log.info("  页面提示：%s", joined)
            if any(s in joined for s in cfg.soldout_texts):
                # 实测 2026-10-19 10:00 场：点下去返回的就是「抱歉，当前商品已售罄」。
                # 认下来结束本场，别拿 90 秒去反复点一个已经确定没货的商品。
                self.dismiss_popups()
                return "soldout", f"页面提示售罄（{joined[:80]}）"

        if st["state"] == "idle":
            seen = "/".join(st.get("status") or [])
            if seen and any(s in seen for s in cfg.soldout_texts):
                return "soldout", f"按钮已变为 {seen}"
            if seen:
                return "retry", f"按钮文案回到 {seen}"
            # 既不是「立即抢购」也不在任何已配置文案里：实测点击后按钮会短暂进入
            # 加载态（老日志里那句「按钮仍是 None」就是它），说明点击确实生效了。
            return "pending", "按钮已离开「立即抢购」文案"
        if st["state"] == "lost":
            return "retry", "按钮容器已消失（可能已跳转）"
        return "retry", f"按钮仍是 {st.get('text')}"


# --------------------------------------------------------------------------- #
# 调度
# --------------------------------------------------------------------------- #


def next_fire_at(cfg: Config, clock: Clock, after: float | None = None) -> float:
    """返回 after（默认现在）之后的第一个开抢时刻。

    返回值与 clock.now() 同一坐标系（即校准后的墙上时间戳），调用方统一用
    clock.now() 与它比较。
    """
    base = clock.now() if after is None else after
    now_dt = datetime.fromtimestamp(base)
    best: float | None = None
    for hh, mm, ss in cfg.fire_specs:
        for day_offset in (0, 1):
            cand = now_dt.replace(hour=hh, minute=mm, second=ss, microsecond=0) + timedelta(days=day_offset)
            ts = cand.timestamp()
            if ts > base + 1:
                best = ts if best is None else min(best, ts)
                break
    if best is None:
        raise RuntimeError("fire_times 配置为空")
    return best


def wait_for_user(timeout_s: int) -> None:
    """抢到之后把浏览器留在原地，让用户人工下单付款。"""
    if sys.stdin and sys.stdin.isatty():
        print(">>> 处理完付款后按回车结束脚本……")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
        return
    # 非交互场景（任务计划）：按配置留一段时间再关闭
    log.warning("非交互运行，%d 秒后自动关闭浏览器", timeout_s)
    try:
        time.sleep(timeout_s)
    except KeyboardInterrupt:
        pass


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    handlers.append(logging.FileHandler(
        cfg.log_dir / f"seckill-{datetime.now().strftime('%Y%m%d')}.log", encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="腾讯云限时秒杀自动抢购脚本")
    ap.add_argument("--config", default=str(BASE_DIR / "config.toml"), help="配置文件路径")
    ap.add_argument("--check", action="store_true",
                    help="诊断：探测日常 Chrome 的调试端点并打印它的登录态")
    ap.add_argument("--chrome", action="store_true",
                    help="附加到日常 Chrome（等价于配置 mode = \"cdp\"）")
    ap.add_argument("--local", action="store_true",
                    help="用脚本自己的 profile 启动浏览器（等价于配置 mode = \"launch\"）")
    ap.add_argument("--cdp-port", type=int, default=None, help="手动指定 Chrome 调试端口")
    ap.add_argument("--login", action="store_true", help="打开登录页，手动登录一次并保存登录态")
    ap.add_argument("--probe", action="store_true", help="诊断：打印页面上的候选按钮")
    ap.add_argument("--dry-run", action="store_true", help="走完整流程但不真正点击")
    ap.add_argument("--once", action="store_true", help="只执行最近一场，结束后退出")
    ap.add_argument("--at", default=None, help="只执行指定时刻，如 10:00（需配合 --once）")
    ap.add_argument("--no-wait", action="store_true", help="不等待开抢时间，立即按「可抢购」逻辑尝试")
    args = ap.parse_args(argv)

    cfg = Config.load(Path(args.config))
    if args.chrome:
        cfg.chrome_mode = "cdp"
    if args.local:
        cfg.chrome_mode = "launch"
    if args.cdp_port:
        cfg.cdp_ports = [args.cdp_port] + [p for p in cfg.cdp_ports if p != args.cdp_port]
    setup_logging(cfg)
    log.info("浏览器模式：%s", "附加日常 Chrome（cdp）" if cfg.chrome_mode == "cdp"
             else f"脚本自己启动（profile={cfg.user_data_dir}）")
    clock = Clock(cfg.url)
    off = clock.sync()
    log.info("本地时钟偏差 %+.0fms（正数表示本地慢；HTTP Date 只有秒级精度，"
             "不确定度 ±%.0fms，偏差在 ±%.1fs 内一律按系统时钟处理）；现在（校准后）%s",
             off * 1000, clock.uncertainty_ms, clock.tolerance_s, clock.stamp())

    if args.at:
        hh, mm, *_ = (args.at.split(":") + ["0"])[:2]
        cfg.fire_specs = [(int(hh), int(mm), 0)]
        if not args.once:
            log.info("--at 已指定，自动切换为单场模式")

    if args.check:
        return Seckiller(cfg, clock).check_cdp()

    if args.login:
        if cfg.chrome_mode == "cdp":
            log.error("当前是 cdp 模式，直接用日常 Chrome 的登录态，不需要 --login。"
                      "若想给脚本自己那份 profile 登录，请加 --local。")
            return 2
        s = Seckiller(cfg, clock)
        s.start()
        try:
            return s.login_flow()
        finally:
            s.stop()

    if args.probe:
        return Seckiller(cfg, clock).probe()

    single = args.once or bool(args.at)
    s = Seckiller(cfg, clock, dry_run=args.dry_run)
    rc = 0
    try:
        if args.no_wait:
            s.start()
            try:
                return 0 if s.run_round(clock.now()) else 1
            finally:
                s.stop()

        while True:
            fire_at = next_fire_at(cfg, clock)
            start_at = fire_at - cfg.preload_seconds
            log.info("下一场：%s，将于 %s 开始预热", clock.stamp(fire_at), clock.stamp(start_at))
            while clock.now() < start_at:
                time.sleep(min(5.0, max(0.2, start_at - clock.now())))
            clock.sync()  # 临近开抢再对一次时
            log.info("开始预热，校准后时间 %s", clock.stamp())

            s.start()
            ok = False
            try:
                ok = s.run_round(fire_at)
            finally:
                rc = 0 if ok else 1
                if ok and cfg.keep_open_after:
                    if s.attached:
                        log.warning("抢购流程已触发，请在你自己的 Chrome 窗口里完成下单付款")
                    else:
                        log.warning("抢购流程已触发，浏览器保持打开；请在窗口里完成下单付款")
                    wait_for_user(cfg.keep_open_seconds)
                s.stop()

            if single or ok:
                break
            # 等这一场彻底过去，再排下一场
            while clock.now() < fire_at + max(cfg.give_up_seconds, 60) + 5:
                time.sleep(2)
    except KeyboardInterrupt:
        log.warning("已手动中断")
    return rc


if __name__ == "__main__":
    sys.exit(main())
