<script setup lang="ts">
// 字幕生成：任务列表、单项与批量操作。筛选条件保存在地址栏。
import { NButton, NCheckbox } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { api, type Job, type ListView } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import type { IconName } from '@/components/icons'
import BatchDock from '@/components/BatchDock.vue'
import ListFooter from '@/components/ListFooter.vue'
import ListToolbar from '@/components/ListToolbar.vue'
import type { MenuItem } from '@/components/menu'
import MoreMenu from '@/components/MoreMenu.vue'
import PosterCell from '@/components/PosterCell.vue'
import SortHeader from '@/components/SortHeader.vue'
import { confirm, toast } from '@/lib/feedback'
import { fileName, fullDate, shortDate, sizeText } from '@/lib/format'
import { isSort, nextSort, primaryJobAction, type Sort, type SortColumn } from '@/lib/jobs'
import { debounced, PAGE_SIZE, queryParam, setQueryParams, useLiveReload } from '@/lib/list'
import { DEFAULT_VIEW, emptyText as viewEmptyText, isView, statusLabels, statusTones } from '@/lib/status'
import { errorMessage } from '@/api/http'
import { refreshStatus, showPosters } from '@/stores/status'
import { cancelJob, completeJob, pauseJob, reprocessJob, resumeJob, skipJob, startJob, togglePriority } from './jobActions'
import JobDetailDrawer from './JobDetailDrawer.vue'
import JobProgress from './JobProgress.vue'
import WarningList from '@/components/WarningList.vue'
import { pipelinePaused, resumePipeline, scanSummary, translationLive, warnings } from './overview'
import { t } from '@/i18n'

// ---- 筛选条件（初始值来自地址栏）----
const initialView = queryParam('view')
const initialSort = queryParam('sort')
const view = ref<ListView>(isView(initialView) ? initialView : DEFAULT_VIEW)
const sort = ref<Sort>(isSort(initialSort) ? initialSort : 'updated_desc')
const search = ref(queryParam('q') || '')
const library = ref(queryParam('library') || '')
const page = ref(1)
const keyword = debounced(search)

// ---- 列表数据 ----
const jobs = ref<Job[]>([])
const total = ref(0)
const pages = ref(1)
const counts = ref<Partial<Record<ListView, number>>>({})
let requestId = 0

async function load(): Promise<void> {
  const id = ++requestId
  try {
    const data = await api.jobs({
      view: view.value, sort: sort.value, page: page.value, page_size: PAGE_SIZE,
      q: keyword.value.trim(), library: library.value,
    })
    if (id !== requestId) return // 只采用最后一次请求的结果
    jobs.value = data.items || []
    total.value = data.total || 0
    pages.value = data.pages || 1
    page.value = data.page || 1
    counts.value = data.counts || {}
  } catch (error) {
    if (id === requestId) toast.error(t('subtitle.loadFailed'), error)
  }
}

watch([view, sort, keyword, library], () => {
  page.value = 1
  setQueryParams({
    view: view.value === DEFAULT_VIEW ? undefined : view.value,
    sort: sort.value === 'updated_desc' ? undefined : sort.value,
    q: keyword.value.trim(), library: library.value,
  })
  void load()
})
watch(page, (next, previous) => {
  if (next !== previous) void load()
})
useLiveReload(load)

const emptyText = computed(() => (keyword.value.trim() || library.value ? t('common.noMatch') : viewEmptyText(view.value, t('subtitle.itemName'))))

// ---- 行内操作 ----
const detailId = ref<string | null>(null)

/** 下载由链接完成（后端返回附件），其余操作调用接口。 */
const primaryActions: Record<NonNullable<ReturnType<typeof primaryJobAction>>, { label: string; icon: IconName; run?: (job: Job) => unknown }> = {
  start: { label: t('subtitle.action.generate'), icon: 'captions', run: (job) => startJob(job.id) },
  resume: { label: t('subtitle.action.resume'), icon: 'play', run: (job) => resumeJob(job.id) },
  retry: { label: t('subtitle.action.retry'), icon: 'rotate', run: (job) => reprocessJob(job.id, 'continue') },
  pause: { label: t('subtitle.action.pause'), icon: 'pause', run: (job) => pauseJob(job.id) },
  download: { label: t('subtitle.action.download'), icon: 'download' },
}
const primaryOf = (job: Job) => {
  const action = primaryJobAction(job)
  return action ? primaryActions[action] : null
}

