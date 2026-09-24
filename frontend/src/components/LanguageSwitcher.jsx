import { useTranslation } from 'react-i18next'
import { LANGUAGES } from '../i18n'

// VI | EN, as a two-button segmented control. Sits in the header on every
// page and on the sign-in card, which is outside that header.
export default function LanguageSwitcher({ className = '' }) {
  const { i18n, t } = useTranslation()
  return (
    <div
      role="group"
      aria-label={t('common.language')}
      className={`inline-flex items-center rounded-full bg-gray-100 p-0.5 text-[11px] font-semibold tracking-wide ${className}`}
    >
      {LANGUAGES.map((lng) => {
        const active = i18n.language === lng
        return (
          <button
            key={lng}
            type="button"
            onClick={() => i18n.changeLanguage(lng)}
            aria-pressed={active}
            title={t(`common.language_names.${lng}`)}
            className={`h-6 min-w-[2.25rem] px-2 rounded-full uppercase transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${
              active
                ? 'bg-white text-gray-900 shadow-sm ring-1 ring-black/5'
                : 'text-gray-500 hover:text-gray-800'
            }`}
          >
            {lng}
          </button>
        )
      })}
    </div>
  )
}
