<script setup lang="ts">
// 大模型：翻译主池与兜底池的节点，以及每个节点的实时调度状态。
import { NButton, NModal, NSwitch } from 'naive-ui'
import { computed, onActivated, reactive, ref } from 'vue'
import { api, type LlmNode, type NodePool, type Upstream } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import { toast } from '@/lib/feedback'
import { duration, fullDate } from '@/lib/format'
import type { Tone } from '@/lib/status'
import { config, loadConfig } from '@/stores/config'
import { refreshStatus, status } from '@/stores/status'
import NodeEditorDialog from './NodeEditorDialog.vue'
import type { NodeEditorTarget } from './types'
import { t } from '@/i18n'

const pools: { id: NodePool; role: 'primary' | 'fallback'; name: string; hint: string; empty: string }[] = [
  { id: 'translation', role: 'primary', name: t('llm.pool.translation'), hint: t('llm.pool.translationHelp'), empty: t('llm.pool.translationEmpty') },
  { id: 'fallback', role: 'fallback', name: t('llm.pool.fallback'), hint: t('llm.pool.fallbackHelp'), empty: t('llm.pool.fallbackEmpty') },
]
type NodeErrors = { probeError: string | null; lastError: string | null }

function nodeErrors(runtime: Upstream | undefined): NodeErrors | null {
  const probeError = runtime?.last_probe_error || (runtime?.last_probe_status && runtime.last_probe_status !== 'compatible' ? t('llm.probeFailed') : null)
  const lastError = runtime?.last_error || null
  return probeError || lastError ? { probeError, lastError } : null
}

const nodes = computed(() => config.value?.llm.nodes || [])
/** 节点的运行状态，来自每 5 秒一次的状态轮询。 */
const upstreams = computed<Upstream[]>(() => {
  const translation = status.value?.translation
  return [...(translation?.upstreams || []), ...(translation?.fallback_upstreams || [])]
})
const roleOf = (upstream: Upstream) => upstream.role || 'primary'
const usable = (upstream: Upstream) => upstream.configured && upstream.enabled
const sum = (list: Upstream[], key: 'active_requests' | 'concurrency') => list.reduce((total, item) => total + (Number(item[key]) || 0), 0)

const hint = computed(() => (nodes.value.length
  ? t('llm.summary', { count: nodes.value.length, usable: upstreams.value.filter(usable).length })
  : t('llm.empty')))

const poolViews = computed(() => pools.map((pool) => {
  const list = nodes.value.filter((node) => node.pool === pool.id)
  const ready = upstreams.value.filter((item) => roleOf(item) === pool.role && usable(item))
  return {
    ...pool,
    stats: list.length ? t('llm.poolSummary', { usable: ready.length, total: list.length, active: sum(ready, 'active_requests'), limit: sum(ready, 'concurrency') }) : '',
    nodes: list.map((node) => {
      const runtime = upstreams.value.find((item) => item.name === node.name && roleOf(item) === pool.role)
      return { node, runtime, errors: nodeErrors(runtime) }
    }),
  }
}))

function nodeState(node: LlmNode, runtime: Upstream | undefined): { text: string; tone: Tone } {
  if (!node.key_configured || (runtime && !runtime.configured)) return { text: t('llm.state.noKey'), tone: 'bad' }
  if (!runtime) return { text: t('llm.state.notLoaded'), tone: '' }
  if (!runtime.enabled) return { text: t('llm.state.stopped'), tone: 'bad' }
  if (runtime.cooldown_seconds > 0) return { text: t('llm.state.cooling', { time: duration(runtime.cooldown_seconds) }), tone: 'warn' }
  return runtime.active_requests > 0 ? { text: t('llm.state.working'), tone: 'work' } : { text: t('llm.state.available'), tone: 'good' }
}

