import { describe, expect, it } from 'vitest'
import {
  analysisReportView,
  isAnalysisReport,
  reportCashflow,
  shouldPromptSend,
  ITEMS_PER_PAGE,
} from './analysisResult'

describe('analysisResult', () => {
  const mockReport = {
    report_meta: {
      date_range: { start: '2024-09-01', end: '2024-09-30' },
      accounts: ['主账户'],
      total_transactions: 89,
      generated_at: '2024-10-12T10:00:00Z',
    },
    modules: [
      {
        module_id: 'customer_risk_profile',
        title: '客户基础信息与风险标签',
        data: {
          risk_tags: [
            { tag: '网贷', hit_count: 5, amount_minor: 50000, confidence: 0.85 },
          ],
          completeness_score: 95,
        },
        insights: [
          { text: '疑似存在网贷类交易特征', type: '推断' as const, confidence: 0.85 },
        ],
      },
      {
        module_id: 'hourly_activity',
        title: '交易活跃时段分析',
        data: {
          night_stats: { count: 15, percentage: 16.8, consecutive_days: 5 },
        },
        insights: [
          { text: '夜间交易占比偏高', type: '推断' as const, confidence: 0.8 },
        ],
      },
    ],
    text_report: '# 账单深度分析报告\n...',
  }

  describe('isAnalysisReport', () => {
    it('识别有效的分析报告', () => {
      expect(isAnalysisReport(mockReport)).toBe(true)
    })

    it('拒绝无效输入', () => {
      expect(isAnalysisReport(null)).toBe(false)
      expect(isAnalysisReport({})).toBe(false)
      expect(isAnalysisReport({ modules: [] })).toBe(false)
    })
  })

  describe('analysisReportView', () => {
    it('正确转换报告', () => {
      const view = analysisReportView(mockReport)

      expect(view.meta.period).toBe('2024-09-01 ~ 2024-09-30')
      expect(view.meta.totalTransactions).toBe(89)
      expect(view.modules).toHaveLength(2)
      expect(view.totalPages).toBe(Math.ceil(2 / ITEMS_PER_PAGE))
    })

    it('模块包含标题和洞察', () => {
      const view = analysisReportView(mockReport)
      const firstModule = view.modules[0]

      expect(firstModule.title).toBe('客户基础信息与风险标签')
      expect(firstModule.insights).toHaveLength(1)
      expect(firstModule.insights[0].text).toBe('疑似存在网贷类交易特征')
    })

    it('格式化模块数据', () => {
      const view = analysisReportView(mockReport)
      const firstModule = view.modules[0]

      expect(firstModule.dataRows.length).toBeGreaterThan(0)
      expect(firstModule.dataRows[0].label).toContain('网贷')
    })
  })

  describe('shouldPromptSend', () => {
    const idle = {
      ready: true, busy: false, locked: false, prompted: false, sent: false, alreadyPrompted: false,
    }

    it('报告就绪且这一轮空闲时弹确认框', () => {
      expect(shouldPromptSend(idle)).toBe(true)
    })

    it('同一份报告问过就不再问 —— live 块和结算后的副本是两个实例', () => {
      expect(shouldPromptSend({ ...idle, alreadyPrompted: true })).toBe(false)
    })

    it('这一轮还在跑时不弹 —— 那时点了「是，发送」会被静默丢掉', () => {
      // 工具结果是在流中途就出现的，这个窗口里父组件还在用 send。
      expect(shouldPromptSend({ ...idle, busy: true })).toBe(false)
    })

    it('有待答问题时也不弹：那时输入都被锁着', () => {
      expect(shouldPromptSend({ ...idle, locked: true })).toBe(false)
    })

    it('弹过一次就不再弹', () => {
      expect(shouldPromptSend({ ...idle, prompted: true })).toBe(false)
    })

    it('这一轮已经发成功了就不再问 —— 刚办完的事别让人再确认一遍', () => {
      expect(shouldPromptSend({ ...idle, sent: true })).toBe(false)
    })

    it('还不是一份分析报告时不弹', () => {
      expect(shouldPromptSend({ ...idle, ready: false })).toBe(false)
    })
  })

  describe('reportCashflow', () => {
    it('优先使用后端给的两套口径', () => {
      const report = {
        ...mockReport,
        report_meta: {
          ...mockReport.report_meta,
          cashflow: {
            all: { income_minor: 1108888, expense_minor: 485875, net_minor: 623013 },
            external: { income_minor: 908888, expense_minor: 57830, net_minor: 851058 },
          },
        },
      }

      const flow = reportCashflow(report as any)

      expect(flow.all).toEqual({ income_minor: 1108888, expense_minor: 485875, net_minor: 623013 })
      expect(flow.external.net_minor).toBe(851058)
    })

    it('旧报告没有 cashflow 时从模块回退汇总', () => {
      const report = {
        ...mockReport,
        modules: [
          ...mockReport.modules,
          {
            module_id: 'account_usage',
            title: '账户使用',
            data: { accounts: [{ income_minor: 1108888, expense_minor: 485875 }] },
            insights: [],
          },
          {
            module_id: 'monthly_cashflow',
            title: '月度收支趋势',
            data: { monthly_data: [{ income_minor: 908888, expense_minor: 57830 }] },
            insights: [],
          },
        ],
      }

      const flow = reportCashflow(report as any)

      expect(flow.all).toEqual({ income_minor: 1108888, expense_minor: 485875, net_minor: 623013 })
      expect(flow.external).toEqual({ income_minor: 908888, expense_minor: 57830, net_minor: 851058 })
    })
  })
})
