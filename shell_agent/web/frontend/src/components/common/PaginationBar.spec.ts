import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PaginationBar from './PaginationBar.vue'

describe('PaginationBar', () => {
  it('emits adjacent pages and disables unavailable directions', async () => {
    const wrapper = mount(PaginationBar, { props: { page: 2, totalPages: 3, total: 45 } })
    const buttons = wrapper.findAll('button')

    expect(wrapper.text()).toContain('共 45 条')
    expect(wrapper.text()).toContain('第 2 / 3 页')
    await buttons[0].trigger('click')
    await buttons[1].trigger('click')
    expect(wrapper.emitted('change')).toEqual([[1], [3]])
  })

  it('hides empty pagination and disables both buttons on a single page', () => {
    const empty = mount(PaginationBar, { props: { page: 1, totalPages: 1, total: 0 } })
    expect(empty.find('nav').exists()).toBe(false)

    const single = mount(PaginationBar, { props: { page: 1, totalPages: 1, total: 1 } })
    expect(single.findAll('button').every((button) => button.attributes('disabled') !== undefined)).toBe(true)
  })
})