function menuItems(job: Job): MenuItem[] {
  const can = (action: Job['actions'][number]) => job.actions.includes(action)
  const items: MenuItem[] = [{ label: t('common.viewDetail'), icon: 'eye', run: () => { detailId.value = job.id } }]
  if (can('priority')) {
    const priority = Number(job.priority) || 0
    items.push({ label: priority > 0 ? t('subtitle.action.unprioritize') : t('subtitle.action.prioritize'), icon: 'star', run: () => togglePriority(job.id, priority) })
  }
  if (can('complete')) items.push({ label: t('subtitle.action.complete'), icon: 'check', run: () => completeJob(job.id) })
  if (can('reprocess')) {
    items.push('-', { heading: t('subtitle.action.reprocess') })
    if (job.status !== 'completed') {
      items.push({ label: t('subtitle.reprocess.continue'), icon: 'play', run: () => reprocessJob(job.id, 'continue') })
    }
    items.push(
      { label: t('subtitle.reprocess.retranslate'), icon: 'captions', run: () => reprocessJob(job.id, 'retranslate') },
      { label: t('subtitle.reprocess.reasr'), icon: 'mic', run: () => reprocessJob(job.id, 'reasr') },
      { label: t('subtitle.reprocess.full'), icon: 'rotate', run: () => reprocessJob(job.id, 'full') },
    )
  }
  if (can('cancel')) items.push('-', { label: t('subtitle.action.cancel'), icon: 'x', danger: true, run: () => cancelJob(job.id) })
  else if (can('skip')) items.push('-', { label: t('subtitle.action.skip'), icon: 'x', danger: true, run: () => skipJob(job.id) })
  return items
}

// ---- 勾选与批量操作（勾选按任务 ID 记录，翻页和自动刷新都不会丢）----
const selected = ref(new Set<string>())
const allSelected = computed(() => jobs.value.length > 0 && jobs.value.every((job) => selected.value.has(job.id)))

function toggle(id: string, checked: boolean): void {
  if (checked) selected.value.add(id)
  else selected.value.delete(id)
}
function toggleAll(checked: boolean): void {
  for (const job of jobs.value) toggle(job.id, checked)
}

const BATCH_LIMIT = 200
const batchProgress = ref('')
/** 需要先确认的批量操作。 */
const confirmNames: Record<string, string> = {
  pause: t('subtitle.batch.pause'), cancel: t('subtitle.batch.cancel'), retranslate: t('subtitle.batch.retranslate'), complete: t('subtitle.batch.complete'), skip: t('subtitle.action.skip'),
}
const batchNotes: Record<string, string> = {
  cancel: t('subtitle.batch.note.cancel'),
  skip: t('subtitle.batch.note.skip'),
  retranslate: t('subtitle.batch.note.retranslate'),
  complete: t('subtitle.batch.note.complete'),
}

async function runBatch(action: string): Promise<void> {
  const ids = [...selected.value]
  if (!ids.length || batchProgress.value) return
  const name = confirmNames[action]
  if (name && !await confirm(t('subtitle.batch.confirm', { name, count: ids.length, note: batchNotes[action] || '' }), { title: t('subtitle.batch.confirmTitle'), confirmLabel: name })) return
  // “标为已完成”要求整批一起成功，不能拆分。
  const step = action === 'complete' ? ids.length : BATCH_LIMIT
  const batches = Math.ceil(ids.length / step)
  let succeeded = 0, failed = 0, confirmed = 0
  try {
    for (let index = 0; index < batches; index++) {
      batchProgress.value = t('subtitle.batch.progress', { batches, current: index + 1 })
      const chunk = ids.slice(index * step, (index + 1) * step)
      const result = await api.jobBatch(chunk, action)
      succeeded += result.succeeded.length
      failed += result.failed.length
      result.succeeded.forEach((id) => selected.value.delete(id))
      confirmed += chunk.length
    }
    if (!failed) selected.value.clear()
    toast.ok(t('subtitle.batch.done', { succeeded }) + (failed ? t('subtitle.batch.ignored', { failed }) : ''))
  } catch (error) {
    toast.fail(t('subtitle.batch.interrupted', { succeeded, unconfirmed: ids.length - confirmed, error: errorMessage(error) }))
  } finally {
    batchProgress.value = ''
    await refreshStatus()
  }
}

const batchMenu: MenuItem[] = [
  { label: t('subtitle.batch.prioritize'), icon: 'star', run: () => runBatch('priority_high') },
  { label: t('subtitle.batch.normal'), icon: 'list', run: () => runBatch('priority_normal') },
  '-',
  { label: t('subtitle.batch.retranslate'), icon: 'captions', run: () => runBatch('retranslate') },
  { label: t('subtitle.batch.completeNeedsFile'), icon: 'check', run: () => runBatch('complete') },
  { label: t('subtitle.action.skip'), icon: 'x', run: () => runBatch('skip') },
]
</script>