function probeText(runtime: Upstream | undefined): string {
  if (!runtime?.last_probe_status) return ''
  return [
    t('llm.probeResult', { result: runtime.last_probe_status === 'compatible' ? t('llm.probe.passed') : t('llm.probe.failed') }),
    runtime.last_probe_seconds != null ? duration(runtime.last_probe_seconds) : '',
    runtime.last_probe_at ? fullDate(runtime.last_probe_at) : '',
  ].filter(Boolean).join(' · ')
}

const nodeMeta = (node: LlmNode) =>
  [node.model, node.reasoning_effort ? t('llm.reasoning', { effort: node.reasoning_effort }) : ''].filter(Boolean).join(' · ')

async function reload(): Promise<void> {
  try {
    await loadConfig()
  } catch (error) {
    toast.error(t('llm.loadFailed'), error)
  }
}
onActivated(reload)

// ---- 节点操作 ----
const editor = ref<NodeEditorTarget | null>(null)
const errorDetail = ref<(NodeErrors & { name: string }) | null>(null)
const probing = reactive(new Set<string>())
const canProbe = (runtime: Upstream | undefined) => !!runtime && usable(runtime) && !(runtime.cooldown_seconds > 0) && !probing.has(runtime.name)

async function toggleNode(node: LlmNode, enabled: boolean): Promise<void> {
  try {
    await api.updateNode(node.id, {
      name: node.name, pool: node.pool, protocol: node.protocol, base_url: node.base_url, auth: node.auth, project: node.project,
      location: node.location, api_key: null, model: node.model, concurrency: node.concurrency, reasoning_effort: node.reasoning_effort, enabled,
    })
    toast.ok(t('llm.toggled', { name: node.name, state: enabled ? t('llm.toggle.on') : t('llm.toggle.off') }))
    await Promise.all([reload(), refreshStatus()])
  } catch (error) {
    toast.error(t('llm.toggleFailed'), error)
  }
}

async function enableUpstream(name: string): Promise<void> {
  try {
    await api.enableUpstream(name)
    toast.ok(t('llm.reenabled', { name }))
    await refreshStatus()
  } catch (error) {
    toast.error(t('llm.enableFailed'), error)
  }
}

async function probe(name: string): Promise<void> {
  if (probing.has(name)) return
  probing.add(name)
  try {
    const body = await api.probeUpstream(name)
    if (!body.compatible) throw new Error(body.error || t('llm.probeFailed'))
    toast.ok(t('llm.probePassed', { name }))
  } catch (error) {
    toast.error(t('llm.probeFailedName', { name }), error)
  } finally {
    probing.delete(name)
    await refreshStatus()
  }
}
</script>

