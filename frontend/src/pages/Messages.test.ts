import { describe, expect, it } from 'vitest'
import { detectOtp } from './Messages'

/**
 * `detectOtp` drives the copy-code button on every received message, so its
 * edge cases are user-visible: a wrong match means the operator copies an
 * order number instead of the verification code they came for.
 */
describe('detectOtp', () => {
  it('finds a plain verification code', () => {
    expect(detectOtp('【某平台】验证码 123456,5分钟内有效')).toBe('123456')
  })

  it('returns the LAST run when several are present', () => {
    // Documented behaviour ("the last 4–8 digit run"), not an accident: the
    // code usually trails the boilerplate. Pinned so a future refactor to
    // first-match has to be a deliberate decision.
    expect(detectOtp('验证码 1234,订单号 567890')).toBe('567890')
  })

  it('ignores digit runs that are too long or too short', () => {
    // A phone number must not match a prefix of itself — that is what the
    // lookbehind/lookahead in OTP_RE exist for.
    expect(detectOtp('你的号码是 13800138000')).toBeNull()
    expect(detectOtp('123')).toBeNull()
    expect(detectOtp('123456789')).toBeNull()
  })

  it('does not match across a decimal point', () => {
    expect(detectOtp('余额 12.34 元')).toBeNull()
  })

  it('returns null when there is nothing to copy', () => {
    expect(detectOtp('没有数字')).toBeNull()
    expect(detectOtp('')).toBeNull()
  })

  it('is not affected by the global regex flag across calls', () => {
    // OTP_RE is module-level and /g: a stateful lastIndex would make the
    // second identical call return something different.
    expect(detectOtp('code: 9999')).toBe('9999')
    expect(detectOtp('code: 9999')).toBe('9999')
  })
})
