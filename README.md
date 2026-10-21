# 腾讯云「上云精选·限时秒杀」自动抢购脚本

每天 10:00 / 15:00 自动守候活动页，在目标商品卡片上等按钮从「添加提醒 / 已抢光」
变成「立即抢购」，命中即刻点击，直到进入购买流程。

**脚本只负责点进购买流程，不做自动付款。** 抢到后你手动确认订单。

---

## 1. 环境

- Windows + Python 3.11+（用到内置 `tomllib`）
- Chrome（脚本默认附加到你日常那个 Chrome）
- 依赖：`playwright`、`requests`

两种跑法等价，任选一种：

```powershell
cd D:\my_test\seckill_script

# A. uv —— 脚本头部已用 PEP 723 声明依赖，uv 自动建隔离环境并装依赖，不用手动 pip install
uv run seckill.py

# B. 系统 Python —— 先装一次依赖
pip install -r requirements.txt
python seckill.py
```

后面所有命令（`--check` / `--probe` / `verify.py`）同理，前面加 `uv run` 或 `python` 即可。
注意 `uv run` 在没有 `pyproject.toml` 时会建一个**临时环境**：依赖声明就写在 `seckill.py`
和 `verify.py` 顶部的 `# /// script` 块里（没有它就会 `ModuleNotFoundError: No module named 'requests'`，
实测过）。第一次 `uv run` 要下 playwright（几十 MB），之后走缓存。

## 2. 浏览器模式（二选一）

### 模式 A：附加到日常 Chrome —— 直接复用你已登录的 cookie（默认，推荐）

一次性操作，之后长期有效：

1. 在**日常 Chrome** 的地址栏打开 `chrome://inspect/#remote-debugging`
2. 勾选 **「Allow remote debugging for this browser instance」**
3. 页面上出现 `Server running at: 127.0.0.1:<port>` 即成功

然后验证：

```powershell
python seckill.py --check
```

期望看到：

```
[OK] 调试端点可用：http://127.0.0.1:9222
     Browser   : Chrome/155.0.8059.40
[OK] 浏览器上下文 1 个
     标签页 3 个：
       - https://cloud.tencent.com/act/pro/featured-202607...
     cloud.tencent.com cookie 12 个：[...]
     登录态判定：已登录
```

**为什么非得走这个开关**（都实测过，不是猜）：

