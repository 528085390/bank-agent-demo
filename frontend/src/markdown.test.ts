/**
 * What the reply renderer must and must not do.
 *
 * The page renders a model's own text as HTML, so this file is mostly about the second half:
 * raw HTML stays text, dangerous link schemes never become `href`s, and a reply cannot make
 * the browser fetch a URL. The first half is what the bug report was about -- headings,
 * lists, bold, links, tables and code blocks have to render as themselves.
 */

import { describe, expect, it } from 'vitest'

import { renderMarkdown, sanitize } from './markdown'

describe('what renders as Markdown', () => {
  it('renders headings, lists, bold and inline code', () => {
    const html = renderMarkdown('## 产品\n\n- **稳健理财**\n- 灵活理财\n\n请查看 `p_demo_cash`。')

    expect(html).toContain('<h2>产品</h2>')
    expect(html).toContain('<strong>稳健理财</strong>')
    expect(html).toContain('<li>灵活理财</li>')
    expect(html).toContain('<code>p_demo_cash</code>')
  })

  it('renders a table', () => {
    const html = renderMarkdown('| 产品 | 收益 |\n| --- | --- |\n| 稳健理财 | 2.5% |')

    expect(html).toContain('<table>')
    expect(html).toContain('<th>产品</th>')
    expect(html).toContain('<td>稳健理财</td>')
  })

  it('keeps a code block literal, so a reply can show code as code', () => {
    const html = renderMarkdown('```json\n{"tool":"wealth_products"}\n```')

    expect(html).toContain('<pre>')
    expect(html).toContain('<code')
    // The braces and quotes survive as themselves: the block is not escaped a second time.
    expect(html).toContain('{"tool":"wealth_products"}')
  })

  it('keeps a single newline as a line break, the way the page always displayed it', () => {
    expect(renderMarkdown('第一行\n第二行')).toContain('第一行<br>')
  })

  it('renders an empty reply as nothing at all', () => {
    expect(renderMarkdown('')).toBe('')
  })
})

describe('what must never execute', () => {
  it('shows raw HTML as text instead of parsing it', () => {
    const html = renderMarkdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(2)>')

    expect(html).not.toContain('<script')
    expect(html).not.toContain('<img')
    expect(html).toContain('&lt;script&gt;')
  })

  it('refuses a javascript: link and keeps the text', () => {
    const html = renderMarkdown('[点我](javascript:alert(1))')

    // The source is shown as text -- it is data, not a link -- and no anchor is produced.
    expect(html).not.toContain('<a')
    expect(html).not.toContain('href=')
    expect(html).toContain('点我')
  })

  it('refuses a data: link', () => {
    const html = renderMarkdown('[点我](data:text/html;base64,PHNjcmlwdD4=)')

    expect(html).not.toContain('<a')
    expect(html).not.toContain('href=')
  })

  it('opens a real link in a new tab without handing over the opener', () => {
    const html = renderMarkdown('[产品页](https://example.com/products)')

    expect(html).toContain('href="https://example.com/products"')
    expect(html).toContain('target="_blank"')
    expect(html).toContain('rel="noopener noreferrer nofollow"')
  })

  it('linkifies a bare URL the model wrote', () => {
    const html = renderMarkdown('详见 https://example.com/help')

    expect(html).toContain('href="https://example.com/help"')
  })

  it('drops an attribute the reply tried to smuggle through the sanitizer', () => {
    // `html: false` means this never becomes a tag; the assertion is about the sanitizer's
    // own allowlist, which is the second gate if that setting is ever changed.
    expect(sanitize('<p style="position:fixed" onclick="alert(1)">x</p>')).toBe('<p>x</p>')
  })
})
