<script setup lang="ts">
import { computed, inject, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowLeft, Check, ClipboardCopy, DatabaseBackup, HardDrive, RefreshCw,
  RotateCcw, ShieldAlert, Video,
} from 'lucide-vue-next'
import { useApi } from '../composables/useApi'

interface Completeness {
  terminal_steps: number
  manifests_seen: number
  manifests_missing: number
  manifests_excluded: number
  media_self_contained: boolean
  external_media_roots: string[]
  portable_ready: boolean
  readiness_reasons: string[]
}
interface Snapshot {
  digest: string
  refs: string[]
  created_at: string | null
  source_app_version: string
  partial: boolean
  portable_ready: boolean
  readiness_reasons: string[]
  completeness: Completeness
  stats: Record<string, any>
}
interface BackupOperation {
  id: string
  status: 'queued' | 'running' | 'success' | 'failed' | 'interrupted'
  created_at: string
  finished_at: string | null
  vendor_media: boolean
  full_rehash: boolean
  snapshot_digest: string | null
  stats: Record<string, any>
  error: string | null
}
interface RecoveryStatus {
  state: 'empty' | 'ready' | 'incomplete' | 'locked' | 'error'
  repository_path: string
  host_repository_env: string
  write_lock: { owner: string | null; acquired_at: string | null } | null
  latest: Snapshot | null
  snapshots: Snapshot[]
  media_vendoring_available: boolean
  deployment_id_configured: boolean
  online_restore_supported: boolean
  operations: BackupOperation[]
  error: string | null
}
interface RestorePlan {
  id: string
  target_generation: string
  snapshot_digest: string
  plan_digest: string
  deployment_id: string
  generated_at: string
  counts: Record<string, number | null>
  bytes_to_write: number
  required_source_roots: string[]
  commands: { verify: string; exact_dr: string; plan: string; restore: string }
  reused: boolean
}

const api = useApi()
const router = useRouter()
const showToast = inject<(message: string, type?: 'success' | 'error' | 'info') => void>('showToast', () => {})
const status = ref<RecoveryStatus | null>(null)
const loading = ref(true)
const error = ref('')
const starting = ref(false)
const preparing = ref(false)
const vendorMedia = ref(false)
const fullRehash = ref(false)
const selectedDigest = ref('')
const handoff = ref<RestorePlan | null>(null)
const confirmText = ref('')
let pollTimer: ReturnType<typeof setTimeout> | null = null
let disposed = false

const hasActiveBackup = computed(() => status.value?.operations.some(
  operation => operation.status === 'queued' || operation.status === 'running',
) ?? false)
const latest = computed(() => status.value?.latest ?? null)
const canPrepareRestore = computed(() => Boolean(
  selectedDigest.value
  && status.value?.deployment_id_configured
  && status.value?.snapshots.find(item => item.digest === selectedDigest.value)?.portable_ready
  && confirmText.value === '准备还原',
))

function formatBytes(value: number | undefined): string {
  if (!value) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let amount = value
  let index = 0
  while (amount >= 1024 && index < units.length - 1) { amount /= 1024; index += 1 }
  return `${amount >= 10 || index === 0 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`
}
function shortDigest(value: string | null | undefined): string {
  return value ? `${value.slice(0, 15)}…${value.slice(-8)}` : '—'
}
function formatTime(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—'
}
function statusLabel(value: RecoveryStatus['state'] | BackupOperation['status']): string {
  return ({
    empty: '尚无备份', ready: '可恢复', incomplete: '备份不完整', locked: '仓库被占用', error: '异常',
    queued: '等待中', running: '备份中', success: '成功', failed: '失败', interrupted: '已中断',
  } as Record<string, string>)[value] || value
}

