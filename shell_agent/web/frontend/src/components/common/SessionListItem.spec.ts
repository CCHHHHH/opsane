import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SessionListItem from './SessionListItem.vue'

const session = {
  id: 'session-1', type: 'chat' as const, title: '原会话', updated_at: '2026-07-15', pinned_at: null,
}

describe('SessionListItem', () => {
  it('supports inline rename from the action menu', async () => {
    const wrapper = mount(SessionListItem, { props: { session } })

    await wrapper.find('.session-menu-trigger').trigger('click')
    await wrapper.findAll('.session-menu button')[0].trigger('click')
    const input = wrapper.find<HTMLInputElement>('.session-rename input')
    await input.setValue('新的会话名称')
    await wrapper.find('.session-rename').trigger('submit')

    expect(wrapper.emitted('rename')?.[0]).toEqual(['新的会话名称'])
  })

  it('toggles pin state and exposes the pinned marker', async () => {
    const wrapper = mount(SessionListItem, { props: { session: { ...session, pinned_at: '2026-07-15T10:00:00' } } })

    expect(wrapper.find('.session-pin-mark').exists()).toBe(true)
    await wrapper.find('.session-menu-trigger').trigger('click')
    await wrapper.findAll('.session-menu button')[1].trigger('click')

    expect(wrapper.emitted('pin')?.[0]).toEqual([false])
  })
})
