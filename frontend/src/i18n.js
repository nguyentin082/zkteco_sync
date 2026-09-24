import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

// Every locales/<lang>/<section>.json becomes t('<section>.<key>') — one file
// per page keeps the two languages easy to diff side by side.
const modules = import.meta.glob('./locales/*/*.json', { eager: true, import: 'default' })
const resources = {}
for (const [path, data] of Object.entries(modules)) {
  const [, lang, section] = path.match(/\.\/locales\/([^/]+)\/([^/]+)\.json$/)
  resources[lang] ??= { translation: {} }
  resources[lang].translation[section] = data
}

export const LANGUAGES = ['vi', 'en']
const STORAGE_KEY = 'lang'

// Vietnamese unless this browser picked English before. Storage can throw in
// a locked-down browser; the default still stands then.
function initialLanguage() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (LANGUAGES.includes(saved)) return saved
  } catch {
    // no storage — fall through to the default
  }
  return 'vi'
}

i18n.on('languageChanged', (lng) => {
  document.documentElement.lang = lng
  try {
    localStorage.setItem(STORAGE_KEY, lng)
  } catch {
    // not remembered across reloads, but still switched for this one
  }
})

i18n.use(initReactI18next).init({
  resources,
  lng: initialLanguage(),
  fallbackLng: 'en',
  // React already escapes what it renders.
  interpolation: { escapeValue: false },
  returnNull: false,
})

// The Intl locale matching the UI language, for dates and numbers.
export function intlLocale() {
  return i18n.language === 'en' ? 'en-US' : 'vi-VN'
}

// Params may carry fragments — {code, params} — for a sentence the server
// built from parts; each becomes its own translated text first.
function resolveParams(params) {
  const out = {}
  for (const [key, value] of Object.entries(params || {})) {
    if (value && typeof value === 'object' && 'code' in value) {
      out[key] = value.code ? i18n.t(`messages.${value.code}`, resolveParams(value.params)) : ''
    } else {
      out[key] = value
    }
  }
  return out
}

// A server refusal in the UI's language: the error's `code` is looked up
// under errors.*, and a code this build has no text for falls back to the
// server's own English `detail`.
export function translateError(code, params, detail) {
  if (code && i18n.exists(`errors.${code}`)) return i18n.t(`errors.${code}`, resolveParams(params))
  if (typeof detail === 'string' && detail) return detail
  return i18n.t('errors.common.request_failed')
}

// The same for a success answer's `message`: messages.<message_code> when
// this build knows it, else the server's English, else `fallback`.
export function serverMessage(result, fallback = '') {
  const code = result?.message_code
  if (code && i18n.exists(`messages.${code}`)) {
    return i18n.t(`messages.${code}`, resolveParams(result.message_params))
  }
  return result?.message || fallback
}

export default i18n