<template>
  <section class="pane">
    <div class="page-head">
      <div class="page-title"><h2>{{ t('llm.title') }}</h2><p class="page-sub">{{ hint }}</p></div>
      <div class="page-actions">
        <NButton type="primary" @click="editor = { pool: 'translation' }"><template #icon><AppIcon name="plus" /></template>{{ t('llm.add') }}</NButton>
      </div>
    </div>

    <section class="card">
      <div v-if="!config" class="empty">{{ t('llm.loading') }}</div>
      <div v-else class="llm-pools">
        <div v-for="pool in poolViews" :key="pool.id" class="pool">
          <div class="pool-head">
            <div><b>{{ pool.name }}</b><span class="pool-hint">{{ pool.hint }}</span></div>
            <span class="pool-tools">
              <span v-if="pool.stats" class="muted num">{{ pool.stats }}</span>
              <NButton size="small" quaternary @click="editor = { pool: pool.id }"><template #icon><AppIcon name="plus" /></template>{{ t('llm.addShort') }}</NButton>
            </span>
          </div>
          <div v-if="!pool.nodes.length" class="pool-empty">{{ pool.empty }}</div>
          <div v-else class="upstream-list">
            <div v-for="{ node, runtime, errors } in pool.nodes" :key="node.id" class="node" :class="{ off: !node.enabled }">
              <div class="upstream-top">
                <div class="upstream-title">
                  <span class="upstream-name">{{ node.name }}</span>
                </div>
                <span class="upstream-tools">
                  <NSwitch size="small" :value="node.enabled" :title="node.enabled ? t('llm.accepting') : t('llm.paused')" :aria-label="t('llm.accepting')" @update:value="(enabled: boolean) => toggleNode(node, enabled)" />
                  <NButton size="small" quaternary :title="t('common.edit')" :aria-label="t('llm.editModel')" @click="editor = { node }"><template #icon><AppIcon name="edit" /></template></NButton>
                </span>
              </div>
              <div class="upstream-url" :title="node.model">{{ nodeMeta(node) }}</div>
              <div v-if="!node.enabled" class="muted node-off">{{ t('llm.pausedHelp') }}</div>
              <div v-else class="unit">
                <div class="unit-main">
                  <span class="unit-label">{{ t('list.col.status') }}</span>
                  <div class="unit-state">
                    <span class="pill" :class="nodeState(node, runtime).tone">{{ nodeState(node, runtime).text }}</span>
                    <NButton v-if="runtime?.configured && !runtime.enabled" size="tiny" type="success" secondary @click="enableUpstream(node.name)">{{ t('llm.reenable') }}</NButton>
                  </div>
                </div>
                <dl class="unit-metrics">
                  <div class="unit-metric">
                    <dt>{{ t('llm.requests') }}</dt>
                    <dd>{{ runtime?.active_requests || 0 }}<span> / {{ runtime?.concurrency || node.concurrency }}</span></dd>
                  </div>
                  <div class="unit-metric" :title="t('llm.failureHelp', { threshold: runtime?.failure_threshold || 8, count: runtime?.failure_count || 0 })">
                    <dt>{{ t('llm.failures') }}</dt>
                    <dd :class="{ bad: runtime?.consecutive_failures }">{{ runtime?.consecutive_failures || 0 }}<span> / {{ runtime?.failure_threshold || 8 }}</span></dd>
                  </div>
                </dl>
                <div class="unit-test">
                  <div class="unit-test-head">
                    <span class="unit-label">{{ t('llm.connection') }}</span>
                    <div class="unit-actions">
                      <NButton size="tiny" secondary :loading="probing.has(node.name)" :disabled="!canProbe(runtime)" :aria-label="t('llm.testName', { name: node.name })" @click="probe(node.name)">
                        <template #icon><AppIcon name="bolt" /></template>{{ probing.has(node.name) ? t('llm.testing') : t('llm.test') }}
                      </NButton>
                    </div>
                  </div>
                  <div v-if="errors" class="unit-probe unit-result">
                    <span class="bad-text" :title="[errors.probeError, errors.lastError].filter(Boolean).join('\n\n')">{{ t('llm.errors') }}</span>
                    <NButton size="tiny" text :aria-label="t('llm.viewErrors', { name: node.name })" @click="errorDetail = { name: node.name, ...errors }">{{ t('common.viewDetail') }}</NButton>
                  </div>
                  <div v-else class="unit-probe">{{ probeText(runtime) || t('llm.untested') }}</div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>

    <NodeEditorDialog v-model="editor" @saved="reload" />
    <NModal :show="!!errorDetail" preset="card" class="modal-card" :title="t('llm.errorDetail')" @update:show="(show: boolean) => { if (!show) errorDetail = null }">
      <template v-if="errorDetail">
        <p class="node-error-name">{{ errorDetail.name }}</p>
        <div class="node-error-body">
          <div v-if="errorDetail.probeError" class="unit-error"><b>{{ t('llm.testError') }}</b><p>{{ errorDetail.probeError }}</p></div>
          <div v-if="errorDetail.lastError" class="unit-error"><b>{{ t('llm.runtimeError') }}</b><p>{{ errorDetail.lastError }}</p></div>
        </div>
      </template>
    </NModal>
  </section>
</template>
