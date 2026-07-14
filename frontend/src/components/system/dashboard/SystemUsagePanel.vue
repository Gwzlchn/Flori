<script setup lang="ts">
import { Braces, Coins, RefreshCw } from 'lucide-vue-next'
import { fmtRelative } from '../../../utils/datetime'
import type { PricingStatus, UsageAggregate } from '../../../types'

withDefaults(defineProps<{ usage: UsageAggregate; groups: any[]; pricing?: PricingStatus | null; pricingBusy: boolean }>(), { pricing: null })
defineEmits<{ refreshPricing: []; openPricing: [] }>()
const fmtCost = (value: number) => `$${(value ?? 0).toFixed(4)}`
const costLabel = (provider: string) => provider === 'claude-cli' ? '（等价）' : ''
</script>

<template>
  <div class="card pad" style="margin-bottom:24px">
  <div class="card-h"><Coins :size="15" />AI 用量 · {{ usage.calls }} 次调用</div>
  <div class="grid4" style="margin-bottom:12px"><div class="metric"><div class="v">{{ usage.total_input_tokens.toLocaleString() }}</div><div class="l">输入 token</div></div><div class="metric"><div class="v">{{ usage.total_output_tokens.toLocaleString() }}</div><div class="l">输出 token</div></div><div class="metric"><div class="v">{{ usage.cache_hit_rate_pct }}%</div><div class="l">平均缓存命中</div></div><div class="metric"><div class="v">{{ fmtCost(usage.total_cost_usd) }}</div><div class="l">累计成本</div></div></div>
  <div><template v-for="group in groups" :key="group.provider"><div v-if="group.models.length === 1" class="prov-flat"><span class="badge b-mut">{{ group.provider }}</span><b class="mono">{{ group.models[0].model }}</b><span class="prov-meta">{{ group.calls }} 次 · 入 {{ group.input.toLocaleString() }} / 出 {{ group.output.toLocaleString() }} · 命中 {{ group.hit }}%</span><span class="prov-cost">{{ fmtCost(group.cost) }}<span class="dim">{{ costLabel(group.provider) }}</span></span></div><details v-else class="prov-group"><summary class="prov-sum"><span class="badge b-mut">{{ group.provider }}</span><span class="prov-meta">{{ group.models.length }} 个模型 · {{ group.calls }} 次 · 命中 {{ group.hit }}%</span><span class="prov-cost">{{ fmtCost(group.cost) }}<span class="dim">{{ costLabel(group.provider) }}</span></span></summary><div class="prov-models"><div v-for="model in group.models" :key="model.model" class="prov-row"><b class="mono">{{ model.model }}</b><span class="prov-meta">{{ model.calls }} 次 · 入 {{ model.input_tokens.toLocaleString() }} / 出 {{ model.output_tokens.toLocaleString() }} · 命中 {{ model.cache_hit_rate_pct }}%</span><span class="prov-cost">{{ fmtCost(model.cost_usd) }}</span></div></div></details></template></div>
  <div v-if="pricing" class="pricing-row"><span class="badge b-mut">LiteLLM 价表</span><span class="prov-meta">{{ pricing.model_count }} 模型 · 更新于 {{ pricing.fetched_at ? fmtRelative(pricing.fetched_at) : '从未' }}</span><button class="btn sm" :disabled="pricingBusy" style="margin-left:auto" @click="$emit('refreshPricing')"><RefreshCw :size="12" :class="pricingBusy ? 'spin' : ''" />手动更新</button><button class="btn sm" @click="$emit('openPricing')"><Braces :size="12" />原始 JSON</button></div>
  </div>
</template>

<style scoped>.spin { animation: spin 1s linear infinite; }@keyframes spin { to { transform: rotate(360deg); } }.prov-cost .dim { font-size: 11px; }</style>
