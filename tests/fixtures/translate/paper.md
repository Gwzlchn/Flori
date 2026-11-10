# Sparse Mixture of Experts at Scale

## 1 Introduction

We study sparse models $f(x) = \sum_i g_i(x) E_i(x)$ with `top_k` routing, following https://example.org/moe and prior work [3, 5–7]. Each token is sent to a small number of experts, so the compute per token stays constant while the total number of parameters grows with the number of experts.

$$
y = \sum_{i=1}^{n} G(x)_i E_i(x)

G(x) = \mathrm{Softmax}(\mathrm{TopK}(x W_g))
$$

```python
def route(x, k=2):

    logits = x @ W_g
    return topk(logits, k)
```

| Model | Active | Total |
|---|---|---|
| Dense 13B | 13B | 13B |
| Sparse 8x7B | 13B | 47B |

![](assets/fig1.png)

Results in Table 1 show that the router stays balanced across experts, and the sparse model matches the dense baseline while using the same amount of compute per token. Costs such as \$5 per hour are escaped.

## References

[1] A. Author. Outrageously large neural networks. 2017.

[2] B. Author. Switch transformers. 2021.

## A Training details

We train with a context length of 32k tokens and a batch size of 4M tokens for all models.
