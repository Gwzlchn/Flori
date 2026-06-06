# 文档体系

```
docs/
├── 00-vision.md                    # 愿景：为什么做，不做什么
├── 01-architecture.md              # 系统架构全景图
├── 02-domain-model.md              # 领域模型：集合/视频/笔记/术语/学习路径
├── 03-contracts.md                 # 接口契约：API/Redis消息/文件Schema
├── 04-module-design/               # 各模块详设
│   ├── scheduler.md                # 调度器（资源池+优先级+Worker自取）
│   ├── worker.md                   # Worker（CPU/GPU/AI/Download）
│   ├── api.md                      # API 服务（FastAPI）
│   ├── step-base.md                # StepBase 统一基类
│   ├── steps-video.md              # 视频分析步骤（00-09）
│   ├── steps-paper.md              # 论文分析步骤（未来）
│   ├── steps-article.md            # 文章分析步骤（未来）
│   ├── ai-gateway.md               # AI 网关（多 Provider/路由/对比/成本追踪）
│   ├── knowledge-store.md          # 知识存储（搜索/术语/关联）
│   └── frontend.md                 # 前端（Vue3 页面+组件）
├── 05-content-adapters.md          # 内容适配器：视频/论文/公众号/网页
├── 06-prompt-engineering.md        # Prompt 工程：Profile/记忆/迭代
├── 07-security.md                  # 安全：攻击面/认证/应急
├── 08-deployment.md                # 部署：单机/多机/Docker
├── 09-testing.md                   # 测试：单步验证/集成/端到端
├── 10-observability.md             # 可观测：进度/日志/监控/告警
├── 11-dev-workflow.md              # 开发流程：并行开发/会话交接
├── 12-dependencies.md              # 开源依赖：工具选型/License
├── adr/                            # 架构决策记录
│   ├── README.md
│   ├── 0001-language-python.md
│   ├── 0002-queue-redis.md
│   ├── 0003-storage-local-first.md
│   ├── 0004-llm-multi-provider.md
│   ├── 0005-frontend-vue3.md
│   ├── 0006-gateway-cloudflare-tunnel.md
│   ├── 0007-remote-worker-polling.md
│   └── 0008-search-sqlite-fts5.md
```
