# vLLM 部署笔记

这次在 NAS 上用 Docker 部署 vLLM,主要想测试 PagedAttention 对 throughput 的提升。模型选的是 Qwen2.5-7B-Instruct,量化方式用 AWQ,max_model_len 设成 8192。启动之后先用 OpenAI compatible API 跑了一遍 benchmark,发现 batch size 上去以后 latency 抖动很明显。

排查下来,问题出在 GPU memory utilization 设得太高,KV cache 预留的空间不够,导致 scheduler 频繁 preempt 请求。把 gpu_memory_utilization 从 0.95 调到 0.85 以后,吞吐反而更稳定了。另外 enable_prefix_caching 对多轮对话场景帮助很大,重复的 system prompt 不用每次都重新计算。

下一步打算对比一下 SGLang 和 TensorRT-LLM 在同样硬件上的表现,顺便看看 speculative decoding 能不能进一步降低首 token 延迟。
