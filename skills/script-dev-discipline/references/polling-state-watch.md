# 轮询式状态巡检脚本：硬化骨架

适用于「cron 每 N 分钟拉一次设备状态 → 判事件 → 推卡」这类脚本（车况、围栏、传感器、水位）。
核心问题不是判定逻辑写错，而是**输入本身可能陈旧、单帧快照含歧义、进程可能重叠**。

---

## 1. 可注入入口（让测试不碰生产）

```python
STATE = os.environ.get("WATCH_STATE") or "/opt/data/state/watch.json"
DRYRUN = os.environ.get("WATCH_DRYRUN") == "1"

def main(data=None, now=None):
    d = fetch() if data is None else data
    now_ts = float(time.time() if now is None else now)
    ...
```

- `data` 注入快照，`now` 注入时钟 —— 否则只能靠改系统时间或等真实定时，测不出「静置 4 分钟」。
- `STATE` / 地点表 / 数据源全部走环境变量覆盖，测试指临时目录。
- 测试脚本在 `import` **之前**设好环境变量（模块级常量在 import 时求值）。
- `LEGACY_*` 迁移路径也要能被测试置空，否则首跑会吞掉旧基线、测不出「首次建基线」分支。

---

## 2. 新鲜度闸门

```python
def parse_device_ts(d):
    """设备上报时刻；缺失/格式错 → None（= 不可信，调用方不放行）"""
    try:
        return datetime.strptime(str(d.get("terminalTime") or ""), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None

GATE_SECONDS = 10 * 60        # 事件判定粒度
HARD_SKIP_SECONDS = 6 * 3600  # 完全陈旧：连基线都不动

car_ts = parse_device_ts(d)
if car_ts is not None and now_ts - car_ts > HARD_SKIP_SECONDS:
    sys.exit(0)

gate_ok = car_ts is not None and now_ts - car_ts <= GATE_SECONDS
if first or not gate_ok:
    state["watch"] = snapshot          # 只更新基线
    state["last_move_ts"] = last_move_ts
    save(STATE, state)
    sys.exit(0)                        # 不生成事件、不判定、不推送
```

要点：
- `parse` 失败返回 `None` → 闸门不放行。**不要** `except: pass` 让 lag 留在 0。
- 闸门内仍要更新「绝对状态基线」（锁/窗/位置的当前值），否则下次可信时会把隔夜变化当成新事件。
- **不要**在闸门内更新「事件历史基线」（如围栏在/不在），保留上次可信值，等数据新鲜时再比对。

---

## 3. 延迟确认（对立语义裁决）

```python
LOCK_CONFIRM_SECONDS = 4 * 60
DRIVING_SPEED = 3          # 单向硬证：>0 绝不可能是停车
ODO_EPS = 0.1

if locked_now:
    if flow.get("lock_pending") is None:
        flow["lock_pending"] = car_ts
        flow["lock_odo"] = odo_now            # 记基线

    odo_since = (odo_now - flow["lock_odo"]) if (odo_now is not None and flow["lock_odo"] is not None) else None
    still_moving = (odo_since is not None and odo_since >= ODO_EPS) \
                   or (speed_now is not None and speed_now > DRIVING_SPEED)

    if still_moving:                          # 过程内事件：撤销挂起
        flow["lock_pending"] = None
        flow["lock_odo"] = None
    elif car_ts - flow["lock_pending"] >= LOCK_CONFIRM_SECONDS:   # 终态：确认
        seal("已闭锁")
else:
    flow["lock_pending"] = None               # 解锁 = 撤销
```

要点：
- 挂起时记的是**基线值**，不是订阅事件本身；裁决靠「基线之后有没有继续变化」。
- 快速通道只接受单向硬证（假阳性不可能的信号）。可逆信号一律走确认轮。
- 判据缺失时降级为「静置满阈值」单一判据，并在卡面/日志写明降级，别装作判据齐全。

---

## 4. 防重叠

```python
lock_fd = os.open(os.path.join(os.path.dirname(STATE), ".watch.lock"), os.O_CREAT | os.O_RDWR, 0o600)
try:
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    sys.exit(0)          # 上一轮还在跑，静默退出
try:
    ...
finally:
    fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)
```

---

## 5. 场景注入测试模板

```python
TMP = tempfile.mkdtemp(prefix="watch-test-")
os.environ["WATCH_DRYRUN"] = "1"
os.environ["WATCH_STATE"] = os.path.join(TMP, "state.json")
yourmod.LEGACY_WATCH = os.path.join(TMP, "none.json")

BASE = dict(...)          # 一整份合法快照

def run(dt, now=None, **kw):
    d = dict(BASE); d.update(kw); d["terminalTime"] = dt
    car_ts = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S").timestamp()
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            yourmod.main(data=d, now=car_ts + 30 if now is None else now)
    except SystemExit:
        pass
    return buf.getvalue()
```

覆盖清单（每类至少一条，全部断言 stdout 与 state）：

| 类 | 场景 |
|---|---|
| 首次 | 无基线 → 只写状态、零输出 |
| 闸门 | 滞后超阈值 / 时间戳缺失 / 格式错 → 零输出、会话不变 |
| 开卡 | 触发事件 → 建会话、零输出 |
| 过程内事件 | 增量继续增长 → 撤销挂起、不封板 |
| 终态事件 | 静置满阈值 → 封板、卡片逐行核对 |
| 撤销 | 挂起期间反向事件 → pending 清空、会话存活 |
| 兜底 | 超时封板 / 停留提醒 |
| 陈旧伪造 | 隔夜快照批量变化 → 零事件 |
| 并发 | 外部持有锁 → 静默 exit 0 |

最后再跑一次**真链路 DRYRUN**（真实数据源 + 真实状态文件副本），确认闸门在生产数据上确实按预期放行/拦截，并检查写回的时间戳是设备上报时刻而不是运行时刻。
