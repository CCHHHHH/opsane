import { describe, expect, it } from 'vitest'

import { renderAgentMarkdown } from './markdown'

function fragment(markdown: string): HTMLDivElement {
  const container = document.createElement('div')
  container.innerHTML = renderAgentMarkdown(markdown)
  return container
}

describe('Agent Markdown renderer', () => {
  it('renders GFM headings, lists, fenced code and tables', () => {
    const output = fragment(`
# 发布检查

- 检查进程
- 检查日志

\`\`\`sh
systemctl status app
\`\`\`

| 项目 | 状态 |
| --- | --- |
| API | 正常 |
`)

    expect(output.querySelector('h1')?.textContent).toBe('发布检查')
    expect([...output.querySelectorAll('li')].map((item) => item.textContent)).toEqual(['检查进程', '检查日志'])
    expect(output.querySelector('pre code')?.textContent).toContain('systemctl status app')
    expect(output.querySelectorAll('table tbody tr')).toHaveLength(1)
    expect(output.querySelector('table tbody td')?.textContent).toBe('API')
  })

  it('strips executable HTML, event handlers and unsafe URL schemes', () => {
    const html = renderAgentMarkdown(`
<script>window.pwned = true</script>
<img src=x onerror="window.pwned = true">
<strong onclick="window.pwned = true">safe text</strong>
[unsafe link](javascript:alert)
<a href="javascript:alert(2)">raw unsafe link</a>
`)
    const output = document.createElement('div')
    output.innerHTML = html

    expect(output.querySelector('script')).toBeNull()
    expect(output.querySelector('img')).toBeNull()
    expect(output.querySelector('[onclick], [onerror]')).toBeNull()
    expect([...output.querySelectorAll('a')].every((link) => !link.getAttribute('href')?.startsWith('javascript:'))).toBe(true)
    expect(output.querySelector('a')?.hasAttribute('href')).toBe(false)
    expect(html).not.toMatch(/\s(?:onerror|onclick)=/i)
    expect(output.textContent).toContain('safe text')
  })
})