async function load(silent = false) {
  if (!silent) loading.value = true
  error.value = ''
  try {
    const value = await api.get<RecoveryStatus>('/api/recovery')
    if (disposed) return
    status.value = value
    if (!selectedDigest.value && value.latest) selectedDigest.value = value.latest.digest
    if (value.operations.some(item => item.status === 'queued' || item.status === 'running')) {
      schedulePoll()
    }
  } catch (e: any) {
    if (!disposed) error.value = e?.message || '读取备份状态失败'
  } finally {
    if (!disposed) loading.value = false
  }
}
function schedulePoll() {
  if (pollTimer || disposed) return
  pollTimer = setTimeout(async () => {
    pollTimer = null
    await load(true)
  }, 2000)
}
async function startBackup() {
  starting.value = true
  handoff.value = null
  try {
    await api.post('/api/recovery/backups', {
      vendor_media: vendorMedia.value,
      full_rehash: fullRehash.value,
    })
    showToast('备份已开始', 'success')
    await load(true)
    schedulePoll()
  } catch (e: any) {
    showToast(e?.message || '备份启动失败', 'error')
  } finally {
    starting.value = false
  }
}
async function prepareRestore() {
  if (!canPrepareRestore.value) return
  preparing.value = true
  try {
    handoff.value = await api.post<RestorePlan>('/api/recovery/restore-plans', {
      snapshot_digest: selectedDigest.value,
    })
    showToast(handoff.value.reused ? '已复用相同恢复交接' : '恢复交接已生成', 'success')
  } catch (e: any) {
    showToast(e?.message || '恢复预检失败', 'error')
  } finally {
    preparing.value = false
  }
}
async function copy(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    showToast('命令已复制', 'success')
  } catch {
    showToast('复制失败,请手动选择', 'error')
  }
}

onMounted(() => load())
onBeforeUnmount(() => {
  disposed = true
  if (pollTimer) clearTimeout(pollTimer)
})
</script>

