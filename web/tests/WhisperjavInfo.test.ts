import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import type { WhisperjavProgress } from '@/api/types'
import WhisperjavInfo from '@/components/WhisperjavInfo.vue'

const render = (progress: WhisperjavProgress, elapsed: number) => mount(WhisperjavInfo, { props: { progress, elapsed } })

describe('WhisperjavInfo', () => {
  it('shows each pass with its own steps and scene progress', () => {
    const wrapper = render({
      stage: 'x', total_passes: 2,
      passes: [
        { pass_number: 1, model: 'Qwen/Qwen3-ASR-1.7B', state: 'running', stage: '生成识别文本', step: 4, completed_scenes: 120, total_scenes: 281, current_scene: 121, started_seconds: 5 },
        { pass_number: 2, model: 'litagin/anime-whisper', state: 'running', stage: '语音检测', step: 3, completed_scenes: 30, total_scenes: 281, started_seconds: 5 },
      ],
    }, 605)
    const text = wrapper.text()
    expect(text).toContain('第 1/2 遍')
    expect(text).toContain('第 2/2 遍')
    expect(text).toContain('步骤 4/5 · 生成识别文本')
    expect(text).toContain('已处理场景 120/281 · 42.7% · 正在处理第 121 个')
    expect(text).toContain('已处理场景 30/281 · 10.7%')
    expect(text).toContain('进行中 · 10分0秒')
    expect(wrapper.findAll('.wj-step.done')).toHaveLength(3 + 2)
    expect(wrapper.findAll('.wj-step.active')).toHaveLength(2)
  })

  it('keeps a finished pass duration while the next pass waits', () => {
    const wrapper = render({
      stage: 'x', total_passes: 2,
      passes: [{ pass_number: 1, model: 'Qwen', state: 'done', started_seconds: 5, finished_seconds: 905 }, { pass_number: 2, model: 'anime', state: 'waiting' }],
    }, 1000)
    expect(wrapper.text()).toContain('已完成 · 15分0秒')
    expect(wrapper.text()).toContain('等待中')
    expect(wrapper.find('.wj-merge').exists()).toBe(false)
    expect(wrapper.find('.mini-progress').exists()).toBe(false)
  })

  it('shows the merge stage once both passes are done', () => {
    const wrapper = render({
      stage: '合并字幕 · Qwen 为主', total_passes: 2,
      passes: [{ pass_number: 1, model: 'Q', state: 'done' }, { pass_number: 2, model: 'A', state: 'done' }],
    }, 1000)
    expect(wrapper.find('.wj-merge').text()).toBe('合并字幕 · Qwen 为主')
  })

  it('labels a single pass and escapes the model name', () => {
    const wrapper = render({ stage: '提取音频', total_passes: 1, passes: [{ pass_number: 1, model: 'Q<x>', state: 'running', stage: '提取音频', step: 1 }] }, 3)
    expect(wrapper.text()).toContain('Qwen3-ASR · 单遍')
    expect(wrapper.text()).not.toContain('第 1/1 遍')
    expect(wrapper.find('.wj-pass b').attributes('title')).toBe('Q<x>')
    expect(wrapper.html()).not.toContain('<x>')
  })
})
