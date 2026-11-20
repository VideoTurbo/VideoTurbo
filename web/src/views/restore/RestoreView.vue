<script setup lang="ts">
// 影片修复：一部影片一行，影片来自媒体库扫描。
import { NButton, NCheckbox } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { api, type ListView, type RestoreAction, type RestoreMedia, type RestoreStatus, type Warning } from '@/api'
import AppIcon from '@/components/AppIcon.vue'
import WarningList from '@/components/WarningList.vue'
import BatchDock from '@/components/BatchDock.vue'
import ListFooter from '@/components/ListFooter.vue'
import ListToolbar from '@/components/ListToolbar.vue'
import type { MenuItem } from '@/components/menu'
import MoreMenu from '@/components/MoreMenu.vue'
import PosterCell from '@/components/PosterCell.vue'
import SortHeader from '@/components/SortHeader.vue'
import { confirm, toast } from '@/lib/feedback'
import { fileName, fullDate, secondsSince, shortDate, sizeText } from '@/lib/format'
import { nextSort, type Sort, type SortColumn } from '@/lib/jobs'
import { debounced, PAGE_SIZE, useLiveReload } from '@/lib/list'
import { restoreActionNames } from '@/lib/restore'
import { DEFAULT_VIEW, emptyText as viewEmptyText } from '@/lib/status'
import { isDesktop, showPosters } from '@/stores/status'
import RestoreDetailDrawer from './RestoreDetailDrawer.vue'
import RestoreDialog from './RestoreDialog.vue'
import RestoreStatusCell from './RestoreStatusCell.vue'
import { t } from '@/i18n'

const view = ref<ListView>(DEFAULT_VIEW)
const sort = ref<Sort>('updated_desc')
const search = ref('')
const library = ref('')
const page = ref(1)
const keyword = debounced(search)

const restoreStatus = ref<RestoreStatus | null>(null)
const items = ref<RestoreMedia[]>([])
const counts = ref<Partial<Record<ListView, number>>>({})
const total = ref(0)
const pages = ref(1)
let requestId = 0

async function load(): Promise<void> {
  const id = ++requestId
  try {
    const [statusData, data] = await Promise.all([
      api.restoreStatus(),
      api.restoreMedia({ status: view.value, sort: sort.value, page: page.value, page_size: PAGE_SIZE, query: keyword.value.trim(), library: library.value }),
    ])
    if (id !== requestId) return
    restoreStatus.value = statusData
    items.value = data.items || []
    counts.value = data.counts || {}
    total.value = data.total || 0
    pages.value = data.pages || 1
    page.value = data.page || 1
    // 已勾选的行和打开的详情换成最新数据，批量操作按当前状态判断。
    for (const item of items.value) {
      if (selected.value.has(item.media_id)) selected.value.set(item.media_id, item)
      if (detail.value?.media_id === item.media_id) detail.value = item
    }
  } catch (error) {
    if (id === requestId) toast.error(t('restore.loadFailed'), error)
  }
}

watch([view, sort, keyword, library], () => {
  page.value = 1
  void load()
})
watch(page, (next, previous) => {
  if (next !== previous) void load()
})
useLiveReload(load)

const emptyText = computed(() => (keyword.value.trim() || library.value ? t('restore.noMatch') : viewEmptyText(view.value, t('restore.itemName'))))
const detail = ref<RestoreMedia | null>(null)

const warnings = computed(() => {
  const s = restoreStatus.value
  if (!s) return []
  const list: Warning[] = []
  const timeout = s.heartbeat_timeout_seconds || 90
  const alive = <T extends { last_seen_at: string }>(workers: T[] = []) => workers.filter((worker) => secondsSince(worker.last_seen_at) <= timeout)
  if (!s.enabled) list.push({ message: t(isDesktop.value ? 'restore.noBackup.desktop' : 'restore.noBackup') })
  if (!alive(s.workers).length) list.push({ message: t('restore.noMachine'), target: 'compute' })
  const unavailable = alive(s.unavailable_workers)
  if (unavailable.length) list.push({ message: t('restore.jasnaFailed', { names: unavailable.map((worker) => worker.name).join(t('common.listSeparator')), reason: unavailable[0]!.reason }), target: 'compute' })
  return list
})

