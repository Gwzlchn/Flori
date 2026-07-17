<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { Images, Table2, X } from 'lucide-vue-next'
import type { VisualCatalogItem } from './types'

const props = defineProps<{
  figures: VisualCatalogItem[]
  tables: VisualCatalogItem[]
  activeId: string
}>()

const emit = defineEmits<{ select: [id: string] }>()
const drawerOpen = ref(false)
const toggleButton = ref<HTMLButtonElement | null>(null)
const drawer = ref<HTMLElement | null>(null)

async function openDrawer(): Promise<void> {
  drawerOpen.value = true
  await nextTick()
  drawer.value?.querySelector<HTMLButtonElement>('.visual-nav-item')?.focus()
}

function closeDrawer(): void {
  drawerOpen.value = false
  void nextTick(() => toggleButton.value?.focus())
}

function select(id: string, close: boolean): void {
  emit('select', id)
  if (close) closeDrawer()
}

function onDrawerKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') {
    closeDrawer()
    return
  }
  if (event.key !== 'Tab' || !drawer.value) return
  const focusable = [...drawer.value.querySelectorAll<HTMLElement>('button:not([disabled]),a[href]')]
  if (!focusable.length) return
  const first = focusable[0]
  const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}
</script>

<template>
  <button
    ref="toggleButton"
    class="visual-catalog-toggle"
    type="button"
    aria-controls="visual-catalog-drawer"
    :aria-expanded="drawerOpen"
    @click="openDrawer"
  >
    图表目录 · 图 {{ figures.length }} / 表 {{ tables.length }}
  </button>

  <nav class="visual-catalog visual-catalog-desktop" aria-label="图表目录">
    <div class="visual-nav-group">
      <div class="visual-nav-heading"><Images :size="14" />图 <span>{{ figures.length }}</span></div>
      <p v-if="!figures.length" class="visual-nav-empty">无</p>
      <button
        v-for="item in figures"
        :key="item.id"
        type="button"
        class="visual-nav-item"
        :class="{ on: item.id === activeId }"
        :aria-current="item.id === activeId ? 'location' : undefined"
        @click="select(item.id, false)"
      ><b>{{ item.label }}</b><span>{{ item.caption || '无图注' }}</span></button>
    </div>
    <div class="visual-nav-group">
      <div class="visual-nav-heading"><Table2 :size="14" />表 <span>{{ tables.length }}</span></div>
      <p v-if="!tables.length" class="visual-nav-empty">无</p>
      <button
        v-for="item in tables"
        :key="item.id"
        type="button"
        class="visual-nav-item"
        :class="{ on: item.id === activeId }"
        :aria-current="item.id === activeId ? 'location' : undefined"
        @click="select(item.id, false)"
      ><b>{{ item.label }}</b><span>{{ item.caption || '无表注' }}</span></button>
    </div>
  </nav>

  <Teleport to="body">
    <div v-if="drawerOpen" class="visual-drawer-backdrop" @click.self="closeDrawer">
      <section
        id="visual-catalog-drawer"
        ref="drawer"
        class="visual-catalog-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="图表目录"
        @keydown="onDrawerKeydown"
      >
        <header><b>图表目录</b><button type="button" aria-label="关闭图表目录" @click="closeDrawer"><X :size="18" /></button></header>
        <nav class="visual-catalog" aria-label="移动端图表目录">
          <div class="visual-nav-group">
            <div class="visual-nav-heading"><Images :size="14" />图 <span>{{ figures.length }}</span></div>
            <p v-if="!figures.length" class="visual-nav-empty">无</p>
            <button v-for="item in figures" :key="item.id" type="button" class="visual-nav-item"
              :class="{ on: item.id === activeId }" :aria-current="item.id === activeId ? 'location' : undefined"
              @click="select(item.id, true)"><b>{{ item.label }}</b><span>{{ item.caption || '无图注' }}</span></button>
          </div>
          <div class="visual-nav-group">
            <div class="visual-nav-heading"><Table2 :size="14" />表 <span>{{ tables.length }}</span></div>
            <p v-if="!tables.length" class="visual-nav-empty">无</p>
            <button v-for="item in tables" :key="item.id" type="button" class="visual-nav-item"
              :class="{ on: item.id === activeId }" :aria-current="item.id === activeId ? 'location' : undefined"
              @click="select(item.id, true)"><b>{{ item.label }}</b><span>{{ item.caption || '无表注' }}</span></button>
          </div>
        </nav>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.visual-catalog { display: flex; flex-direction: column; gap: 18px; }
.visual-catalog-desktop { position: sticky; top: 88px; align-self: start; max-height: calc(100vh - 112px); overflow-y: auto; padding-right: 5px; }
.visual-catalog-toggle { display: none; width: 100%; min-height: 44px; padding: 8px 12px; border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--surface); color: var(--ink-700); text-align: left; font-weight: 600; }
.visual-nav-group { min-width: 0; }
.visual-nav-heading { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; color: var(--ink-800); font-size: 12px; font-weight: 700; }
.visual-nav-heading span { color: var(--ink-400); font-weight: 500; }
.visual-nav-item { display: flex; width: 100%; min-width: 0; flex-direction: column; gap: 1px; padding: 6px 8px; border-left: 2px solid var(--line); color: var(--ink-500); text-align: left; }
.visual-nav-item b { overflow: hidden; color: inherit; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.visual-nav-item span { overflow: hidden; font-size: 11.5px; text-overflow: ellipsis; white-space: nowrap; }
.visual-nav-item:hover, .visual-nav-item:focus-visible { border-color: var(--brand-300); background: var(--brand-50); color: var(--ink-800); outline: none; }
.visual-nav-item.on { border-color: var(--brand-600); color: var(--brand-700); font-weight: 600; }
.visual-nav-empty { padding-left: 8px; color: var(--ink-400); font-size: 12px; }

.visual-drawer-backdrop { position: fixed; inset: 0; z-index: 1000; display: flex; justify-content: flex-end; background: rgba(0, 0, 0, .35); }
.visual-catalog-drawer { width: min(88vw, 340px); height: 100%; overflow-y: auto; background: var(--surface); box-shadow: var(--sh-lg); }
.visual-catalog-drawer header { position: sticky; top: 0; z-index: 1; display: flex; align-items: center; justify-content: space-between; min-height: 52px; padding: 10px 14px; border-bottom: 1px solid var(--line); background: var(--surface); }
.visual-catalog-drawer header button { display: grid; width: 44px; height: 44px; place-items: center; }
.visual-catalog-drawer .visual-catalog { padding: 14px; }
.visual-catalog-drawer .visual-nav-item { min-height: 44px; justify-content: center; }

@media (max-width: 900px) {
  .visual-catalog-desktop { display: none; }
  .visual-catalog-toggle { display: block; }
}
</style>
