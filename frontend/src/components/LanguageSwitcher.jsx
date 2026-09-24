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
      className={`inline-flex rounded-lg border border-gray-200 p-0.5 text-xs font-medium ${className}`}
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
            className={`px-2 py-1 rounded-md uppercase transition-colors ${
              active ? 'bg-blue-600 text-white' : 'text-gray-500 hover:text-gray-800'
            }`}
          >
            {lng}
          </button>
        )
      })}
    </div>
  )
}
