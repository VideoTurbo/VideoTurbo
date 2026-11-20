<script lang="ts">
import type { InstalledPlugin, PluginPreview } from '@/api'

/** 待确认的安装或更新：preview 是新内容静态检查的结果；current 是要更新的已安装插件，安装时为空；
 *  upload 是本地上传的文件，确认后原样再次提交；repository 是从插件仓库安装时的仓库名称和仓库公布的文件指纹（可为空）。 */
export interface PluginConfirm {
  preview: PluginPreview
  current?: InstalledPlugin
  upload?: { filename: string; source: string }
  repository?: { name: string; sha256: string }
}
</script>

<script setup lang="ts">
// 安装、更新插件前的确认：面向普通用户，只显示插件信息（名称、类型、版本、来源）和一句风险提示；
// 插件 ID、大小、文件指纹（SHA-256）等技术信息折叠在“详细信息”里。
// 插件信息是静态读取的，未执行插件代码；实际以安装后为准。读不到的信息在更新时沿用已安装插件的。
import { NButton, NModal } from 'naive-ui'
import { computed } from 'vue'
import AppIcon from '@/components/AppIcon.vue'
import { fileName, sizeText } from '@/lib/format'
import { t } from '@/i18n'

defineProps<{ loading?: boolean }>()
/** 为空时关闭。 */
const pending = defineModel<PluginConfirm | null>({ required: true })
const emit = defineEmits<{ confirm: [pending: PluginConfirm] }>()

const versionText = (version?: string | null) => version ? `v${version.replace(/^v/i, '')}` : t('plugins.confirm.noVersion')
const hostOf = (address: string) => {
  try { return new URL(address).host } catch { return address }
}

const info = computed(() => {
  if (!pending.value) return null
  const { preview, current } = pending.value
  return {
    ...preview,
    label: preview.label || current?.label || fileName(preview.url || preview.filename || '').replace(/\.py$/i, '') || t('plugins.confirm.unnamed'),
    kinds: (preview.kinds.length ? preview.kinds : current?.kinds ?? []).map(kind => kind.label),
    icon: preview.icon || current?.icon || '',
    homepage: preview.homepage || current?.homepage || '',
    version: current ? `${versionText(current.version)} → ${versionText(preview.version)}` : versionText(preview.version),
  }
})
const updating = computed(() => !!pending.value?.current)
/** 仓库公布了文件指纹时，与下载的是否一致。 */
const repositoryMatch = computed(() => {
  const repository = pending.value?.repository
  if (!repository?.sha256) return null
  return repository.sha256.toLowerCase() === pending.value?.preview.sha256.toLowerCase()
})
const uploaded = computed(() => !!pending.value?.upload)
</script>

