// 展示专用：直接按十进制文本四舍五入，既不经过浮点数，也不改写计算/存档。
export function formatDecimal(value: string): string {
  const match = /^([+-]?)(\d+)(?:\.(\d*))?(?:[eE]([+-]?\d+))?$/.exec(value)
  if (!match || (!value.includes('.') && !/[eE]/.test(value))) return value
  const sign = match[1]!
  const integer = match[2]!
  const fraction = match[3] ?? ''
  const exponent = Number(match[4] ?? '0')
  if (!Number.isSafeInteger(exponent) || Math.abs(exponent) > 1000) return value
  const digits = integer + fraction
  const point = integer.length + exponent
  const whole = point <= 0 ? '0' : digits.slice(0, point).padEnd(point, '0')
  const rest = point <= 0 ? '0'.repeat(-point) + digits : digits.slice(point)
  let cents = BigInt(whole + rest.padEnd(3, '0').slice(0, 2))
  if (rest.padEnd(3, '0')[2]! >= '5') cents += 1n
  const padded = cents.toString().padStart(3, '0')
  const tail = padded.slice(-2).replace(/0+$/, '')
  const amount = padded.slice(0, -2) + (tail ? '.' + tail : '')
  return (cents === 0n ? '' : sign) + amount
}

export function presentationValue(value: unknown): unknown {
  if (typeof value === 'string') return formatDecimal(value)
  if (typeof value === 'number' && !Number.isInteger(value)) return formatDecimal(String(value))
  if (Array.isArray(value)) return value.map(presentationValue)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, entry]) => [key, presentationValue(entry)]))
  }
  return value
}
