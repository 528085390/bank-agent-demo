import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import QueryResultTable from './QueryResultTable.vue'

function item(index: number) {
  return {
    booked_date: `2026-05-${String((index % 28) + 1).padStart(2, '0')}`,
    amount_minor: 10000 + index,
    direction: index % 2 === 0 ? 'credit' : 'debit',
    transaction_type: '消费-餐饮',
    counterparty: `商户${index}`,
    category: '餐饮',
  }
}

function envelope(data: unknown): string {
  return JSON.stringify({ request_id: 'req_test', data, error: null })
}

function response(data: unknown): Response {
  return new Response(envelope(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('QueryResultTable', () => {
  it('renders the first page and loads the next page through the read-only endpoint', async () => {
    const firstPage = {
      total: 25,
      page: 1,
      page_size: 10,
      period_start: '2026-05-05',
      period_end: '2026-05-06',
      items: Array.from({ length: 10 }, (_, index) => item(index)),
    }
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      response({
        total: 25,
        page: 2,
        page_size: 10,
        items: Array.from({ length: 10 }, (_, index) => item(index + 10)),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(QueryResultTable, { props: { display: firstPage } })

    expect(wrapper.findAll('tbody tr')).toHaveLength(10)
    expect(wrapper.text()).toContain('共 25 笔')
    expect(wrapper.text()).toContain('第 1 / 3 页')

    await wrapper.findAll('.txn-page-btn')[1].trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledOnce()
    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/api/bill/transactions?')
    expect(url).toContain('page=2')
    expect(url).toContain('period_start=2026-05-05')
    expect(wrapper.text()).toContain('第 2 / 3 页')
    expect(wrapper.findAll('tbody tr')).toHaveLength(10)
  })

  it('pages a task\'s parsed transactions without any date range', async () => {
    // 上传卡片没有用户选的日期区间：翻页按「这份任务解析出来的流水」取，
    // 而不是拼 period_start/period_end。
    const firstPage = {
      total: 25,
      page: 1,
      page_size: 10,
      items: Array.from({ length: 10 }, (_, index) => item(index)),
    }
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      response({
        total: 25,
        page: 2,
        page_size: 10,
        items: Array.from({ length: 10 }, (_, index) => item(index + 10)),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(QueryResultTable, {
      props: { display: firstPage, taskId: 'task_abc' },
    })

    await wrapper.findAll('.txn-page-btn')[1].trigger('click')
    await flushPromises()

    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/api/bill/tasks/task_abc/transactions?')
    expect(url).toContain('page=2')
    expect(url).not.toContain('period_start')
    expect(wrapper.text()).toContain('第 2 / 3 页')
  })

  it('pages by the task id carried in the restored result', async () => {
    // 恢复出来的查询结果没有 period 的翻页能力，但带着任务号：直接按它翻页，
    // 不用重新问模型。
    const firstPage = {
      total: 25,
      page: 1,
      page_size: 10,
      task_id: 'task_restored',
      items: Array.from({ length: 10 }, (_, index) => item(index)),
    }
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      response({
        total: 25,
        page: 2,
        page_size: 10,
        items: Array.from({ length: 10 }, (_, index) => item(index + 10)),
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const wrapper = mount(QueryResultTable, { props: { display: firstPage, persisted: true } })
    // persisted 仍隐藏翻页按钮（快照语义）；换一份非 persisted 的来验证接口。
    const live = mount(QueryResultTable, { props: { display: firstPage } })

    await live.findAll('.txn-page-btn')[1].trigger('click')
    await flushPromises()

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/bill/tasks/task_restored/transactions?')
    expect(wrapper.find('.txn-pager').exists()).toBe(false)
  })

  it('does not show pager when the result fits one page', () => {
    const wrapper = mount(QueryResultTable, {
      props: {
        display: {
          total: 2,
          page: 1,
          page_size: 20,
          period_start: '2026-05-05',
          period_end: '2026-05-06',
          items: [item(1), item(2)],
        },
      },
    })

    expect(wrapper.findAll('tbody tr')).toHaveLength(2)
    expect(wrapper.find('.txn-pager').exists()).toBe(false)
  })

  it('renders a restored page as a static snapshot and tells the user to re-query', () => {
    const wrapper = mount(QueryResultTable, {
      props: {
        persisted: true,
        display: {
          total: 25,
          page: 1,
          page_size: 20,
          period_start: '2026-05-05',
          period_end: '2026-05-06',
          items: Array.from({ length: 20 }, (_, index) => item(index)),
        },
      },
    })

    expect(wrapper.find('.txn-pager').exists()).toBe(false)
    expect(wrapper.text()).toContain('已保留当前页 20 条')
    expect(wrapper.text()).toContain('刷新后仅保留当前页')
    expect(wrapper.text()).toContain('查看完整明细请再次查询')
  })
})
