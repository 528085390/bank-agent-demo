import { describe, expect, it } from 'vitest'
import {
  clampDay,
  composeDate,
  daysInMonth,
  MONTHS,
  todayParts,
  yearOptions,
  YEAR_SPAN,
} from './dateFields'

describe('dateFields', () => {
  describe('todayParts', () => {
    it('取的是本地年月日，月份从 1 开始', () => {
      expect(todayParts(new Date(2026, 9, 13))).toEqual({ year: 2026, month: 10, day: 13 })
    })

    it('跨年那天也对', () => {
      expect(todayParts(new Date(2025, 0, 1))).toEqual({ year: 2025, month: 1, day: 1 })
    })
  })

  describe('yearOptions', () => {
    it('到今年为止，不给未来', () => {
      const years = yearOptions(new Date(2026, 9, 13))

      expect(years[0]).toBe(2026 - YEAR_SPAN)
      expect(years.at(-1)).toBe(2026)
      expect(years).toHaveLength(YEAR_SPAN + 1)
    })
  })

  describe('daysInMonth', () => {
    it('大月小月', () => {
      expect(daysInMonth(2026, 1)).toBe(31)
      expect(daysInMonth(2026, 4)).toBe(30)
      expect(daysInMonth(2026, 12)).toBe(31)
    })

    it('二月看闰年', () => {
      expect(daysInMonth(2026, 2)).toBe(28)
      expect(daysInMonth(2024, 2)).toBe(29)
      expect(daysInMonth(2000, 2)).toBe(29)
      expect(daysInMonth(1900, 2)).toBe(28)
    })
  })

  describe('clampDay', () => {
    it('1月31日换到2月，天要落到当月最后一天', () => {
      expect(clampDay(2026, 2, 31)).toBe(28)
      expect(clampDay(2024, 2, 31)).toBe(29)
    })

    it('收得进当月就原样保留', () => {
      expect(clampDay(2026, 3, 31)).toBe(31)
      expect(clampDay(2026, 2, 15)).toBe(15)
    })
  })

  describe('composeDate', () => {
    it('拼成工具认得的写法', () => {
      expect(composeDate({ year: 2026, month: 9, day: 3 })).toBe('2026年9月3日')
    })
  })

  describe('MONTHS', () => {
    it('是 1 到 12', () => {
      expect([...MONTHS]).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])
    })
  })
})