<template>
  <NModal :show="!!pending" preset="card" class="modal-card" :title="updating ? t('plugins.confirm.updateTitle') : t('plugins.confirm.installTitle')" :auto-focus="false" @update:show="(show: boolean) => { if (!show && !loading) pending = null }">
    <template v-if="pending && info">
      <div class="plugin-preview-head">
        <span class="plugin-icon large">
          <img v-if="info.icon" :src="info.icon" alt="" draggable="false">
          <span v-else>{{ info.label.slice(0, 1).toUpperCase() }}</span>
        </span>
        <div class="plugin-preview-title">
          <b>{{ info.label }}</b>
          <span class="plugin-tags">
            <span v-for="kind in info.kinds" :key="kind" class="pill plain">{{ kind }}</span>
            <span v-if="!info.kinds.length" class="pill plain">{{ t('plugins.confirm.unknownKind') }}</span>
            <span class="pill plain" :class="{ 'plugin-version-change': updating }">{{ info.version }}</span>
          </span>
        </div>
      </div>
      <dl class="plugin-preview-info">
        <template v-if="info.homepage">
          <dt>{{ t('plugins.confirm.homepage') }}</dt>
          <dd><a :href="info.homepage" target="_blank" rel="noopener noreferrer" :title="info.homepage">{{ hostOf(info.homepage) }}</a></dd>
        </template>
        <template v-if="pending.repository">
          <dt>{{ t('plugins.confirm.repository') }}</dt>
          <dd>{{ pending.repository.name }}</dd>
        </template>
        <dt>{{ t('plugins.confirm.source') }}</dt>
        <dd v-if="uploaded" :title="info.filename">{{ t('plugins.confirm.localFile', { filename: info.filename }) }}</dd>
        <dd v-else><a :href="info.url" target="_blank" rel="noopener noreferrer" :title="info.url">{{ hostOf(info.url || '') }}</a></dd>
        <template v-if="info.members.length > 1">
          <dt>{{ t('plugins.confirm.members') }}</dt>
          <dd>{{ info.members.map(member => t('plugins.confirm.member', { label: member.label || member.id, kind: member.kind_label })).join(t('common.listSeparator')) }}</dd>
        </template>
      </dl>
      <div class="plugin-verify">
        <div class="plugin-verify-ok"><AppIcon name="check" />{{ updating ? t('plugins.confirm.checkedUpdate') : t('plugins.confirm.checkedInstall') }}</div>
        <div v-if="repositoryMatch === false" class="plugin-verify-warn">{{ updating ? t('plugins.confirm.mismatchUpdate') : t('plugins.confirm.mismatchInstall') }}</div>
        <p>{{ updating ? t('plugins.confirm.trustUpdate') : t('plugins.confirm.trustInstall') }}</p>
      </div>
      <details class="more plugin-details">
        <summary>{{ t('plugins.confirm.details') }}</summary>
        <dl class="plugin-preview-info">
          <template v-if="!uploaded">
            <dt>{{ t('plugins.confirm.fullUrl') }}</dt>
            <dd>{{ info.url }}</dd>
          </template>
          <template v-if="info.id">
            <dt>{{ t('plugins.confirm.id') }}</dt>
            <dd>{{ info.id }}</dd>
          </template>
          <dt>{{ t('list.col.size') }}</dt>
          <dd>{{ sizeText(info.size) }}</dd>
          <template v-if="info.icon_url || info.icon_error">
            <dt>{{ t('plugins.confirm.icon') }}</dt>
            <dd v-if="info.icon_error" :title="info.icon_url">{{ t('plugins.confirm.noIcon', { error: info.icon_error }) }}</dd>
            <dd v-else>{{ info.icon_url }}</dd>
          </template>
          <template v-if="pending.current">
            <dt>{{ t('plugins.confirm.currentSha') }}</dt>
            <dd><code>{{ pending.current.sha256 }}</code></dd>
            <dt>{{ t('plugins.confirm.newSha') }}</dt>
            <dd><code>{{ info.sha256 }}</code></dd>
          </template>
          <template v-else>
            <dt>{{ t('plugins.confirm.sha') }}</dt>
            <dd><code>{{ info.sha256 }}</code></dd>
          </template>
          <template v-if="info.icon_sha256">
            <dt>{{ t('plugins.confirm.iconSha') }}</dt>
            <dd><code>{{ info.icon_sha256 }}</code></dd>
          </template>
        </dl>
        <p>{{ t('plugins.confirm.fingerprintHelp') }}{{ repositoryMatch === true ? t('plugins.confirm.fingerprintMatch') : '' }}{{ updating ? t('plugins.confirm.noRunUpdate') : t('plugins.confirm.noRunInstall') }}</p>
      </details>
    </template>
    <template #footer>
      <div class="modal-actions">
        <NButton :disabled="loading" @click="pending = null">{{ t('common.cancel') }}</NButton>
        <NButton type="primary" :loading="loading" @click="pending && emit('confirm', pending)">{{ updating ? t('plugins.confirm.update') : t('plugins.confirm.install') }}</NButton>
      </div>
    </template>
  </NModal>
</template>
