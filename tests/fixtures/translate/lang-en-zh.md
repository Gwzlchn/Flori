# DeepSeek-V2: A Strong, Economical, and Efficient Mixture-of-Experts Language Model

## 4 Alignment

We collect 1.5M instances of instruction tuning data in both English and Chinese, covering helpfulness and safety. The helpful data contains 1.2M instances, with 31.2% for general language tasks, 46.6% for mathematical problems, and 22.2% for coding exercises. The safety data contains 300K instances, covering various sensitive topics.

Below is an example prompt from the Chinese subset together with the response of the chat model, which shows that the model follows the requested format closely.

> 用户:请用三句话解释什么是混合专家模型。
> 助手:混合专家模型由多个专家网络和一个路由器组成。路由器为每个词元选择少数几个专家。这样可以在不增加计算量的情况下扩大参数规模。

During reinforcement learning, we adopt Group Relative Policy Optimization, which foregoes the critic model that is typically the same size as the policy model, and estimates the baseline from group scores instead. We find that the reinforcement learning stage further improves performance on open-ended conversation benchmarks while keeping the scores on standard benchmarks almost unchanged.
