# ADR-0011: Worker 运行时编排 —— 暂停/恢复、per-worker 并发、12h 宽限、download→io

> 承接 [ADR-0009](0009-worker-gateway-outbound-https.md)（worker 接入通路）。本 ADR 记录
> worker 接入之后的**运行时编排**实现决策。源于 2026-06-22 用户三问的调研（`.local/processing/2026-06-22/14-*`）
> 与随后的拍板——用户否决了「用宿主 cron 做时段调度 / 保留 download 类型 / per-machine 并发属 YAGNI」
> 的保守建议，要求**完整实现**暂停按钮、异构机器并发、改名。本 ADR 以实现为准。

## 背景

随多机 / 多 GPU 接入，三个运行时需求需要落地：

1. **暂停 / 恢复**：希望在 UI 上按按钮暂停/恢复某个 worker（例：NAS 夜间只跑 io 下载 worker、白天才跑 cpu-bound worker）。
2. **异构机器并发**：不同算力的机器能各自声明最大并发；能接入多台 GPU 机器并真正并行。
3. **类型边界**：`download` 类型路由上等价 `--pools io`，类型名不诚实——并入/改名。

调研（详见决策日志 14）确认：路由只看 pool+tags、池上限是全局单计数器、单 worker 串行执行、
旧 `draining` 与运行时 `status` 复用同一字段导致暂停态被覆盖（3 个 bug）。

## 选项

| 维度 | 选项 | 取舍 |
|------|------|------|
| 暂停机制 | A. 服务端暂停态（独立 `admin_status`，进程留存）**[采纳]** | 对本地+远程 worker 一致生效、不挂 docker.sock；进程留存占少量空闲内存 |
| | B. 挂 docker.sock 真停/启容器 | 彻底释放资源，但只控本地 worker、远程 GPU/网关 worker 够不着，且 docker.sock=宿主 root 等价（违反项目「API 不挂 docker.sock」约定，见 `docker-compose.yml` 注释） |
| 并发 | A. per-worker `--concurrency N`（多条认领循环）**[采纳]** | 异构机器自报容量；全局每池上限仍是天花板 |
| | B. 维持单 worker 串行 | 简单但表达不了异构容量（用户已否决） |
| 多 GPU | 每机一个声明 `--pools gpu cpu` 的 worker + 调 `gpu.limit` **[采纳]** | 无设备亲和（用所有卡，YAGNI） |
| 能力表达 | `download`→`io`，随后收敛为显式 `--pools` 集合 **[采纳]** | 路由原语就是 pool；一台机器可声明多种能力 |
| 宽限 | `NO_WORKER_GRACE_SEC` 默认改 12h **[采纳]** | 暂停某类 worker 后，下载好的 job 等 12h 才 fail-fast |

## 决定

1. **服务端暂停态**：新增独立字段 `admin_status`（Redis hash + DB `workers.admin_status` 列 + `Worker.admin_status`），
   取值 `"" / "paused"`，**只由 `PUT /api/workers/{id}` 写**，运行时 `claim/release/心跳` 永不触碰。
   - `claim_step` 读 `admin_status=="paused"` → 不认领（`shared/runner_ops.py`）。
   - `compute_worker_status` 的管理员叠加位改读 `admin_status`，公共态 `draining`→`paused`（`shared/status.py`）。
   - 前端「排空/取消排空」升级为「暂停/恢复」，store `pause/resume` → `PUT {status:"paused"|"active"}`。
   - 这一拆分修掉旧 `draining` 复用 `status` 字段被 busy-release / gateway 心跳 idle / 重注册 覆盖的 3 个 bug。
   - 选 A 而非 docker.sock：对远程 GPU / 网关 worker 也生效，且守住「API 不挂 docker.sock」的安全约定。

2. **per-worker 并发**：`--concurrency N`（或 env `WORKER_CONCURRENCY`，默认 1）让 worker 起 N 条认领循环
   并发执行（`worker/worker.py` 的 `_claim_loop`）。**全局每池槽位（`pools.yaml` 的 `limit`）仍是系统级天花板**，
   并发度只决定单 worker 的并行上限。异构机器据此自报容量。

3. **多 GPU 机器**：每台一个声明 `--pools gpu cpu` 的 worker；要跨机并行就把 `configs/pools.yaml` 的 `gpu.limit` 调成卡数。
   不做 GPU 设备亲和（用「任意空闲卡」语义；同机多卡/严格 1-job-per-卡属 YAGNI，需要时再用资源槽+`CUDA_VISIBLE_DEVICES`）。

4. **能力改为显式池集合**：历史交付先把 `download` 改名为 `io`，当前 CLI 进一步移除单一
   `--type` 映射，统一由 `--pools io|cpu|ai|gpu ...` 声明一个或多个能力；路由只认 pool + tags。

5. **无-worker 宽限默认 12h**：`scheduler` 的 `NO_WORKER_GRACE_SEC` 在 `docker-compose.yml` 默认 `43200`。
   被暂停的 worker 在 `_pool_has_workers` 算「无可用」→ 只剩它服务的池里就绪步等 12h 才 fail-fast。

## 理由

1. 暂停态拆字段是把「管理员意图」与「运行时态」解耦的最小正确改动，一次修掉 3 个 bug，且天然适配本地+远程。
2. 容器即并发单元 + per-worker 并发度，既保留「全局池上限保护共享资源」又让异构机器表达自身容量，不引入线程复杂度。
3. 多 GPU 用「加 worker + 调 limit」满足当前需求；设备亲和成本高、收益低，留到真多卡并行再做。
4. 类型名 `io` 诚实指代池，消除「download 是路由原语」的误解（实际只是 `[io]` 默认）。
5. 12h 宽限把暂停/夜间运维窗口内的 job 从「90s 被误杀」改为「等候到次日」，与暂停按钮配套。

## 与其它 ADR 的关系

- 承接 [ADR-0009](0009-worker-gateway-outbound-https.md)：0009 定 worker **怎么接入**，本 ADR 定接入后**怎么编排运行**。
- 与 [ADR-0002](0002-queue-redis.md) 一致：并发/暂停均复用 Redis（计数器 + hash 字段），不引入新组件。

## 影响

- 契约变更（同提交 `contract:`）：`docs/03-contracts.md` worker hash 增 `admin_status`、公共态 `draining`→`paused`、
  type 枚举 `download`→`io`、heartbeat 回发 `{"paused": bool}`、PUT 暂停/恢复示例。
- DB：`workers` 表加 `admin_status` 列（经 `_EXPECTED_COLUMNS` 平滑迁移，旧库自动 ALTER ADD，默认 `''`）。
- 部署：`docker-compose.yml` scheduler 默认 `NO_WORKER_GRACE_SEC=43200`；`.local` 活栈 uptest 对齐 43200；
  worker 启动命令统一使用 `python -m worker.main --pools ...`。多副本仍须各设独立 `WORKER_NAME`。
- 兼容：旧 `status="draining"` 持久值无害（不再作叠加源）；已发 per-worker token 不受影响。
- 仍 YAGNI（留待后续）：GPU 设备亲和 / 1-job-per-物理卡、并发态下 `current_job` 仅展示代表性单值（busy/idle 有短暂抖动，非正确性问题）。