// ---- 提交修复 ----
const dialogMedia = ref<RestoreMedia[]>([])

function openDialog(media: RestoreMedia[]): void {
  if (restoreStatus.value && !restoreStatus.value.enabled) return toast.fail(t('restore.noBackupShort'))
  dialogMedia.value = media
}
function onSubmitted(ids: number[]): void {
  ids.forEach((id) => selected.value.delete(id))
  if (view.value === 'all' || view.value === 'active') void load()
  else view.value = 'active'
}

// ---- 单项操作 ----
const actionConfirms: Partial<Record<RestoreAction, (count: number) => Promise<boolean>>> = {
  purge_original: (count) => confirm(count > 1 ? t('restore.purgeManyConfirm', { count }) : t('restore.purgeConfirm'),
    { title: t('restore.purgeTitle'), confirmLabel: t('restore.purgeButton'), danger: true }),
  cancel: (count) => confirm(count > 1 ? t('restore.cancelManyConfirm', { count }) : t('restore.cancelConfirm'),
    { title: t('jobs.action.cancelTitle'), confirmLabel: t('common.confirmCancel'), danger: true }),
}

async function runAction(jobId: string, action: RestoreAction): Promise<void> {
  if (await actionConfirms[action]?.(1) === false) return
  try {
    const body = await api.restoreJobAction(jobId, action)
    toast.ok(t('restore.actionResult', { action: restoreActionNames[action], message: body.message || t('common.done') }))
    await load()
  } catch (error) {
    toast.error(t('restore.actionFailed', { action: restoreActionNames[action] }), error)
  }
}

function menuItems(media: RestoreMedia): MenuItem[] {
  const menu: MenuItem[] = [{ label: t('common.viewDetail'), icon: 'eye', run: () => { detail.value = media } }]
  const job = media.job
  if (media.status === 'failed' && media.actions.includes('restore')) menu.push({ label: t('restore.resubmit'), icon: 'wand', run: () => openDialog([media]) })
  if (job && media.actions.includes('purge_original')) menu.push('-', { label: t('restore.purgeBackup'), icon: 'trash', danger: true, run: () => runAction(job.id, 'purge_original') })
  return menu
}

// ---- 勾选与批量操作（按影片记录，翻页和自动刷新都不会丢）----
const selected = ref(new Map<number, RestoreMedia>())
const selectable = (media: RestoreMedia) => media.actions.length > 0
const allSelected = computed(() => {
  const rows = items.value.filter(selectable)
  return rows.length > 0 && rows.every((media) => selected.value.has(media.media_id))
})

function toggle(media: RestoreMedia, checked: boolean): void {
  if (checked) selected.value.set(media.media_id, media)
  else selected.value.delete(media.media_id)
}
function toggleAll(checked: boolean): void {
  items.value.filter(selectable).forEach((media) => toggle(media, checked))
}

function batchRestore(): void {
  const media = [...selected.value.values()].filter((item) => item.actions.includes('restore'))
  if (!media.length) return toast.fail(t('restore.noneEligible'))
  openDialog(media)
}

