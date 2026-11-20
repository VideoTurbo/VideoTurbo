<script setup lang="ts">
// Qwen3-ASR 识别进度：每一遍五个步骤的进度格。
import type { WhisperjavPass, WhisperjavProgress } from '@/api/types'
import { duration } from '@/lib/format'
import { t } from '@/i18n'

const props = defineProps<{ progress: WhisperjavProgress; elapsed: number | undefined }>()

const STEPS = [t('asr.whisperjav.step.extract'), t('asr.whisperjav.step.scenes'), t('asr.whisperjav.step.vad'), t('asr.whisperjav.step.recognize'), t('asr.whisperjav.step.subtitles')]
const stateNames = { waiting: t('asr.whisperjav.state.waiting'), running: t('asr.whisperjav.state.running'), done: t('asr.whisperjav.state.done') }

interface Scenes { completed: number; total: number; percent: number; current: string }

/** 场景进度只在数字完整且合理时才展示。 */
function scenes(source: Pick<WhisperjavPass, 'completed_scenes' | 'total_scenes' | 'current_scene'>): Scenes | null {
  const completed = source.completed_scenes, total = source.total_scenes
  if (!Number.isInteger(completed) || !Number.isInteger(total)) return null
  if (total! <= 0 || completed! < 0 || completed! > total!) return null
  const scene = source.current_scene
  const current = Number.isInteger(scene) && scene! > 0 && scene! <= total! ? t('asr.whisperjav.currentScene', { scene }) : ''
  return { completed: completed!, total: total!, percent: (100 * completed!) / total!, current }
}
const scenesText = (s: Scenes) => t('asr.whisperjav.scenes', { completed: s.completed, total: s.total, current: s.current, percent: s.percent.toFixed(1) })

function passView(pass: WhisperjavPass, totalPasses: number) {
  const running = pass.state === 'running'
  const progress = running ? scenes(pass) : null
  const end = pass.finished_seconds ?? props.elapsed
  const spent = pass.started_seconds != null && end != null ? Math.max(0, end - pass.started_seconds) : null
  const step = running ? (pass.step ? t('asr.whisperjav.stepOf', { step: pass.step, stage: pass.stage || STEPS[pass.step - 1] }) : pass.stage || t('asr.whisperjav.starting')) : ''
  return {
    label: totalPasses > 1 ? t('asr.whisperjav.passOf', { pass: pass.pass_number, total: totalPasses }) : t('asr.whisperjav.singlePass'),
    state: `${stateNames[pass.state] || pass.state}${spent != null ? ` · ${duration(spent)}` : ''}`,
    detail: [step, progress ? scenesText(progress) : ''].filter(Boolean).join(' · '),
    steps: STEPS.map((name, index) => {
      const n = index + 1
      const status = pass.state === 'done' || (pass.step && n < pass.step) ? 'done' : pass.step === n ? 'active' : ''
      return { title: `${n}. ${name}`, status, fill: status === 'active' && progress ? `${progress.percent.toFixed(1)}%` : undefined }
    }),
  }
}
</script>

<template>
  <div class="wj">
    <template v-if="progress.passes?.length">
      <div class="info-head">
        <b>Qwen3-ASR · {{ (progress.total_passes || progress.passes.length) > 1 ? t('asr.whisperjav.dualPass') : t('asr.whisperjav.singlePass') }}</b>
        <span class="num">{{ t('asr.whisperjav.elapsed', { time: duration(elapsed) }) }}</span>
      </div>
      <div v-for="pass in progress.passes" :key="pass.pass_number" class="wj-pass" :class="pass.state">
        <template v-for="view in [passView(pass, progress.total_passes || progress.passes.length)]" :key="0">
          <b :title="pass.model">{{ view.label }}</b>
          <div class="wj-steps">
            <span v-for="step in view.steps" :key="step.title" class="wj-step" :class="step.status" :title="step.title" :style="step.fill ? { '--p': step.fill } : undefined" />
          </div>
          <span class="wj-state">{{ view.state }}</span>
          <div v-if="view.detail" class="wj-detail" :title="view.detail">{{ view.detail }}</div>
        </template>
      </div>
      <div v-if="(progress.total_passes || progress.passes.length) > 1 && progress.passes.every((pass) => pass.state === 'done')" class="wj-merge">
        {{ progress.stage || t('asr.whisperjav.merge') }}
      </div>
    </template>
  </div>
</template>
