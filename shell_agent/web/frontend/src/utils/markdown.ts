import DOMPurify from 'dompurify'
import { marked } from 'marked'

const SAFE_MARKDOWN_TAGS = [
  'a', 'blockquote', 'br', 'code', 'del', 'em', 'h1', 'h2', 'h3', 'h4',
  'h5', 'h6', 'hr', 'li', 'ol', 'p', 'pre', 'strong', 'table', 'tbody',
  'td', 'th', 'thead', 'tr', 'ul',
]

/** Render trusted UI markup from untrusted Agent Markdown. */
export function renderAgentMarkdown(markdown: string): string {
  const rendered = marked.parse(markdown, {
    async: false,
    breaks: true,
    gfm: true,
  })
  return DOMPurify.sanitize(rendered, {
    ALLOWED_TAGS: SAFE_MARKDOWN_TAGS,
    ALLOWED_ATTR: ['align', 'href', 'title'],
  })
}
