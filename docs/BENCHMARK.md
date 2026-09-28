# 性能基准报告

> 复现命令见文末。数据为**开发机单机实测**（Windows 10，Python 3.12，SQLite WAL，
> runserver 单进程 16 线程闭环），非生产环境数据——用于说明量级与瓶颈形态。
> 生产部署（PostgreSQL + gunicorn 多进程）吞吐上限更高。

## 测试方法

- 工具：`scripts/loadtest.py`（纯标准库，每线程一条 HTTP keep-alive 连接闭环压测）；
- 场景 1：`GET /api/health/` 公开探活——轻读路径，反映框架 + 全部中间件的基础开销；
- 场景 2：`POST /api/ingest/metrics/` 令牌上报——写路径，含令牌鉴权、JSON 校验、
  非法值过滤、数据库写入；
- 场景 3：`GET /forum/optimized/` 业务页面（带登录会话）——**唯一会走 APM 采集中间件
  的路径**（`/api/`、`/monitor/` 等平台自身路径在 `SKIP_PATHS` 里不采集），
  用于评估请求指标的落库方式对请求延迟的影响；
- 每场景 16 线程 × 20 秒。

## 场景 1 / 2：接入链路

| 场景 | QPS | p50 | p95 | p99 | 错误率 |
|---|---|---|---|---|---|
| GET /api/health/（读） | **1266** | 12.0 ms | 15.6 ms | 43.1 ms | 0% |
| POST /api/ingest/metrics/（写） | **445** | 5.1 ms | 74.9 ms | 787.3 ms | 0% |

1. **限速器是写路径的第一道天花板（by design）**：默认配置（120 次/分钟/IP）下
   16 线程压上报接口会在 ~2 秒内打满配额，此后返回 429——同一轮实测 49.66% 请求被拦截，
   证明限速器在真实并发下可靠生效。写路径吞吐基准需 `OBS_RATE_LIMIT_SCALE=1000` 放宽后测量。
2. **写路径 p99 长尾来自 SQLite 单写者锁**：p50 5.1ms 而 p99 787ms，形态符合
   "多线程争抢单条写锁、超时排队"。切 PostgreSQL（`OBS_DATABASE_URL`）后可消除。

这两个场景不写 `RequestMetric`，因此不受下面那项改造影响。

## 场景 3：请求指标"逐条同步写"vs"批量缓冲写"

改造背景：中间件原先在**响应返回前**同步 `INSERT` 一行 `RequestMetric`，
每个请求都参与一次单写者锁竞争；现在改成入队 + 后台线程 `bulk_create` 批量落库
（`monitor/buffer.py`），可用 `OBS_METRIC_BUFFER=0` 关闭以复现旧形态做对照。

单位：QPS 次/秒，延迟 ms。`OBS_METRIC_BUFFER=0` = 旧的逐条同步写。

| 形态 | QPS | p50 | p95 | p99 | max | 均值(=16/QPS) |
|---|---|---|---|---|---|---|
| 逐条同步写 | 74 | 91.6 | 957.7 | 2190.3 | 5498.5 | 216 |
| 逐条同步写（复测） | 73 | 96.0 | 962.4 | 2144.4 | 4550.8 | 219 |
| 批量缓冲 200 条 / 1s（默认） | **91** | 173.3 | **214.3** | **251.0** | **644.0** | **176** |
| 批量缓冲 50 条 / 0.2s | 89 | 176.5 | 215.8 | 235.0 | 661.0 | 180 |
| 批量缓冲 50 条 / 1s | 81 | 188.5 | 265.3 | 316.2 | 672.8 | 198 |

### 结论：换掉了长尾，代价是中位数看起来变差——而后者是测量假象

1. **尾部塌了**：p99 2190ms → 251ms（降 8.7 倍），max 5498ms → 644ms，QPS +23%，
   两轮同步写的结果几乎一致（73/74 QPS），说明不是偶发波动。
2. **p50 从 92ms 抬到 176ms，但这不是服务变慢**。闭环压测里并发固定在 16 线程，
   均值 = 16/QPS：同步形态 216ms、缓冲形态 176ms——**均值是改善的**。
   同步形态的中位数"好看"来自两个机制：
   - 延迟分布重尾：一半请求 ~90ms 就返回，另有百分之几要等 1~5.5 秒，
     均值被拖到 216ms，中位数却只反映快的那一半；
   - 卡在 SQLite 写锁上的线程会释放 GIL，反而让没卡住的线程跑得更快。
3. **缓冲形态下 p50 ≈ 均值（173 ≈ 176）**，说明请求延迟几乎全部来自单进程 GIL 排队，
   而不是写锁——瓶颈从"I/O 争抢"变回"CPU 串行"，这正是想要的形态：
   延迟可预测，扩容靠加进程而不是调批大小。
4. **批大小不是关键变量**：200/1s 与 50/0.2s 结果基本相同（QPS 91 vs 89），
   50/1s 最差（写库次数最多）。因此默认取 200/1s。

### 缓冲区自身的代价（实测核对）

按"落库行数 vs 请求数"对账，硬杀进程（`taskkill /F`，不走 atexit）时的丢点数：

| 轮次 | 请求数 | 落库行数 | 丢弃 | 上界 |
|---|---|---|---|---|
| 200 条 / 1s | 1821 | 1742 | 79 | ≤ 批量上限 200 |
| 50 条 / 0.2s | 1797 | 1782 | 15 | ≤ 50 |
| 50 条 / 1s | 1622 | 1600 | 22 | ≤ 50 |

即**丢点数量被批量上限约束**，且只发生在进程被强杀时；正常退出（`atexit → shutdown()`）
会先把队列排空，`monitor/tests.py::MetricBufferThreadTests` 用 30 秒刷新周期把这条
承诺钉成回归测试。丢弃与积压计数经 `/metrics` 暴露
（`obs_metric_buffer_dropped_total` / `obs_metric_buffer_pending`），采集不会静默丢数据。

调参环境变量：`OBS_METRIC_BUFFER`（0 关闭）、`OBS_METRIC_BUFFER_BATCH`、
`OBS_METRIC_BUFFER_FLUSH_SEC`、`OBS_METRIC_BUFFER_QUEUE_SIZE`。

## 复现

```bash
python manage.py runserver 127.0.0.1:8015 --noreload        # 场景 3（默认批量缓冲）
OBS_METRIC_BUFFER=0 python manage.py runserver 127.0.0.1:8015 --noreload    # 场景 3 对照组
OBS_DISABLE_WORKERS=1 一并加上可排除采集/告警线程的干扰

# 场景 3：业务页面（走 APM 采集中间件，账号取自 demo_credentials.txt）
python scripts/loadtest.py --url http://127.0.0.1:8015 --threads 16 --seconds 20 --scenario page
# 场景 1/2
python scripts/loadtest.py --url http://127.0.0.1:8015 --threads 16 --seconds 20 --scenario health
OBS_RATE_LIMIT_SCALE=1000 python manage.py runserver 127.0.0.1:8015 --noreload
python scripts/loadtest.py --url http://127.0.0.1:8015 --threads 16 --seconds 20 \
    --scenario ingest --token "$(cat ingest_token.txt)"
```