- Chrome 136 起，默认 profile 上 `--remote-debugging-port` **不再生效**，官方要求改用非默认
  `--user-data-dir` 或 Chrome for Testing —— 那样就又得重新登录一次。见
  [Chrome 官方博客](https://developer.chrome.com/blog/remote-debugging-port)。
- 也不能"直接把 cookie 文件复制出来"：Chrome 155 对
  `<User Data>\Default\Network\Cookies` 加了**独占锁**，实测 `python open(rb)`、
  `CreateFileW(share=READ)`、`share=READ|WRITE`、`share=ALL`、`share=0` **全部 WinError 32**；
  同一目录下的 `Local State`／`Preferences`／`Login Data` 却能正常读，说明是针对 cookie 的
  刻意保护。加上 Chrome 155 的 App-Bound Encryption（`os_crypt.app_bound_encrypted_key`），
  外部程序即使拿到文件也解不开。

**这个开关打开的实例走的是"新特性模式"，和传统 `--remote-debugging-port` 不一样**：

| | 传统端口模式 | `chrome://inspect` 开关模式 |
| --- | --- | --- |
| `http://127.0.0.1:9222/json/version` | 可用 | **404**（实测） |
| 权威入口 | `/json/version` 里的 `webSocketDebuggerUrl` | `<User Data>\DevToolsActivePort`：第一行端口，第二行 browser 级 ws 路径 |
| 连接方式 | `connect_over_cdp("http://…")` | `connect_over_cdp("ws://127.0.0.1:<port>/devtools/browser/<uuid>")` |

所以脚本不会用 `/json/version` 去"预检"端点是否可用 —— 那会把好端点误判成不可用（我第一版
就踩了这个坑）。它直接读 `DevToolsActivePort` 拿 ws 地址去连，`http://` 只作为老式实例的兜底；
连不上时重读一次该文件（Chrome 重启后 uuid 会变）。

**连接会比较慢，这是正常的**：Playwright 连上后会 auto-attach 到浏览器里**所有** target，
你日常 Chrome 开着二十几个标签页和扩展，它要逐个 `Page.enable`/`Network.enable`/`Runtime.enable`。
实测 15 秒超时不够、25 秒稳定，脚本因此给到 90 秒。命令行会打印实际耗时。

脚本附加时会优先复用你已打开的腾讯云标签页（`reuse_existing_tab = true`）；没有就新开一个。
**注意：它会重新导航那个标签页到活动页**，所以别把正在用的重要页面留在那里。
抢购期间也别关掉那个标签页。

如果重启 Chrome 后 `--check` 又报不出端点，把这个开关再打开一次即可（开关状态会写进
`Default/Preferences`，但重启后是否仍然生效以实测为准）。

### 模式 B：脚本自己登录一份（`mode = "launch"`）

不碰你的日常 Chrome，代价是要单独登录一次：

```powershell
python seckill.py --local --login
```

登录态存在 `./profile`，之后 `--local` 直接复用。

## 3. 校准 / 确认目标卡片（强烈建议先做）

```powershell
python seckill.py --probe
```

它会打印两类卡片并写入 `logs/probe.json`：

- **命中锚点的卡片**（脚本的候选），附上「实际按钮文案」和「已识别/未识别」；
- **视口内未命中锚点的卡片**，用来反过来校验 `card_anchors` 是不是排除了不该选的卡。

本机 2026-10-16 13:05 实测输出：

```
扫到卡片 76 个；锚点 ['轻量应用服务器', '4核4G3M'] 命中 2 张；当前状态文案 ['取消提醒']

命中锚点的卡片 2 张（脚本的候选）：
  cidx=1 rect={'x': 70, 'y': 162, 'w': 460, 'h': 312} inViewport=True
         实际按钮文案='取消提醒' 已识别=取消提醒
         轻量应用服务器 建站、Web应用… 4核4G3M 300GB 月流量 地域 广州/上海/北京 … 0.5折 个人专享
  cidx=2 rect={'x': 70, 'y': 764, 'w': 340, 'h': 394} inViewport=True
         实际按钮文案='立即购买' 已识别=未识别
         轻量应用服务器 4核4G3M 建站、Web应用… 规格 4核4G3M 地域 上海 时长 1年 1.4折

视口内未命中锚点的卡片 8 张（用来人工校准 card_anchors）：
  rect={'x': 550,  'y': 162, …} 按钮='取消提醒' | 优选流量包 … 活动价：0.1 元 日常价：2 元
  rect={'x': 1030, 'y': 162, …} 按钮='取消提醒' | COS 标准存储容量包 … 500GB …
  rect={'x': 430,  'y': 764, …} 按钮='立即购买' | 轻量应用服务器 2核2G3M …
  …

=> 会选中这张：轻量应用服务器 … 4核4G3M 300GB 月流量 …
   按钮 取消提醒 @ (444,449)
```

**怎么读这个结果**：秒杀区那三张卡横排在同一行（y=162），`x` 分别约 70 / 550 / 1030，
对应「轻量应用服务器」/「优选流量包」/「COS 标准存储容量包」。当前配置只命中第一张 ——
因为另两张的卡片文本里没有 `4核4G3M`。这就是 `card_anchors` 的作用：**结构上排除掉点错商品**
（早期版本按按钮文案反查，实测会点到 x=929 的「优选流量包」和 x=1409 的「COS」）。
命中后 `_pick` 再按「有按钮 > 在视口内 > y 最小」选，秒杀卡（y=162）赢过下面的专区卡（y=764）。

定位不对就改 `config.toml` 的 `card_anchors`：填目标卡片**独有**的文本片段（商品名 + 规格最稳），
用 `--probe` 的「未命中」列表确认别的卡确实被排除了。

## 4. 端到端自检

```powershell
python verify.py
```

定位目标卡片 → 检查未开抢时 `tick` 返回 `idle` 且能读到当前按钮文案 → 把按钮文案改成
「立即抢购」模拟开抢 → 发一次真实鼠标事件，验证事件确实落在按钮元素上且
`isTrusted=true`，并顺带测量 20 次 `tick` 的延迟。用捕获阶段监听 + `stopPropagation`
拦截，**不会**触发真实抢购。

在本机日常 Chrome（22 个标签页）上的实测输出：

```
已连上 ws://127.0.0.1:9222/devtools/browser/515f559c-…（耗时 1.3s）
已附加到日常 Chrome，上下文标签页 22 个，腾讯云 cookie 36 个
已定位目标卡片：轻量应用服务器 … 4核4G3M 300GB 月流量 …
  卡片内按钮：text=添加提醒 cls=uno3-buy-card__btn 坐标=(444,449) 可点=True
OK   tick 延迟：中位 2.6ms  最大 7.9ms（20 次；配置间隔 30ms）
OK   未开抢时的 tick：{"state": "idle", "status": ["添加提醒"]}
OK   tick：{"state": "ready", "text": "立即抢购", "x": 444.484375, "y": 449, …}
OK   按钮 rect={'x': 359.98, 'y': 425.5, 'w': 169, 'h': 47}
OK   点击回执：[{"x": 444, "y": 449, "trusted": true}]

全部通过：定位 → 命中 → 坐标点击 → 事件落在按钮且 isTrusted=true
```

几点说明：
- 未开抢时按钮文案是 **「添加提醒」**（10:00 场已过、15:00 场未到），坐标 (444,449)；
  售罄时是「已抢光」，两者都定位得到 —— 脚本不依赖具体文案。
- `tick` 在 22 个标签页同时推网络事件的情况下仍然 2.6ms，30ms 的轮询间隔名不副实不了。
- 结束时只打印「断开与日常 Chrome 的连接」，**你的浏览器不会被关掉**。

## 5. 试跑

```powershell
# 不等到开抢时间，立刻按「可抢购」逻辑走一遍，命中后不点击
python seckill.py --no-wait --dry-run

# 立刻真点一次（非抢购时段一般会点到「已抢光」，只用于确认点击链路）
python seckill.py --no-wait
```

## 6. 正式运行

```powershell
python seckill.py                 # 常驻，今天 10:00、15:00 两场都跑
python seckill.py --once          # 只跑最近一场就退出
python seckill.py --once --at 10:00
```

常驻模式在每场开抢前 180 秒开始预热，跑完一场断开（cdp 模式只断开 CDP 连接，
**不会关掉你的 Chrome**），安静等下一场。日志同时写控制台和 `logs/seckill-YYYYMMDD.log`。

### 用任务计划在固定时间启动

```
程序：  D:\py3.14\python.exe
参数：  seckill.py --once --at 10:00      （另一条改成 --at 15:00）
起始于：D:\my_test\seckill_script
```

> 任务计划场景下如果 `--check` 连不上：Chrome 必须**已经启动并且开过那个开关**，
> 脚本不会替你启动 Chrome。

## 7. 命令行参数

| 参数 | 作用 |
| --- | --- |
| `--check` | 探测 Chrome 调试端点并打印它的登录态（只读，不改任何东西） |
| `--chrome` / `--local` | 临时切换浏览器模式，覆盖 `config.toml` |
| `--cdp-port N` | 手动指定调试端口，插到尝试列表最前面 |
| `--probe` | 打印页面上的候选卡片，用于校准选择器 |
| `--no-wait` | 不等开抢时间，立刻尝试 |
| `--dry-run` | 走完整流程但不点击 |
| `--once` / `--at HH:MM` | 单场模式 |

## 8. 配置说明（`config.toml`）

| 配置 | 作用 |
| --- | --- |
| `[chrome] mode` | `cdp` = 附加日常 Chrome；`launch` = 脚本自己启动 |
| `[chrome] cdp_ports` | 尝试的调试端口，默认 `[9222]`（脚本还会自动读 `DevToolsActivePort`） |
| `[chrome] reuse_existing_tab` | 附加时是否复用已打开的腾讯云标签页 |
| `[target] card_selector` | 卡片容器选择器，实测 `.uno3-buy-card` |
| `[target] card_anchors` | 目标卡片必须**同时**包含的文本 |
| `[target] buy_texts` | 命中即点击的按钮文案，默认 `立即抢购` |
| `[target] ready_texts`/`soldout_texts` | 只用于日志显示状态，不参与点击 |
| `[timing] fire_times` | 每天开抢时刻，默认 `10:00:00` / `15:00:00` |
| `[timing] preload_seconds` | 提前多久预热，默认 180 |
| `[timing] hot_seconds` | 开抢前多少秒进入高频轮询，默认 20 |
| `[timing] hot_interval_ms` | 高频轮询间隔，默认 30ms（约 33 次/秒） |
| `[timing] allow_early_ms` | 允许比开抢时刻提前多少毫秒点击，防误触 |
| `[timing] give_up_seconds` | 开抢后多久还没成功就放弃，默认 90 |
| `[timing] max_click_attempts` | 最多点多少次就放弃，默认 60（原来 20 次在实测里 11.6 秒就耗尽，而放弃时限还留着 90 秒） |
| `[timing] mask_wait_ms` | 等抢购点击后的遮罩退掉多久，默认 1500；超时才改用 JS 直达点击 |
| `[timing] blind_click_ms` | 开抢后多少毫秒内必须发出第一次点击（硬指标），默认 100；`0` = 关闭。到点还没命中「立即抢购」就直接按预热时记下的按钮坐标补点一次 |
| `[browser] keep_open_after` | 抢到后是否留着浏览器给你付款 |

## 9. 工作原理（以及为什么必须这么写）

- **先卡片、后按钮**。活动页 SSR 里的 12 处「立即抢购」全属于顶部导航
  （`uno3-nav2__item-btn`），秒杀卡片的按钮是客户端渲染的。不能按按钮文案反查卡片：
  目标卡处于「已抢光 / 添加提醒」时它的按钮文案根本不在 `buy_texts` 里，反查会退化成
  「全页第一个立即抢购」——**实测那会点到「优选流量包」(x=929) 和「COS」(x=1409)
  的按钮上，买错商品**。正确顺序：先按 `card_selector` 拿卡片容器 → 用 `card_anchors`
  校验 → 再在卡片内找按钮。
- **点按钮本体，不点卡片**。卡片自身也是 `cursor:pointer`，只按 cursor 上溯会一路走到
  整张卡片（459×312），点下去等于点卡片中心。判定可点元素时优先认 `BUTTON/A` 标签和
  class 里带 `btn` 的元素，再用「面积明显小于卡片」兜底。实测该按钮是
  `div.uno3-buy-card__btn`，169×47，中心 (449, 449)。
- **标记 + 高频轮询**。定位成功后给卡片容器打 `data-sk-card`，之后每 30ms 只在这个容器里
  找文本，单次探测 1~3ms。文案变化时元素本身不变（实测改文案前后矩形完全一致），
  所以标记稳定。
- **页面内哨兵（2026-10-20 加的，为了把首次点击压进 100ms）**。Python 侧发现一次要付两份
  固定开销：`tick`（CDP `Runtime.evaluate` 往返，实测 p50 1.4~4.2ms）+ 判断 + 坐标点击
  （`mouse.move/down/up` 三次往返，实测 34ms）。现在进入高频窗口时先往页面里装一个哨兵
  （`JS_ARM`）：`MutationObserver` 盯着目标卡片子树，`requestAnimationFrame` 每帧兜底；
  一旦「立即抢购」落进 DOM，**在同一微任务里**就 `el.click()`，实测从 DOM 改完到点击发出
  **0~1ms**。它同时把「按钮文案变化时刻 / 帧数 / 帧间隔峰值 / 命中时刻」记进
  `window.__skWatch`，日志里的「页面内时间线：『添加提醒』-20s → 『立即抢购』+780ms」就是它。
  命中后 Python 只补一次真实鼠标点击（`isTrusted=true`）并做 `settle` 判定，避免重复提交。
  两个坑：① 窗口被遮挡/最小化时 Chrome 会暂停 rAF（日志里「帧数 0」就是这个），
  但 `MutationObserver` 照常触发，所以仍能命中；② 卡片被框架重建会让 `data-sk-card` 标记
  失效，脚本会重新定位，`attach` 每 500ms 也会重挂一次观察器。
- **抢跑点击**。页面的「立即抢购」可能明显晚于开抢时刻才渲染（2026-10-20 15:00 场：脚本
  到 `+828ms` 才看到它，其中脚本侧开销约 40ms，其余是页面状态/渲染延迟；注意那一次的
  `+828ms` 用的还是旧的 Date 秒级校准，本身可能含几百毫秒噪声，下一场起日志里的
  「开抢 +Xms」才是页面内墙钟口径）。为了守住「开抢后 100ms 内必须发出第一次点击」，
  哨兵到 `blind_click_ms` 还没命中时，脚本先提前 300ms 把鼠标挪到按钮上（省掉 move 往返），
  判定提前 60ms，然后只发 down/up，实测按下时刻落在**开抢 +58~63ms**。
  **代价**：那一击可能打在「添加提醒」上（会弹一次提醒浮层，脚本随后关掉）；若按钮是
  「取消提醒」则跳过（点了会真的退掉提醒）。不想要这个过程就把 `blind_click_ms` 设成 `0`。
- **点击**：CDP 注入鼠标事件（`mouse.move/down/up`）点按钮中心，比 `locator.click()`
  少一轮可见性/可操作检查，也不要求窗口在前台；事件 `isTrusted=true`。
- **遮罩是抢购点击的正常表现，不是障碍（2026-10-16 15:00 场的教训）**。抢购点击之后页面会
  盖一层遮罩（请求进行中），此时 `tick` 报 `obstructed`、按钮文案往往还没变。那一场 20 次
  点击全部落空，就是被这层遮罩带偏的：老实现把它当成「浮层挡住按钮」，遇到就连续 6 轮只关
  浮层、不点击（每轮约 85ms），11.6 秒里只发出 20 次点击，按钮还是「立即抢购」时就放弃了
  —— 而同一张卡 2 分钟后才变成「已抢光」，窗口本来存在。复盘先排除了坐标链路：`verify.py`
  与现场几何 dump 都显示 `tick` 坐标与按钮中心误差 0、顶层元素就是按钮、点击回执
  `isTrusted=true`；hover、进度条层里的同名文本也都不是原因。
  现在的做法：**先等遮罩退**（`mask_wait_ms`，默认 1500ms），退掉再看按钮变成了什么；等不到
  才认定是真浮层，改用 `el.click()` 直达按钮。遮罩的 tag/class 会写进日志（`按钮被 XXX 遮住`）。
- **抓业务拒绝的原因**。点击后按钮若离开「立即抢购」文案，`settle` 会把当前可见的提示文案一起
  打出来（`页面提示：…`）——未开始 / 资格不符 / 已抢光这些原因基本只在那儿闪一下，不抓就永远
  只能看到「按钮仍是立即抢购」这种没信息量的结果。提示里命中 `soldout_texts`（如「已售罄」）
  就直接判本场结束：实测 2026-10-19 10:00 场点下去返回的就是「抱歉，当前商品已售罄」，脚本
  在那儿空点了 21 次、烧满 90 秒才放弃，现在会点掉弹窗的「确认」并立刻收工。
- **点击后的两种结局**。`settle` 区分「按钮仍是『立即抢购』」（`retry`，继续点）和「按钮离开
  了这个文案」（`pending`，说明点击被页面收下了，正在加载/跳转）——后者会多等一会儿再判定，
  避免在跳转过程中连点，那是风控最敏感的信号。老日志里那句「按钮仍是 None」就是加载态，
  当时被误当成「没效果」。
- **跳转会打断 evaluate**。点击成功立刻导航时 `page.evaluate` 会因执行上下文销毁而抛错，
  现在接住它并直接判成功——否则抢到的那一刻脚本自己会崩。
- **时钟**：用 `Date` 响应头**只能做秒级粗校准**。服务器真实时刻落在 `[Date, Date+1s)`，
  观测区间宽度必然 ≥1s，把它当偏移量会把本来已被 w32time/NTP 对齐到几毫秒的系统时钟
  推偏几百毫秒（同一天两次校准得到 `+0.02s` 和 `-0.23s`，全是取整噪声）。现在的做法：
  各样本取观测区间交集，只有整个区间都明显偏离 0（>1.2s）才修正，否则 `offset=0`、
  只打印不确定度。**本机 `w32time` 已与 NTP 对齐（偏差 ~2ms），所以默认就是直接用系统时钟。**
  命中判定本来也不依赖时钟——真正的判据是页面按钮文案。
- **按钮文案的假信号**：实测页面刚导航完的首帧里，目标卡按钮会**短暂显示「立即抢购」**，
  等秒杀接口数据回来才变成真实状态（添加提醒 / 取消提醒 / 已抢光）。这不是 hover 造成的——
  做过对照实验：鼠标在四个不同位置下三张卡的文案完全一致。它也不会造成误点，因为点击有
  `allow_early_ms` 时间闸门（只允许在开抢时刻前 300ms 内点击），而高频轮询在开抢前 20 秒
  才开始、那时页面已预热 180 秒、状态早已稳定。若预热期读到「立即抢购」，脚本会打 warning
  说明原因，便于你判断是不是该商品正在售。
- **按钮文案是单一文本节点**：`<div class="uno3-buy-card__btn">取消提醒</div>`，由 Vue 按状态
  整体替换文本，所以按文本判定是可靠的。

## 10. 排错

| 现象 | 原因 / 处理 |
| --- | --- |
| `--check` 报「没能附加到 Chrome」 | 确认 Chrome 在运行且 `chrome://inspect/#remote-debugging` 的开关是勾上的；`<User Data>\DevToolsActivePort` 应存在 |
| 连接耗时十几秒 | 正常。Chrome 标签页/扩展越多，Playwright 的 auto-attach 越慢，脚本给到 90 秒 |
| `登录态判定：未登录` | 那个 Chrome 里没登录腾讯云，或用的不是这个 profile；先在里面手动打开一次活动页 |
| 换过 Chrome 重启后连不上 | 脚本会自动重读 `DevToolsActivePort`；仍不行就把开关再勾一次 |
| 日志「未能定位到目标卡片」 | 跑 `--probe` 看卡片列表，调 `card_anchors`；`logs/scan-dump.json` 有完整快照 |
| `cardCount: 0` | 页面改版了，用 `--probe` 重新校准 |
| `--probe` 里「实际按钮文案=X，已识别=未识别」 | X 不在 `ready_texts`/`soldout_texts` 里。只影响日志状态显示，不影响点击；想让日志显示就把它加进 `ready_texts` |
| 页面出现滑块/验证码 | 脚本会截图、响铃并停止点击，需要人工过验证 |
| 日志「按钮被 XXX 遮住」 | 抢购点击后的遮罩（请求进行中）。脚本会等 `mask_wait_ms` 再看结果，不在这期间连点；XXX 是遮罩的 tag/class |
| 日志「按钮被 XXX 遮住且 1.5s 没退」 | 遮罩迟迟不退，按真浮层处理，改用 `el.click()` 直达点击 |
| 日志「页面提示：…」 | 点击被业务拒绝时页面的提示原文（未开始/资格不符/已抢光…），排查失败原因先看它 |
| 日志「第 N 次命中『立即抢购』@ … （开抢 +Xms）：页面内点击已发出」 | 页面内哨兵命中了。`+Xms` 是开抢后多少毫秒，直接看它判 100ms 指标；紧跟的「页面内时间线」是按钮文案变化序列 |
| 日志「帧数 0（rAF 被浏览器节流，命中来自 MutationObserver）」 | Chrome 窗口被遮挡/最小化，rAF 暂停。哨兵仍靠 MutationObserver 命中，但建议把窗口露出来 |
| 日志「开抢 +100ms 硬指标：哨兵仍未命中（按钮当前 添加提醒），抢跑点击已发出 @ …（开抢 +63ms）」 | 页面还没把按钮切成「立即抢购」，脚本按 `blind_click_ms` 抢跑点了一次（实测落在开抢 +58~63ms）。嫌它有副作用就把 `blind_click_ms` 设为 0 |
| 日志「按钮是『取消提醒』…跳过抢跑点击」 | 抢跑会真的把已设的提醒退掉，所以这种情况脚本只等哨兵，不抢跑 |
| 日志「页面提示售罄」 | 点下去后端返回的就是售罄文案（实测 2026-10-19 10:00 场：「抱歉，当前商品已售罄」）。脚本点掉弹窗的「确认」后直接结束本场，不再空点到时限 |
| 日志「第 N 次点击有响应…等页面反应」 | 按钮离开了「立即抢购」文案（加载态），说明点击被页面收下了，脚本会等跳转，不会连点 |
| 抢完浏览器被关掉了 | 不该发生：cdp 模式下 `stop()` 只断开 CDP 连接。若真发生请反馈 |
| `--login` 被拒绝 | cdp 模式不需要登录；要登录脚本自己的 profile 请加 `--local` |

## 11. 边界与风险

- 活动规则限制「同一账号限购 1 次、仅产品新用户」，脚本无法也不试图绕过任何资格校验。
- 打开远程调试开关后，本机任何程序都能控制该 Chrome 并读取其数据。这是官方提供的
  开发者功能，用完可以关掉开关。
- 自动化操作可能触发腾讯云风控（滑块验证、订单风控），后果由使用者承担。
- 脚本不支付、不改订单、不调用下单接口，只在页面上点那个按钮。
