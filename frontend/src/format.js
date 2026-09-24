import i18n, { intlLocale } from './i18n'

// Dates and numbers in the UI's language rather than the browser's, so a
// Vietnamese page never shows an American date.
export function formatDateTime(value) {
  return new Date(value).toLocaleString(intlLocale())
}

export function formatDate(value) {
  return new Date(value).toLocaleDateString(intlLocale())
}

export function formatTime(value) {
  return new Date(value).toLocaleTimeString(intlLocale())
}

export function formatNumber(value) {
  return Number(value).toLocaleString(intlLocale())
}

// A span of time, rounded to its largest unit: 45s, 3m, 2h, 5d (or the
// Vietnamese words). Callers wrap it in their own "… ago" / "in …".
export function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(Math.abs(seconds)))
  if (s < 60) return i18n.t('time.seconds', { count: s })
  if (s < 3600) return i18n.t('time.minutes', { count: Math.floor(s / 60) })
  if (s < 86400) return i18n.t('time.hours', { count: Math.floor(s / 3600) })
  return i18n.t('time.days', { count: Math.floor(s / 86400) })
}
