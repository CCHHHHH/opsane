import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ConfigTip from './ConfigTip.vue'
import configTipSource from './ConfigTip.vue?raw'

describe('ConfigTip', () => {
  it('exposes the field explanation to pointer and keyboard users', () => {
    const text = '该配置用于控制命令执行超时。'
    const wrapper = mount(ConfigTip, {
      props: { text },
      global: { stubs: { Teleport: true } },
    })
    const trigger = wrapper.get('.config-tip')

    expect(trigger.attributes('tabindex')).toBe('0')
    expect(trigger.attributes('aria-label')).toBe(text)
    expect(trigger.attributes('aria-describedby')).toMatch(/^config-tip-/)
    expect(wrapper.get('[role="tooltip"]').text()).toBe(text)
  })

  it('renders above clipping containers and constrains the popover to the viewport', () => {
    expect(configTipSource).toContain('<Teleport to="body">')
    expect(configTipSource).toMatch(/position:\s*fixed/)
    expect(configTipSource).toContain('view.innerWidth - popoverRect.width - margin')
    expect(configTipSource).toContain('view.innerHeight - popoverRect.height - margin')
    expect(configTipSource).toContain("side.value = showBelow ? 'bottom' : 'top'")
  })
})