<template>
  <section class="pane">
    <div class="page-head">
      <div class="page-title"><h2>{{ t('subtitle.title') }}</h2><p class="page-sub">{{ scanSummary }}</p></div>
    </div>
    <div v-if="pipelinePaused || warnings.length" class="warnings">
      <div v-if="pipelinePaused" class="warning">
        <span>{{ t('subtitle.paused') }}<button type="button" class="link-btn" @click="resumePipeline">{{ t('subtitle.resume') }}</button></span>
      </div>
      <WarningList :items="warnings" />
    </div>
    <div v-if="translationLive" class="live-line"><span :title="translationLive">{{ translationLive }}</span></div>

    <section class="card table-card">
      <ListToolbar v-model:view="view" v-model:search="search" v-model:library="library" :counts="counts" :search-placeholder="t('subtitle.search')" />
      <div class="table-wrap">
        <table class="jobs-table">
          <thead>
            <tr>
              <th class="col-check"><NCheckbox :checked="allSelected" :aria-label="t('common.selectPage')" @update:checked="toggleAll" /></th>
              <th v-if="showPosters" class="poster-col"><span class="sr-only">{{ t('list.col.poster') }}</span></th>
              <SortHeader column="name" :sort="sort" @sort="(c: SortColumn) => (sort = nextSort(sort, c))">{{ t('list.col.video') }}</SortHeader>
              <SortHeader column="size" :sort="sort" @sort="(c: SortColumn) => (sort = nextSort(sort, c))">{{ t('list.col.size') }}</SortHeader>
              <th>{{ t('list.col.status') }}</th>
              <SortHeader column="updated" :sort="sort" @sort="(c: SortColumn) => (sort = nextSort(sort, c))">{{ t('list.col.updated') }}</SortHeader>
              <th class="col-actions">{{ t('list.col.actions') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="job in jobs" :key="job.id" :class="{ selected: selected.has(job.id) }">
              <td class="col-check"><NCheckbox :checked="selected.has(job.id)" :aria-label="t('subtitle.selectJob')" @update:checked="(checked: boolean) => toggle(job.id, checked)" /></td>
              <PosterCell v-if="showPosters" :url="job.poster_url" />
              <td class="movie">
                <button class="movie-button" :title="job.media_path" @click="detailId = job.id">
                  {{ fileName(job.media_path) }}<span v-if="Number(job.priority) > 0" class="star">★</span>
                </button>
                <div class="movie-sub">
                  <span v-if="job.library_name" class="tag">{{ job.library_name }}</span>
                  <span class="tag" :title="t('subtitle.pairTitle', { source: job.source_language_label, target: job.target_language_label })">{{ job.source_language_label }} → {{ job.target_language_label }}</span>
                  <span class="path">{{ job.media_path }}</span>
                </div>
              </td>
              <td class="col-time">{{ sizeText(job.media_size) }}</td>
              <td class="job-info">
                <span class="pill" :class="statusTones[job.status]">{{ statusLabels[job.status] }}</span>
                <span v-if="job.step" class="state-note">{{ job.step }}</span>
                <JobProgress :job="job" />
              </td>
              <td class="col-time" :title="fullDate(job.updated_at)">{{ shortDate(job.updated_at) }}</td>
              <td class="col-actions">
                <div class="row-actions">
                  <NButton v-if="primaryJobAction(job) === 'download'" size="small" tag="a" :href="api.jobFileUrl(job.id, 'subtitle')" download>
                    <template #icon><AppIcon name="download" /></template>{{ t('subtitle.action.download') }}
                  </NButton>
                  <NButton v-else-if="primaryOf(job)" size="small" @click="primaryOf(job)!.run?.(job)">
                    <template #icon><AppIcon :name="primaryOf(job)!.icon" /></template>{{ primaryOf(job)!.label }}
                  </NButton>
                  <MoreMenu :items="menuItems(job)" />
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!jobs.length" class="empty">{{ emptyText }}</div>
      </div>
      <ListFooter v-model:page="page" :total="total" :pages="pages" :shown="jobs.length" :page-size="PAGE_SIZE" />
    </section>

    <BatchDock :count="selected.size" :label="batchProgress" @clear="selected.clear()">
      <NButton size="small" type="primary" :disabled="!!batchProgress" @click="runBatch('start')">{{ t('subtitle.action.generate') }}</NButton>
      <NButton size="small" :disabled="!!batchProgress" @click="runBatch('pause')">{{ t('subtitle.action.pause') }}</NButton>
      <NButton size="small" :disabled="!!batchProgress" @click="runBatch('resume')">{{ t('subtitle.action.resume') }}</NButton>
      <NButton size="small" :disabled="!!batchProgress" @click="runBatch('continue')">{{ t('subtitle.action.retry') }}</NButton>
      <MoreMenu :items="batchMenu" :text="t('common.more')" />
      <NButton size="small" type="error" secondary :disabled="!!batchProgress" @click="runBatch('cancel')">{{ t('subtitle.action.cancel') }}</NButton>
    </BatchDock>

    <JobDetailDrawer v-model="detailId" />
  </section>
</template>
