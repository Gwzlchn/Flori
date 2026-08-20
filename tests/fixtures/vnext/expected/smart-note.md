# 智能笔记

## 来源事实

- Source 触发 Pipeline Job。[00:00-00:01]
- Task 的可发布输出受声明约束。[00:01-00:02]
- 成功发布轮换 current 和 previous。[00:02-00:03]

## AI 分析

声明式 Artifact 缩小了发布边界；双版本指针让回滚语义保持简单。这里是分析，不冒充原视频表述。

## 关键名词

- Pipeline Job: 一次固化 Pipeline 和 Prompt 后的完整执行。
- Artifact: Task 声明并由服务端校验提交的不可变输出。
