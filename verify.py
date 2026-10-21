# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "playwright>=1.45",
#   "requests>=2.31",
# ]
# ///
"""端到端验证（只读、无副作用）：

1. 定位目标卡片；
2. 把按钮文案改成「立即抢购」模拟开抢；
3. 看 tick() 是否返回 ready 且坐标/遮挡判定正确；
4. 用 click_at 发真实鼠标事件，验证它确实落在按钮元素上（isTrusted=true）。

按钮上挂了捕获阶段监听并 stopPropagation，所以不会触发页面真实的抢购逻辑。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import seckill as sk  # noqa: E402

ARM = r"""
(cfg) => {
  const scope = document.querySelector('[data-sk-card="1"]');
  if (!scope) return { ok: false, reason: 'scope missing' };
  // 找到当前状态文案所在的元素——它才是真正的按钮本体，不要把 __btn-progress 那种
  // 子元素当成按钮，否则会点到进度条中间。文案列表从 config.toml 读，避免两处硬编码不一致
  // （之前这里漏了「取消提醒」，页面处于"已设提醒"时就找不到按钮）。
  const norm = (s) => (s || '').replace(/[\s\u00a0\u3000]+/g, '');
  const wanted = cfg.texts;
  let btn = null;
  const tw = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
  let n;
  while ((n = tw.nextNode())) {
    if (wanted.includes(norm(n.nodeValue))) { btn = n.parentElement; break; }
  }
  if (!btn) return { ok: false, reason: 'status button not found' };
  // 防重复挂监听：同一次会话里按钮元素可能被复用，重复挂会让回执计数虚高
  if (btn.__skHooked) {
    window.__skClicks = [];
  } else {
    btn.__skHooked = true;
    window.__skClicks = [];
    btn.addEventListener('click', (e) => {
      window.__skClicks.push({ x: e.clientX, y: e.clientY, trusted: e.isTrusted });
      e.stopPropagation();
      e.preventDefault();
    }, true);
  }
  const r0 = btn.getBoundingClientRect();
  btn.textContent = '立即抢购';
  const r1 = btn.getBoundingClientRect();
  return {
    ok: true,
    cls: String(btn.className),
    text: btn.textContent,
    rectBefore: { x: r0.x, y: r0.y, w: r0.width, h: r0.height },
    rectAfter: { x: r1.x, y: r1.y, w: r1.width, h: r1.height },
  };
}
"""


def main() -> int:
    cfg = sk.Config.load(sk.BASE_DIR / "config.toml")
    sk.setup_logging(cfg)
    clock = sk.Clock(cfg.url)
    clock.sync()
    s = sk.Seckiller(cfg, clock)
    failures: list[str] = []
    s.start()
    try:
        s.open_activity()
        s.page.wait_for_timeout(2000)

        picked = s.locate(max_wait_s=30)
        if not picked:
            print("FAIL 定位卡片失败")
            return 1
        print(f"OK   定位卡片：{picked['text'][:40]}")
        print(f"     原按钮：{picked['btn']['text']} cls={picked['btn']['cls']}")

        # 轮询延迟测量：附加模式下日常 Chrome 可能有几十个标签页在推网络事件，
        # 要确认 tick 没被拖慢（否则 30ms 的轮询间隔会名不副实）
        lat = []
        for _ in range(20):
            t0 = time.perf_counter()
            s.tick()
            lat.append((time.perf_counter() - t0) * 1000)
        lat.sort()
        med = lat[len(lat) // 2]
        print(f"OK   tick 延迟：中位 {med:.1f}ms  最大 {lat[-1]:.1f}ms（20 次；配置间隔 {cfg.hot_interval_ms}ms）")
        if med > cfg.hot_interval_ms * 2:
            failures.append(
                f"tick 中位延迟 {med:.1f}ms 超过轮询间隔 {cfg.hot_interval_ms}ms 的两倍，"
                f"建议把 hot_interval_ms 调到 {int(med * 2)}ms 以上")

        # 未开抢时：应返回 idle，并且能收集到按钮状态文案（日志靠它显示当前进度）
        idle = s.tick()
        print(f"OK   未开抢时的 tick：{json.dumps(idle, ensure_ascii=False)}")
        if idle.get("state") == "idle":
            if not idle.get("status"):
                failures.append("idle 状态但没收集到状态文案，日志会显示空白")
        elif idle.get("state") == "ready":
            print("     注意：页面此刻本来就处于可抢购状态，跳过 idle 断言")
        else:
            failures.append(f"未开抢时 tick 返回异常状态 {idle.get('state')}")

        armed = s.page.evaluate(ARM, {
            "texts": cfg.buy_texts + cfg.ready_texts + cfg.soldout_texts,
        })
        print(f"OK   模拟开抢：{armed}")
        if not armed.get("ok"):
            return 1
        s.page.wait_for_timeout(150)

        st = s.tick()
        print(f"OK   tick：{json.dumps(st, ensure_ascii=False)}")
        if st.get("state") != "ready":
            failures.append(f"tick 未命中 ready，实际 {st.get('state')}")
        if st.get("obstructed"):
            failures.append("tick 判定按钮被遮挡")
        if st.get("disabled"):
            failures.append("tick 判定按钮不可点")

        # 用按钮自身矩形做交叉验证
        box = s.page.evaluate(
            """() => {
                 const scope = document.querySelector('[data-sk-card="1"]');
                 let btn = null;
                 const tw = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
                 let n;
                 while ((n = tw.nextNode())) {
                   if ((n.nodeValue || '').trim() === '立即抢购') { btn = n.parentElement; break; }
                 }
                 if (!btn) return null;
                 const r = btn.getBoundingClientRect();
                 return { x: r.x, y: r.y, w: r.width, h: r.height, cls: String(btn.className) };
               }"""
        )
        if not box:
            print("FAIL 页面上找不到「立即抢购」文本")
            return 1
        exp = (box["x"] + box["w"] / 2, box["y"] + box["h"] / 2)
        got = (st.get("x"), st.get("y"))
        print(f"OK   按钮 rect={box}  期望中心={exp}  tick 坐标={got}")
        if abs(exp[0] - got[0]) > 2 or abs(exp[1] - got[1]) > 2:
            failures.append(f"tick 坐标与按钮中心偏差过大：{got} vs {exp}")
        if box["w"] > 260:
            failures.append(f"命中的按钮宽 {box['w']:.0f}px，疑似点到了进度条/卡片而非按钮")

        s.click_at(got[0], got[1])
        s.page.wait_for_timeout(400)
        clicks = s.page.evaluate("() => window.__skClicks")
        print(f"OK   点击回执：{json.dumps(clicks, ensure_ascii=False)}")
        if not clicks:
            failures.append("点击事件没有落到按钮上")
        elif not clicks[0].get("trusted"):
            failures.append("点击事件 isTrusted=false（会被前端校验拒绝）")

        # JS 直达点击：按钮被浮层盖住时的兜底路径，同样要能落到按钮元素上
        s.page.evaluate("() => { window.__skClicks = []; }")
        jsres = s.click_via_js()
        s.page.wait_for_timeout(300)
        clicks2 = s.page.evaluate("() => window.__skClicks")
        print(f"OK   JS 直达点击：{json.dumps(jsres, ensure_ascii=False)}"
              f" 回执={json.dumps(clicks2, ensure_ascii=False)}")
        if not jsres.get("ok"):
            failures.append(f"JS 直达点击没找到按钮：{jsres.get('why')}")
        elif not clicks2:
            failures.append("JS 直达点击没有落到按钮上")
        elif clicks2[0].get("trusted"):
            failures.append("JS 直达点击的 isTrusted 应为 false")

        # 点击后按钮离开「立即抢购」文案（加载态）应判为 pending，而不是「未见效果」
        s.page.evaluate(
            """() => {
                 const scope = document.querySelector('[data-sk-card="1"]');
                 const tw = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
                 let n;
                 while ((n = tw.nextNode())) {
                   if ((n.nodeValue || '').trim() === '立即抢购') {
                     n.parentElement.textContent = '加载中';
                     return true;
                   }
                 }
                 return false;
               }"""
        )
        settle = s._settle("")
        print(f"OK   加载态 settle：{settle}")
        if settle[0] != "pending":
            failures.append(f"按钮进入加载态时 settle 应返回 pending，实际 {settle[0]}")

        # 页面内哨兵：把按钮文案从「加载中」改回「立即抢购」，模拟页面把文案落进 DOM，
        # 量它多久能触发页面内点击。这是「100ms 内开始第一次点击」的执行路径：
        # 不经过 Python 轮询，也不经过 CDP 鼠标事件往返。
        if not s.arm_watcher(time.time() - 1.0, time.time() + 3600):            failures.append("页面内哨兵安装失败")
        else:
            s.page.wait_for_timeout(300)
            s.page.evaluate("() => { window.__skClicks = []; }")
            t_change = s.page.evaluate(
                """() => {
                     const scope = document.querySelector('[data-sk-card="1"]');
                     const tw = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
                     let n;
                     while ((n = tw.nextNode())) {
                       if ((n.nodeValue || '').trim() === '加载中') {
                         const w = Date.now();
                         n.parentElement.textContent = '立即抢购';
                         return w;
                       }
                     }
                     return 0;
                   }"""
            )
            s.page.wait_for_timeout(250)
            wst = s.watch_state()
            hits = (wst or {}).get("hits") or []
            if not hits:
                failures.append("页面内哨兵没有命中合成切换出来的「立即抢购」")
            else:
                dt = hits[0].get("wall", 0) - t_change
                print(f"OK   哨兵命中延迟 {dt:.0f}ms  {json.dumps(hits[0], ensure_ascii=False)}")
                if dt < 0 or dt > 80:
                    failures.append(f"哨兵命中延迟 {dt:.0f}ms 超出预期（应 ≤80ms）")
                if not hits[0].get("x") or not hits[0].get("y"):
                    failures.append("哨兵命中记录里没有坐标")
            clicks3 = s.page.evaluate("() => window.__skClicks")
            print(f"OK   哨兵点击回执：{json.dumps(clicks3, ensure_ascii=False)}")
            if not clicks3:
                failures.append("哨兵没有把点击落到按钮元素上")
            elif clicks3[0].get("trusted"):
                failures.append("哨兵页面内点击的 isTrusted 应为 false")
            if wst:
                print(f"OK   哨兵时间线：{s.watcher_timeline(wst)}"
                      f" 帧数={wst.get('frames')} 帧间隔峰值={wst.get('maxGapMs'):.0f}ms")

        s.shot("verify-click")
    finally:
        s.stop()

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\n全部通过：定位 → 命中 → 坐标点击 → 事件落在按钮且 isTrusted=true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
