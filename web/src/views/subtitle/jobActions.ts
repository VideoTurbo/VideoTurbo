// 字幕任务的单项操作：调用接口、提示结果、刷新状态。
import { api } from '@/api'
import { confirm, toast } from '@/lib/feedback'
import { reprocessNames, type ReprocessMode } from '@/lib/jobs'
import { refreshStatus } from '@/stores/status'
import { t } from '@/i18n'

async function run(action: () => Promise<unknown>, done: string, failed: string): Promise<void> {
  try {
    await action()
    toast.ok(done)
    await refreshStatus()
  } catch (error) {
    toast.error(failed, error)
  }
}

export const startJob = (id: string) => run(() => api.jobAction(id, 'start'), t('jobs.action.submitted'), t('jobs.action.submitFailed'))
export const skipJob = (id: string) => run(() => api.jobAction(id, 'skip'), t('jobs.action.skipped'), t('common.actionFailed'))
export const pauseJob = (id: string) => run(() => api.jobAction(id, 'pause'), t('jobs.action.paused'), t('jobs.action.pauseFailed'))
export const resumeJob = (id: string) => run(() => api.jobAction(id, 'resume'), t('jobs.action.resumed'), t('jobs.action.resumeFailed'))

export function togglePriority(id: string, current: number): Promise<void> {
  const next = current > 0 ? 0 : 100
  return run(() => api.jobAction(id, 'priority', { priority: next }), next ? t('jobs.action.prioritized') : t('jobs.action.normalPriority'), t('jobs.action.priorityFailed'))
}

export async function reprocessJob(id: string, mode: ReprocessMode): Promise<void> {
  if (mode !== 'continue') {
    const warning = mode === 'retranslate'
      ? t('jobs.action.retranslateWarning')
      : t('jobs.action.reprocessWarning')
    const ok = await confirm(t('jobs.action.reprocessConfirm', { warning, mode: reprocessNames[mode] }), { title: t('jobs.action.reprocessTitle'), confirmLabel: reprocessNames[mode] })
    if (!ok) return
  }
  await run(() => api.jobAction(id, 'reprocess', { mode }), t('jobs.action.reprocessSubmitted', { mode: reprocessNames[mode] }), t('jobs.action.reprocessFailed'))
}

export async function cancelJob(id: string): Promise<void> {
  const ok = await confirm(t('jobs.action.cancelConfirm'),
    { title: t('jobs.action.cancelTitle'), confirmLabel: t('common.confirmCancel'), danger: true })
  if (ok) await run(() => api.jobAction(id, 'cancel'), t('jobs.action.cancelled'), t('jobs.action.cancelFailed'))
}

export async function completeJob(id: string): Promise<void> {
  const ok = await confirm(t('jobs.action.completeConfirm'),
    { title: t('jobs.action.completeTitle'), confirmLabel: t('jobs.action.completeButton') })
  if (ok) await run(() => api.jobAction(id, 'complete'), t('jobs.action.completed'), t('jobs.action.completeFailed'))
}