<template>
  <section class="page recovery-page">
    <button class="back-link" @click="router.push('/settings')"><ArrowLeft :size="15" />返回设置</button>
    <div class="page-title"><DatabaseBackup :size="20" /><div><h1>备份与还原</h1><p>保留有效产物与审计事实,清库后按当前代码重建。</p></div></div>

    <div v-if="loading" class="muted">读取备份仓库…</div>
    <div v-else-if="error" class="card pad error-state">
      <span>{{ error }}</span><button class="btn sm" @click="load()">重试</button>
    </div>

    <template v-else-if="status">
      <div class="card pad recovery-card">
        <div class="card-head">
          <div><div class="card-h"><HardDrive :size="15" />便携内容仓库</div><p>只收入成功 manifest 证明的产物;失败步骤仅保留元信息。</p></div>
          <span class="state-pill" :class="`recovery-state-${status.state}`">{{ statusLabel(status.state) }}</span>
        </div>
        <div class="facts">
          <div><span>容器路径</span><code>{{ status.repository_path }}</code></div>
          <div><span>最新快照</span><code>{{ shortDigest(latest?.digest) }}</code></div>
          <div><span>创建时间</span><b>{{ formatTime(latest?.created_at) }}</b></div>
          <div><span>Job / Part</span><b>{{ latest?.stats.jobs ?? 0 }} / {{ latest?.stats.parts ?? 0 }}</b></div>
        </div>
        <div class="notice warn"><ShieldAlert :size="15" />宿主目录由 {{ status.host_repository_env }} 指定。CAS去重不等于第二份物理备份,仍需NAS快照或复制到另一块盘。</div>
        <div v-if="status.error" class="notice danger"><ShieldAlert :size="15" />{{ status.error }}</div>
        <div v-if="status.write_lock" class="notice danger">
          <ShieldAlert :size="15" />写锁由 {{ status.write_lock.owner || 'unknown' }} 持有。不要自动破锁,先确认原进程已退出。
        </div>
        <div v-if="latest && !latest.portable_ready" class="notice warn">
          <ShieldAlert :size="15" />当前快照不可用于清库恢复: {{ latest.readiness_reasons.join(', ') || '完整性声明缺失' }}
        </div>

        <div class="video-box">
          <Video :size="18" />
          <div><b>原视频</b><p v-if="latest?.completeness.media_self_contained">已收入CAS,恢复不依赖原下载目录。</p><p v-else>默认仍引用NAS原目录,不会重复下载;勾选下方选项才把原视频也复制进CAS。</p></div>
          <span>{{ latest?.stats.vendored_source_parts ?? 0 }} 个已归档</span>
        </div>

        <div class="options">
          <label><input v-model="vendorMedia" type="checkbox" :disabled="!status.media_vendoring_available" />把NAS原视频收入仓库</label>
          <small v-if="!status.media_vendoring_available">需先配置可读的受控视频源目录</small>
          <label><input v-model="fullRehash" type="checkbox" />同时重读全部二进制CAS(慢)</label>
        </div>
        <div class="actions">
          <button class="btn primary" :disabled="starting || hasActiveBackup || status.state === 'locked' || status.state === 'error'" @click="startBackup">
            <RefreshCw :size="14" :class="{ spin: starting || hasActiveBackup }" />{{ hasActiveBackup ? '备份进行中' : '创建增量备份' }}
          </button>
          <span>相同内容按摘要复用,不会复制第二份;失败不推进 latest。</span>
        </div>
      </div>

      <div class="card pad recovery-card">
        <div class="card-head"><div><div class="card-h"><RotateCcw :size="15" />清库还原</div><p>在线页面只生成恢复计划。真正还原必须在API、Scheduler、MCP与全部Worker停写后执行。</p></div></div>
        <div class="notice danger"><ShieldAlert :size="15" />portable 不是回滚。执行前必须先创建并校验 exact DR;本页不会直接清空正在使用的数据库。</div>
        <div v-if="!status.deployment_id_configured" class="notice warn">先在部署环境配置稳定的 FLORI_DEPLOYMENT_ID,才能生成恢复交接。</div>
        <div class="restore-form">
          <label>恢复快照<select v-model="selectedDigest" class="input">
            <option value="" disabled>选择快照</option>
            <option v-for="snapshot in status.snapshots" :key="snapshot.digest" :value="snapshot.digest" :disabled="!snapshot.portable_ready">
              {{ snapshot.refs.join(', ') || 'snapshot' }} · {{ shortDigest(snapshot.digest) }}{{ snapshot.portable_ready ? '' : ' · 不完整' }}
            </option>
          </select></label>
          <label>风险确认<input v-model="confirmText" class="input" placeholder="输入:准备还原" autocomplete="off" /></label>
          <button class="btn" :disabled="preparing || !canPrepareRestore" @click="prepareRestore">{{ preparing ? '正在全链预检…' : '生成恢复交接' }}</button>
        </div>

        <div v-if="handoff" class="handoff" data-test="restore-handoff">
          <div class="handoff-title"><Check :size="16" /><div>交接单 <code>{{ handoff.id }}</code></div><span>{{ handoff.target_generation }} · {{ formatBytes(handoff.bytes_to_write) }} · {{ handoff.counts.insert ?? 0 }} 条记录</span></div>
          <ol>
            <li v-for="(entry, index) in [
              ['1. 全链校验便携仓库', handoff.commands.verify],
              ['2. 创建并校验 exact DR', handoff.commands.exact_dr],
              ['3. 停服前重算导入计划', handoff.commands.plan],
              ['4. 停掉全部写入者、移走旧库后执行', handoff.commands.restore],
            ]" :key="index">
              <div><b>{{ entry[0] }}</b><button class="copy-btn" @click="copy(entry[1])"><ClipboardCopy :size="13" />复制</button></div>
              <pre>{{ entry[1] }}</pre>
            </li>
          </ol>
        </div>
      </div>

      <div v-if="status.operations.length" class="card pad recovery-card">
        <div class="card-h"><RefreshCw :size="15" />最近操作</div>
        <div class="operation" v-for="operation in status.operations" :key="operation.id">
          <span class="state-pill" :class="`op-${operation.status}`">{{ statusLabel(operation.status) }}</span>
          <div><b>{{ operation.vendor_media ? '增量备份 + 原视频' : '增量备份' }}</b><small>{{ formatTime(operation.created_at) }} · {{ shortDigest(operation.snapshot_digest) }}</small><em v-if="operation.error">{{ operation.error }}</em></div>
          <span>{{ operation.stats.jobs ?? 0 }} Jobs</span>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.recovery-page{max-width:980px}.back-link{border:0;background:none;color:var(--ink-600);display:flex;align-items:center;gap:5px;padding:0;margin-bottom:14px;cursor:pointer}.page-title{display:flex;gap:10px;align-items:flex-start;margin-bottom:20px}.page-title h1{font-size:20px;margin:0 0 4px}.page-title p,.card-head p,.video-box p{font-size:13px;color:var(--ink-600);margin:0}.recovery-card{margin-bottom:18px}.card-head{display:flex;justify-content:space-between;gap:18px;margin-bottom:15px}.card-head>div{min-width:0}.state-pill{align-self:flex-start;font-size:12px;padding:4px 9px;border-radius:999px;background:var(--mut-bg);white-space:nowrap}.recovery-state-ready,.op-success{background:#ecfdf3;color:#15803d}.recovery-state-incomplete,.recovery-state-locked,.op-interrupted{background:#fff7ed;color:#c2410c}.recovery-state-error,.op-failed{background:#fef2f2;color:#dc2626}.op-running,.op-queued{background:var(--brand-50);color:var(--brand-700)}.facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-bottom:14px}.facts>div{background:var(--mut-bg);border-radius:8px;padding:10px;min-width:0}.facts span,.facts b,.facts code{display:block;font-size:12px}.facts span{color:var(--ink-500);margin-bottom:5px}.facts code{overflow:hidden;text-overflow:ellipsis}.notice{display:flex;align-items:flex-start;gap:7px;padding:9px 11px;border-radius:8px;font-size:12px;margin:10px 0;min-width:0;overflow-wrap:anywhere}.notice.danger{background:#fef2f2;color:#b91c1c}.notice.warn{background:#fff7ed;color:#9a3412}.video-box{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:10px;border:1px solid var(--line);border-radius:9px;padding:12px;margin:12px 0}.video-box>span{font-size:12px;color:var(--ink-500)}.options{display:flex;gap:16px;align-items:center;flex-wrap:wrap;font-size:12px}.options label{display:flex;gap:6px;align-items:center}.options small{color:var(--ink-500)}.actions{display:flex;gap:12px;align-items:center;margin-top:14px}.actions button{display:flex;align-items:center;gap:6px}.actions span{font-size:12px;color:var(--ink-500)}.restore-form{display:grid;grid-template-columns:1fr 220px auto;gap:10px;align-items:end;margin-top:14px}.restore-form label{font-size:12px;color:var(--ink-600)}.restore-form .input{display:block;width:100%;margin-top:5px}.handoff{margin-top:16px;border-top:1px solid var(--line);padding-top:14px}.handoff-title{display:flex;align-items:center;flex-wrap:wrap;gap:6px;font-size:13px;font-weight:600}.handoff-title>div{min-width:0;overflow-wrap:anywhere}.handoff-title span{margin-left:auto;color:var(--ink-500);font-weight:400;overflow-wrap:anywhere}.handoff ol{padding-left:22px}.handoff li{margin:12px 0}.handoff li>div{display:flex;justify-content:space-between;align-items:center}.copy-btn{border:0;background:none;color:var(--brand-600);display:flex;gap:4px;align-items:center;cursor:pointer}.handoff pre{white-space:pre-wrap;word-break:break-all;background:#101827;color:#dbeafe;padding:10px;border-radius:7px;font-size:11px}.operation{display:grid;grid-template-columns:auto 1fr auto;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--line)}.operation:first-of-type{border-top:0}.operation b,.operation small,.operation em{display:block;font-size:12px}.operation small{color:var(--ink-500);margin-top:3px}.operation em{color:#dc2626;margin-top:3px;font-style:normal}.operation>span:last-child{font-size:12px;color:var(--ink-500)}.error-state{display:flex;justify-content:space-between}.muted{color:var(--ink-500);font-size:13px}.spin{animation:spin 1s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}@media(max-width:760px){.facts{grid-template-columns:1fr 1fr}.restore-form{grid-template-columns:1fr}.actions{align-items:flex-start;flex-direction:column}.video-box{grid-template-columns:auto 1fr}.video-box>span{grid-column:2}.operation{grid-template-columns:auto 1fr}.operation>span:last-child{display:none}.handoff-title span{width:100%;margin-left:22px}}
</style>
