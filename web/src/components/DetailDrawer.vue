<script setup lang="ts">
// 详情抽屉的外框：状态、路径、说明、结果与概要。字幕生成和影片修复共用。
import { NButton, NDrawer, NDrawerContent } from 'naive-ui'
import { computed } from 'vue'
import type { ListStatus } from '@/api/types'
import { statusLabels, statusTones } from '@/lib/status'
import { t } from '@/i18n'

export interface DetailRow {
  label: string
  value: string | null | undefined
  /** 有值时在这一行后面显示下载按钮。 */
  href?: string
}
export interface DetailSection { title: string; rows: DetailRow[] }

const props = defineProps<{
  show: boolean
  title: string
  status?: ListStatus | null
  step?: string | null
  tags?: string[]
  path?: string | null
  /** 已完成时是说明（如“已跳过”），其余状态按错误显示。 */
  note?: string | null
  sections?: DetailSection[]
  loading?: boolean
  error?: string
}>()
const emit = defineEmits<{ close: [] }>()

const visible = computed(() =>
  (props.sections || [])
    .map((section) => ({ ...section, rows: section.rows.filter((row) => row.value) }))
    .filter((section) => section.rows.length),
)
</script>

<template>
  <NDrawer :show="show" :width="560" style="max-width: 100vw" @update:show="(value: boolean) => { if (!value) emit('close') }">
    <NDrawerContent :title="title" closable>
      <div v-if="error" class="warning bad">{{ t('common.loadDetailFailed', { error }) }}</div>
      <div v-else-if="loading" class="muted">{{ t('common.loading') }}</div>
      <template v-else>
        <div class="detail-head">
          <span v-if="status" class="pill" :class="statusTones[status]">{{ statusLabels[status] }}</span>
          <span v-if="step" class="state-note">{{ step }}</span>
          <span v-for="tag in tags" :key="tag" class="tag">{{ tag }}</span>
        </div>
        <div v-if="path" class="path wrap">{{ path }}</div>
        <div v-if="note" class="detail-note" :class="{ error: status !== 'completed' }">{{ note }}</div>
        <div v-for="section in visible" :key="section.title" class="detail-section">
          <h3>{{ section.title }}</h3>
          <div class="detail-rows">
            <div v-for="row in section.rows" :key="row.label" class="detail-row">
              <span>{{ row.label }}</span>
              <b>{{ row.value }}</b>
              <NButton v-if="row.href" size="small" tag="a" :href="row.href" download>{{ t('common.download') }}</NButton>
            </div>
          </div>
        </div>
        <slot />
      </template>
    </NDrawerContent>
  </NDrawer>
</template>

