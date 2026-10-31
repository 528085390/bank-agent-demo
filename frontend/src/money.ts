/**
 * Minor units (分) as the amount a user reads.
 *
 * The server sends exact integers and a confirmation box is the last place a rounding
 * mistake is acceptable, so this is integer arithmetic on the digits rather than a division:
 * 200000 分 is 2,000.00 元, never 1999.9999999999998. The spelling matches the server's own
 * `format_minor` (backend/app/modules/money.py), so the amount on the card and the amount in
 * the preview text are the same string.
 */

export function formatMinor(amountMinor: unknown): string {
  if (typeof amountMinor !== 'number' || !Number.isInteger(amountMinor)) return ''
  const sign = amountMinor < 0 ? '-' : ''
  const digits = String(Math.abs(amountMinor)).padStart(3, '0')
  const yuan = digits.slice(0, -2).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  return `${sign}${yuan}.${digits.slice(-2)} 元`
}
