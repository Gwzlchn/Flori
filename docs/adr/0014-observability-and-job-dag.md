# ADR-0014: 可观测体系 + 每个 job 的流水线 DAG（纯 CSS/SVG,pipelines.yaml 单一事实源）

## 背景

系统是多 worker、多池、多步骤的分布式流水线，但运维和使用时缺乏「全局健康 + 单任务处理过程 + 成本/流量/存储」的可视化：

- 看不清各组件版本/健康、各池吞吐、AI 花了多少、网关中转了多少流量、对象存储用了多少、最近发生了什么。
- 单个 job 的处理过程只有一条扁平步骤列表，看不出步骤间的依赖（哪些并行、哪些汇入），也看不出每步是 cpu 还是 ai、ai 步花了多少。

需要一套可观测能力，且**不引入重前端依赖**（项目约定：纯 Vue + CSS，不上图布局库）。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| 接 Grafana/Prometheus 等外部观测栈 | 功能全 | 重、跑偏「个人工具」定位、运维成本高 |
| 前端引图布局库（dagre/cytoscape 等）画 DAG | 现成 | 增重依赖、与纯 CSS 风格不符 |
| **后端聚合 `/api/status` 等只读接口 + 前端纯 CSS/SVG 自绘** | 轻、零新依赖、契合定位 | 自绘 DAG/采集需自己写 |

## 决定

**后端聚合只读接口 + 前端纯 CSS/SVG 自绘**，分两块：

### 1. `/system` 健康总览页（B 档采集）
`/api/status` 聚合：组件（api/scheduler/redis/minio）版本+健康、四段吞吐、AI 用量聚合（按 provider/model、命中率、等价开销）、LiteLLM 价表状态（模型数+更新时间+手动更新）、**网关中转流量**、**MinIO 容量**；`/api/events` 透出 scheduler emit 的系统事件流（`events:system`）。前端 6 区健康总览。

### 2. 每个 job 的流水线 DAG（`PipelineDag.vue`,纯 CSS + SVG）
- **分层拓扑**：按 `needs` 最长路径分层 → 横向列（同列=可并行），`justify-content:space-between` 铺满宽。
- **SVG 贝塞尔连线**画依赖（源步右缘→目标步左缘），选中步的边高亮（不再用「⟵合」文字注记）。
- **节点信息**：左边框色=池（io/cpu 灰、ai 蓝、gpu 琥珀，不占宽）；圆点=状态着色（done/running/ready/failed/skipped/waiting）；AI 步附 provider+开销（claude-cli 标等价），头部「AI 总开销」。
- **DAG 当步骤选择器**：点节点=选该步，下方全宽显该步产物/日志；从选中步重跑。

### 3. `configs/pipelines.yaml` 单一事实源
步骤的 `needs`（GitLab-CI 风格）经 `shared/config.py` 归一化为 `depends_on`，由 `/api/pipelines` 透出 `{name, steps:[{key,label,pool,needs}]}`，前端 About 流水线列表与 job DAG 都据此动态渲染，**不再硬编码**（修过「列表与实际不符」）。

## 理由

1. 轻、零新前端依赖，契合「个人工具、纯 Vue+CSS」定位；DAG 用 CSS 列 + SVG 连线即可，无需图布局库。
2. 后端聚合成只读接口，前端无状态拉取即可，故障隔离好。
3. `pipelines.yaml` 单一事实源，杜绝前端硬编码与实际流水线漂移。
4. DAG 兼作步骤选择器，一处既看依赖关系又能下钻产物/重跑，信息密度高。

## 后果

- 采集逻辑（流量/容量/版本/事件）散在 `api/*_store.py` + `scheduler` emit + `shared/storage.py`，新增观测项需对应加采集。
- 改任何对外接口须同步 `docs/03-contracts.md`（`contract:` 前缀）。
- 相关：[[0013-version-semver-build-sha]]（版本展示）、边缘部署见 `.local/ops/edge-frontend-deploy.txt`。
