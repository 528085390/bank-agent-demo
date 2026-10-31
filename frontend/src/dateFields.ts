/**
 * 追问卡片里「年 / 月 / 日」三个下拉框的取值。
 *
 * 为什么不是 `<input type="date">`：原生控件的占位符由浏览器按**自己的语言**渲染，
 * 页面 `<html lang="zh-CN">` 管不了它（实际显示过 `yyyy/mm/dd`）。三个下拉框里写的就是
 * 「2026 年 / 9 月 / 13 日」，所见即所得。
 *
 * 拼出来的字符串交给 agent，再交给工具的日期解析（后端 `app/modules/dates.py`），
 * 它认得 `2026年9月13日` 这种写法。
 */

export interface DateParts {
  year: number
  month: number
  day: number
}

/** 年下拉框往前给多少年。分析的是已经发生的流水，所以到今年为止，不给未来。 */
export const YEAR_SPAN = 10

/** 年下拉框的选项，从老到新。 */
export function yearOptions(now: Date = new Date()): number[] {
  const current = now.getFullYear()
  return Array.from({ length: YEAR_SPAN + 1 }, (_, index) => current - YEAR_SPAN + index)
}

/** 某年某月有多少天。`month` 从 1 开始 —— 第 0 天就是上个月的最后一天。 */
export function daysInMonth(year: number, month: number): number {
  return new Date(year, month, 0).getDate()
}

/** 今天。三个下拉框打开时默认选的就是它。 */
export function todayParts(now: Date = new Date()): DateParts {
  return { year: now.getFullYear(), month: now.getMonth() + 1, day: now.getDate() }
}

/** 把天数收进当月范围：1 月 31 日把月份改成 2 月之后，天要落到 28 / 29。 */
export function clampDay(year: number, month: number, day: number): number {
  return Math.min(day, daysInMonth(year, month))
}

/** 拼成工具认得的写法：`2026年9月13日`。 */
export function composeDate(parts: DateParts): string {
  return `${parts.year}年${parts.month}月${parts.day}日`
}

/** 12 个月的选项。 */
export const MONTHS: readonly number[] = Array.from({ length: 12 }, (_, index) => index + 1)
