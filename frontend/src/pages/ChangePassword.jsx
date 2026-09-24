import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { useAuth } from '../auth'
import LanguageSwitcher from '../components/LanguageSwitcher'

export default function ChangePassword() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { user, refresh } = useAuth()
  const forced = !!user?.must_change_password
  const [form, setForm] = useState({ current: '', next: '', confirm: '' })
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    if (form.next !== form.confirm) {
      setError(t('auth.passwords_do_not_match'))
      return
    }
    setSaving(true)
    try {
      await api.auth.changePassword(form.current, form.next)
      // Every other session of this account was just revoked server-side.
      await refresh()
      navigate('/devices', { replace: true })
    } catch (err) {
      setError(err.message)
      setSaving(false)
    }
  }

  return (
    <div className="min-h-screen bg-gray-100 flex items-center justify-center">
      <div className="bg-white rounded-2xl shadow-lg w-full max-w-sm p-8">
        <div className="flex justify-end -mt-4 -mr-4 mb-2">
          <LanguageSwitcher />
        </div>
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold text-gray-900">{t('auth.change_password')}</h1>
          <p className="text-sm text-gray-500 mt-1">
            {forced
              ? t('auth.setup_password_notice')
              : t('auth.signed_in_as', { username: user?.username || '' })}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              {t('auth.current_password')}
            </label>
            <input
              type="password"
              required
              autoFocus
              value={form.current}
              onChange={(e) => setForm({ ...form, current: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              {t('auth.new_password')}
            </label>
            <input
              type="password"
              required
              minLength={8}
              value={form.next}
              onChange={(e) => setForm({ ...form, next: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="text-xs text-gray-400 mt-1">{t('auth.min_length', { min: 8 })}</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              {t('auth.confirm_new_password')}
            </label>
            <input
              type="password"
              required
              value={form.confirm}
              onChange={(e) => setForm({ ...form, confirm: e.target.value })}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          {error && (
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={saving}
            className="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white font-medium rounded-lg px-4 py-2 text-sm transition-colors"
          >
            {saving ? t('common.saving') : t('auth.change_password')}
          </button>
        </form>
      </div>
    </div>
  )
}
