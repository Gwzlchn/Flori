# ADR-0004: 多 Provider AI 网关

## 背景

Pipeline 中多个步骤需要 LLM（标点/笔记/评审），前端需要交互式 AI（问答/术语解释）。

不同任务对模型能力需求差异大（标点用不上 Opus），且用户希望对比多个 Provider 的生成效果。需要一个统一的 AI 调用层。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| 单一 Claude CLI | 有订阅就不额外花钱 | CLI 订阅有额度限制，不支持多 Provider |
| 单一 Anthropic API | 质量最高 | 成本高，单点依赖 |
| **多 Provider 网关** | 按任务分级、成本灵活、可对比 | 实现复杂度稍高 |
| LiteLLM 代理 | 现成的多 Provider 统一层 | 多一个外部依赖，定制能力有限 |

## 决定

自建多 Provider AI 网关，支持三种接入方式：

1. **API Key**：Anthropic/OpenAI/Google/DeepSeek 等（按 token 计费，无限制）
2. **CLI 订阅**：Claude CLI / Gemini CLI（订阅额度内免费，超出自动降级到 API）
3. **本地模型**：Ollama / vLLM（免费，需 GPU）

用户按自己的情况配置——有订阅用 CLI，有 API key 用 API，有 GPU 用本地，可以混搭。

## 理由

1. **灵活接入**：不绑死任何一个 Provider 或付费方式
2. **按任务分级**：标点用便宜模型，笔记用强模型
3. **多输出对比**：同一步骤可配置多个 Provider 并行生成，用户选最好的
4. **前端复用**：笔记问答、术语解释走同一个网关
5. **降级容灾**：CLI 额度用完 → 自动切 API；API 挂了 → 切备选 Provider
6. **成本追踪**：每次调用记录 token 和费用

## 影响

- 新增模块 `ai-gateway`（详见 [04/ai-gateway.md](../04-module-design/ai-gateway.md)）
- StepBase 的 `run_claude()` 改为 `call_ai()`，步骤不感知具体 Provider
- 新增 `providers.yaml` 配置（按需配置 API key / CLI 路径 / 本地地址）
- `pipelines.yaml` 每步增加 `ai` 配置段（primary/fallback/compare）
- 新增 `ai_usage` 表追踪成本
- 使用 CLI 订阅的 Worker 仍需挂载对应 CLI 二进制和凭证
