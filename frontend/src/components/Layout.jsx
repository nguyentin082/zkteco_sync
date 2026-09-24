import { Outlet, NavLink, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import Brand from './Brand'
import LanguageSwitcher from './LanguageSwitcher'
import { api } from '../api'
import { useAuth } from '../auth'

const tabs = [
  { key: 'devices', to: '/devices' },
  { key: 'employees', to: '/employees' },
  { key: 'attendance', to: '/attendance' },
  { key: 'settings', to: '/settings' },
  { key: 'users', to: '/users', adminOnly: true },
]

export default function Layout() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { user, refresh } = useAuth()
  const visibleTabs = tabs.filter((tab) => !tab.adminOnly || user?.role === 'admin')

  async function logout() {
    // Revoke server-side first; the cookie alone means nothing afterwards.
    try {
      await api.auth.logout()
    } finally {
      await refresh()
      navigate('/login', { replace: true })
    }
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <header className="bg-white border-b border-gray-200">
        <div className="max-w-6xl mx-auto px-6">
          {/* Top bar */}
          <div className="flex items-center justify-between h-14">
            <Brand nameClassName="text-base" />
            <div className="flex items-center gap-4">
              <LanguageSwitcher />
              <span className="text-sm text-gray-500">{user?.username}</span>
              {/* The app's secondary button in red: signing out is a leaving
                  action, and this one sits on every page, so it is outlined
                  rather than filled — the same restraint the destructive
                  actions on the pages show. */}
              <button
                onClick={logout}
                className="px-3 py-1.5 rounded-lg border border-red-200 text-sm font-medium text-red-600 hover:bg-red-50 transition-colors"
              >
                {t('common.logout')}
              </button>
            </div>
          </div>

          {/* Tabs */}
          <nav className="flex gap-1">
            {visibleTabs.map((tab) => (
              <NavLink
                key={tab.to}
                to={tab.to}
                className={({ isActive }) =>
                  `px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                    isActive
                      ? 'border-blue-600 text-blue-600'
                      : 'border-transparent text-gray-500 hover:text-gray-800'
                  }`
                }
              >
                {t(`nav.${tab.key}`)}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-8">
        <Outlet />
      </main>
    </div>
  )
}
