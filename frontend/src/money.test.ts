/**
 * Money on screen.
 *
 * The one property that matters: the number the server sent is the number the user reads,
 * to the cent, with no float in between.
 */

import { describe, expect, it } from 'vitest'

import { formatMinor } from './money'

describe('formatMinor', () => {
  it('renders minor units the way the server does', () => {
    expect(formatMinor(200000)).toBe('2,000.00 元')
    expect(formatMinor(832000)).toBe('8,320.00 元')
    expect(formatMinor(50)).toBe('0.50 元')
    expect(formatMinor(1)).toBe('0.01 元')
    expect(formatMinor(0)).toBe('0.00 元')
    expect(formatMinor(123456789)).toBe('1,234,567.89 元')
  })

  it('keeps a negative amount negative', () => {
    expect(formatMinor(-200000)).toBe('-2,000.00 元')
  })

  it('renders nothing for a value that is not an integer number of fen', () => {
    expect(formatMinor(undefined)).toBe('')
    expect(formatMinor(null)).toBe('')
    expect(formatMinor('200000')).toBe('')
    expect(formatMinor(2000.5)).toBe('')
    expect(formatMinor(Number.NaN)).toBe('')
  })
})