/** 批量操作只作用于所选影片里状态相符的那部分，其余忽略并在提示里说明。 */
async function runBatch(action: RestoreAction): Promise<void> {
  const chosen = [...selected.value.values()]
  const ids = chosen.flatMap((media) => (media.job && media.actions.includes(action) ? [media.job.id] : []))
  if (!ids.length) return toast.fail(t('restore.noneForAction', { action: restoreActionNames[action] }))
  if (await actionConfirms[action]?.(ids.length) === false) return
  try {
    const body = await api.restoreBatch(ids, action)
    const ignored = chosen.length - ids.length
    toast.ok(t('restore.batch.done', { action: restoreActionNames[action], count: body.succeeded.length })
      + (body.failed.length ? t('restore.batch.mismatch', { count: body.failed.length }) : '')
      + (ignored ? t('restore.batch.ignored', { count: ignored }) : ''))
    selected.value.clear()
    await load()
  } catch (error) {
    toast.error(t('restore.batchFailed'), error)
  }
}
</script>

<template>
  <section class="pane">
    <div class="page-head">
      <div class="page-title"><h2>{{ t('restore.title') }}</h2><p class="page-sub">{{ t('restore.lead') }}</p></div>
    </div>
    <div v-if="warnings.length" class="warnings">
      <WarningList :items="warnings" />
    </div>

    <section class="card table-card">
      <ListToolbar v-model:view="view" v-model:search="search" v-model:library="library" :counts="counts" :search-placeholder="t('restore.search')" />
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
            <tr v-for="media in items" :key="media.media_id" :class="{ selected: selected.has(media.media_id) }">
              <td class="col-check">
                <NCheckbox v-if="selectable(media)" :checked="selected.has(media.media_id)" :aria-label="t('restore.selectVideo')" @update:checked="(checked: boolean) => toggle(media, checked)" />
              </td>
              <PosterCell v-if="showPosters" :url="media.poster_url" />
              <td class="movie">
                <button class="movie-button" :title="media.path" @click="detail = media">{{ fileName(media.path) }}</button>
                <div class="movie-sub"><span class="tag">{{ media.library_name }}</span><span class="path">{{ media.path }}</span></div>
              </td>
              <td class="col-time">{{ sizeText(media.size) }}</td>
              <td class="job-info"><RestoreStatusCell :media="media" :models="restoreStatus?.models || {}" /></td>
              <td class="col-time" :title="fullDate(media.updated_at)">{{ shortDate(media.updated_at) }}</td>
              <td class="col-actions">
                <div class="row-actions">
                  <NButton v-if="media.status === 'pending' && media.actions.includes('restore')" size="small" @click="openDialog([media])">
                    <template #icon><AppIcon name="wand" /></template>{{ t('restore.action.restore') }}
                  </NButton>
                  <NButton v-else-if="media.job && media.actions.includes('cancel')" size="small" @click="runAction(media.job.id, 'cancel')">
                    <template #icon><AppIcon name="x" /></template>{{ t('restore.action.cancel') }}
                  </NButton>
                  <NButton v-else-if="media.job && media.actions.includes('retry')" size="small" @click="runAction(media.job.id, 'retry')">
                    <template #icon><AppIcon name="rotate" /></template>{{ t('restore.action.retry') }}
                  </NButton>
                  <MoreMenu :items="menuItems(media)" />
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!items.length" class="empty">{{ emptyText }}</div>
      </div>
      <ListFooter v-model:page="page" :total="total" :pages="pages" :shown="items.length" :page-size="PAGE_SIZE" />
    </section>

    <BatchDock :count="selected.size" @clear="selected.clear()">
      <NButton size="small" type="primary" @click="batchRestore">{{ t('restore.title') }}</NButton>
      <NButton size="small" @click="runBatch('retry')">{{ t('restore.action.retry') }}</NButton>
      <NButton size="small" @click="runBatch('cancel')">{{ t('restore.action.cancel') }}</NButton>
      <NButton size="small" type="error" secondary @click="runBatch('purge_original')">{{ t('restore.action.purge') }}</NButton>
    </BatchDock>

    <RestoreDialog v-model="dialogMedia" :status="restoreStatus" @submitted="onSubmitted" />
    <RestoreDetailDrawer :media="detail" :models="restoreStatus?.models || {}" @close="detail = null" />
  </section>
</template>
