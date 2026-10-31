/**
 * Assistant replies are Markdown, and the page renders them as Markdown.
 *
 * Why this module exists: a reply reaches the page as the model's own text, and the message
 * component used to print it as pre-wrapped plain text. Nothing escaped it twice, nothing
 * turned newlines into a literal `\n`, and nothing wrapped it in a code fence -- `## 标题`,
 * `- 列表`, `**加粗**` and tables were simply shown as their source, because there was no
 * renderer at all.
 *
 * Rendering a model's output as HTML is the one place this page could be talked into
 * executing something, so the rules are deliberately narrow:
 *
 * - `html: false`: raw HTML in a reply is escaped and shown as text, never parsed. A
 *   `<script>` inside a product name is a string, not a tag.
 * - links are limited to `http`/`https`/`mailto`, and every rendered link opens in a new tab
 *   with `rel="noopener noreferrer nofollow"`. `javascript:` and `data:` never survive.
 * - the output is sanitized against an explicit allowlist of tags and attributes, so
 *   anything markdown-it is talked into emitting that is not on the list is dropped.
 * - images are not on the list: a reply cannot make the browser fetch a URL.
 */

import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'

/** The only link schemes a reply may produce. Everything else renders as plain text. */
const SAFE_LINK = /^(?:https?:|mailto:)/i

/**
 * What a reply may render as. `table`/`thead`/`tbody`/`tr`/`th`/`td` are here because GFM
 * tables are part of "normal Markdown"; `pre`/`code` keep code blocks literal, which is the
 * one place a reply's own punctuation must survive untouched.
 */
const ALLOWED_TAGS = [
  'p', 'br', 'hr', 'strong', 'em', 's', 'del', 'ins', 'mark',
  'blockquote', 'ul', 'ol', 'li',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'pre', 'code', 'a',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
]

/** `target`/`rel` are set by the renderer below, never by the reply's own text. */
const ALLOWED_ATTR = ['href', 'title', 'target', 'rel', 'align', 'class']

const markdown = new MarkdownIt({
  // Raw HTML is shown, not parsed: this is the difference between "the model wrote a tag"
  // and "the browser ran one".
  html: false,
  linkify: true,
  // A chat reply uses single newlines as line breaks, and that is what the page did before
  // it rendered Markdown. Keeping it means an existing reply does not reflow when rendered.
  breaks: true,
  typographer: false,
})

// `validateLink` is markdown-it's own gate, and it is replaced rather than extended: an
// allowlist cannot be widened by a scheme nobody thought of.
markdown.validateLink = (url: string) => SAFE_LINK.test(url.trim())

const defaultLinkOpen =
  markdown.renderer.rules.link_open
  ?? ((tokens, index, options, _env, self) => self.renderToken(tokens, index, options))

markdown.renderer.rules.link_open = (tokens, index, options, env, self) => {
  const token = tokens[index]
  token.attrSet('target', '_blank')
  token.attrSet('rel', 'noopener noreferrer nofollow')
  return defaultLinkOpen(tokens, index, options, env, self)
}

/**
 * Rendering is pure but not free, and the view re-renders every message on every streamed
 * event. A small bounded cache keeps that from becoming quadratic in the transcript length;
 * the bound is what stops a long session from growing the map without limit.
 */
const CACHE_LIMIT = 200
const cache = new Map<string, string>()

/** The sanitizer's own rules, in one place, so the allowlist cannot drift from the call. */
export function sanitize(html: string): string {
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    // A link that survives sanitizing is one markdown-it already validated; forbidding
    // these again here means neither layer alone is the whole defence.
    FORBID_ATTR: ['style', 'srcset'],
    ALLOW_DATA_ATTR: false,
  })
}

/** One reply as HTML. Empty input renders nothing rather than an empty paragraph. */
export function renderMarkdown(source: string): string {
  if (!source) return ''
  const cached = cache.get(source)
  if (cached !== undefined) return cached
  const html = sanitize(markdown.render(source))
  if (cache.size >= CACHE_LIMIT) cache.clear()
  cache.set(source, html)
  return html
}
