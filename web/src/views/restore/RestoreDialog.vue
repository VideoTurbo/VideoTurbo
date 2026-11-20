<script setup lang="ts">
// 提交影片修复：选择模型和选项。
import { NButton, NModal, NSelect } from 'naive-ui'
import { computed, ref, watch } from 'vue'
import { api, type RestoreMedia, type RestoreStatus } from '@/api'
import SwitchRow from '@/components/SwitchRow.vue'
import { toast } from '@/lib/feedback'
import { fileName } from '@/lib/format'
import { isLtx } from '@/lib/restore'
import { t } from '@/i18n'

const props = defineProps<{ status: RestoreStatus | null }>()
/** 要提交的影片；为空时关闭。 */
const media = defineModel<RestoreMedia[]>({ required: true })
const emit = defineEmits<{ submitted: [ids: number[]] }>()

const model = ref('basicvsrpp')
const largeCanvas = ref(false)
const submitting = ref(false)

const models = computed(() => Object.entries(props.status?.models || { basicvsrpp: 'BasicVSR++' }).map(([value, label]) => ({ value, label })))
const workerHint = computed(() => {
  const workers = props.status?.workers || []
  const ltx = workers.filter((worker) => worker.restore_models?.includes('ltx')).length
  return t('restore.dialog.machines', { ltx, count: workers.length })
})
const summary = computed(() => (media.value.length === 1 ? t('restore.dialog.single', { name: fileName(media.value[0]!.path) }) : t('restore.dialog.multiple', { count: media.value.length })))

// 每次打开都回到设置里的默认模型和选项。
watch(() => media.value.length > 0, (open) => {
  if (!open) return
  model.value = props.status?.settings?.restoration_model || 'basicvsrpp'
  largeCanvas.value = props.status?.settings?.ltx_large_canvas === true
})

async function submit(): Promise<void> {
  const ids = media.value.map((item) => item.media_id)
  submitting.value = true
  try {
    const body = await api.createRestoreJobs({ media_ids: ids, restoration_model: model.value, ltx_large_canvas: isLtx(model.value) && largeCanvas.value })
    const skipped = body.skipped || []
    toast.ok(t('restore.dialog.submitted', { count: body.created.length }) + (skipped.length ? t('restore.dialog.skipped', { count: skipped.length, reason: skipped[0]!.reason }) : ''))
    media.value = []
    emit('submitted', ids)
  } catch (error) {
    toast.error(t('restore.dialog.submitFailed'), error)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <NModal :show="media.length > 0" preset="card" class="modal-card" title="影片修复" @update:show="(show: boolean) => { if (!show) media = [] }">
    <p class="lead">{{ summary }}</p>
    <label class="dc-option">{{ t('restore.dialog.model') }}<NSelect v-model:value="model" :options="models" /></label>
    <SwitchRow
      v-model="largeCanvas" :label="t('restore.dialog.largeCanvas')" :disabled="!isLtx(model)"
      :tip="t('restore.dialog.largeCanvasHelp')"
    />
    <p class="field-help">{{ workerHint }}</p>
    <details class="more">
      <summary>{{ t('restore.dialog.timeTitle') }}</summary>
      <p>{{ t('restore.dialog.timeHelp') }}</p>
    </details>
    <template #footer>
      <div class="modal-actions">
        <NButton @click="media = []">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="submitting" @click="submit">{{ t('restore.dialog.start') }}</NButton>
      </div>
    </template>
  </NModal>
</template>
