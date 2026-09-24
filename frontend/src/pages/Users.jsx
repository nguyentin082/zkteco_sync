import { useState, useEffect, useCallback } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import { api } from '../api'
import { formatDateTime } from '../format'
import { useAuth } from '../auth'
import KebabMenu from '../components/KebabMenu'

const ROLES = ['admin', 'viewer']

function RoleBadge({ role }) {
  const { t } = useTranslation()
  return (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium ${
        role === 'admin' ? 'bg-purple-100 text-purple-700' : 'bg-gray-100 text-gray-600'
      }`}
    >
      {t(`users.roles.${role}`, { defaultValue: role })}
    </span>
  )
}

function StatusBadge({ active }) {
  const { t } = useTranslation()
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium ${
        active ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
      }`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${active ? 'bg-green-500' : 'bg-gray-400'}`} />
      {active ? t('users.active') : t('users.inactive')}
    </span>
  )
}

function Toast({ message, type, onDismiss }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 3500)
    return () => clearTimeout(t)
  }, [onDismiss])

  return (
    <div
      className={`fixed bottom-6 right-6 px-4 py-3 rounded-lg shadow-lg text-sm font-medium text-white z-50 ${
        type === 'error' ? 'bg-red-600' : 'bg-gray-900'
      }`}
    >
      {message}
    </div>
  )
}

function Field({ label, required, hint, children }) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-700 mb-1">
        {label}
        {required && <span className="text-red-500 ml-0.5">*</span>}
      </label>
      {children}
      {hint && <p className="text-xs text-gray-400 mt-1">{hint}</p>}
    </div>
  )
}

function UserFormModal({ mode, user, isSelf, onSave, onClose }) {
  const { t } = useTranslation()
  const isEdit = mode === 'edit'
  const [form, setForm] = useState({ username: '', full_name: '', password: '', role: 'viewer' })
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (isEdit && user) {
      setForm({ username: user.username, full_name: user.full_name || '', password: '', role: user.role })
    }
  }, [isEdit, user])

  function set(field, value) {
    setForm((f) => ({ ...f, [field]: value }))
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setSaving(true)
    try {
      const payload = isEdit
        ? { full_name: form.full_name || null, role: form.role }
        : { username: form.username, full_name: form.full_name || null, password: form.password, role: form.role }
      await onSave(payload)
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/40" />
      <div className="relative bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-gray-900">{isEdit ? t('users.edit_title') : t('users.add_title')}</h2>
          <button type="button" onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors">
            ✕
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <Field label={t('auth.username')} required>
            <input
              type="text"
              required
              disabled={isEdit}
              value={form.username}
              onChange={(e) => set('username', e.target.value)}
              className="input disabled:bg-gray-100 disabled:text-gray-400"
            />
          </Field>

          <Field label={t('users.full_name')}>
            <input
              type="text"
              value={form.full_name}
              onChange={(e) => set('full_name', e.target.value)}
              className="input"
            />
          </Field>

          {!isEdit && (
            <Field label={t('users.setup_password')} required hint={t('users.setup_password_hint')}>
              <input
                type="password"
                required
                minLength={8}
                value={form.password}
                onChange={(e) => set('password', e.target.value)}
                className="input"
              />
            </Field>
          )}

          <Field label={t('users.role')} required hint={isSelf ? t('users.cannot_change_own_role') : undefined}>
            <select
              disabled={isSelf}
              value={form.role}
              onChange={(e) => set('role', e.target.value)}
              className="input disabled:bg-gray-100 disabled:text-gray-400"
            >
              {ROLES.map((r) => (
                <option key={r} value={r}>{t(`users.roles.${r}`)}</option>
              ))}
            </select>
          </Field>

          {error && (
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</p>
          )}

          <div className="flex gap-3 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 border border-gray-300 text-gray-700 hover:bg-gray-50 text-sm font-medium py-2 rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium py-2 rounded-lg transition-colors"
            >
              {saving ? t('common.saving') : isEdit ? t('common.save_changes') : t('users.add_title')}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

function ResetPasswordModal({ user, onSave, onClose }) {
  const { t } = useTranslation()
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setSaving(true)
    try {
      await onSave(password)
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/40" />
      <div className="relative bg-white rounded-2xl shadow-xl w-full max-w-sm mx-4 p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-gray-900">{t('users.reset_password')}</h2>
          <button type="button" onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors">
            ✕
          </button>
        </div>

        <p className="text-sm text-gray-500 mb-4">
          <Trans
            i18nKey="users.reset_intro"
            values={{ username: user.username }}
            components={{ b: <span className="font-medium text-gray-700" /> }}
          />
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <Field label={t('auth.new_password')} required>
            <input
              type="password"
              required
              minLength={8}
              autoFocus
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="input"
            />
          </Field>

          {error && (
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">{error}</p>
          )}

          <div className="flex gap-3">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 border border-gray-300 text-gray-700 hover:bg-gray-50 text-sm font-medium py-2 rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={saving}
              className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium py-2 rounded-lg transition-colors"
            >
              {saving ? t('common.saving') : t('users.reset_password')}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

export default function Users() {
  const { t } = useTranslation()
  const { user: me } = useAuth()
  const [users, setUsers] = useState([])
  const [loading, setLoading] = useState(true)
  const [modal, setModal] = useState(null) // { mode: 'create' | 'edit', user? }
  const [resetTarget, setResetTarget] = useState(null)
  const [toast, setToast] = useState(null)

  const showToast = useCallback((message, type = 'success') => setToast({ message, type }), [])
  const dismissToast = useCallback(() => setToast(null), [])

  const loadUsers = useCallback(async () => {
    try {
      setUsers(await api.users.list())
    } catch (err) {
      showToast(err.message || t('users.load_failed'), 'error')
    } finally {
      setLoading(false)
    }
  }, [showToast, t])

  useEffect(() => {
    loadUsers()
  }, [loadUsers])

  async function handleSave(formData) {
    if (modal.mode === 'create') {
      await api.users.create(formData)
      showToast(t('users.created'))
    } else {
      await api.users.update(modal.user.id, formData)
      showToast(t('users.updated'))
    }
    setModal(null)
    loadUsers()
  }

  async function handleResetPassword(newPassword) {
    await api.users.resetPassword(resetTarget.id, newPassword)
    showToast(t('users.password_reset_for', { username: resetTarget.username }))
    setResetTarget(null)
    loadUsers()
  }

  async function handleToggleActive(u) {
    try {
      await api.users.update(u.id, { is_active: !u.is_active })
      showToast(u.is_active ? t('users.deactivated', { username: u.username }) : t('users.activated', { username: u.username }))
      loadUsers()
    } catch (err) {
      showToast(err.message, 'error')
    }
  }

  async function handleDelete(u) {
    if (!confirm(t('users.confirm_delete', { username: u.username }))) return
    try {
      await api.users.delete(u.id)
      showToast(t('users.deleted'))
      loadUsers()
    } catch (err) {
      showToast(err.message, 'error')
    }
  }

  function menuItems(u) {
    // SessionOut (GET /auth/me) carries no numeric id, only username — that's
    // the only stable field we can compare a row against to know it's "you".
    const isSelf = u.username === me?.username
    return [
      { label: t('common.edit'), onClick: () => setModal({ mode: 'edit', user: u }) },
      { label: t('users.reset_password'), onClick: () => setResetTarget(u) },
      {
        label: u.is_active ? t('users.deactivate') : t('users.activate'),
        danger: u.is_active,
        disabled: isSelf && u.is_active,
        onClick: () => handleToggleActive(u),
      },
      'divider',
      { label: t('common.delete'), danger: true, disabled: isSelf, onClick: () => handleDelete(u) },
    ]
  }

  function formatDate(iso) {
    if (!iso) return '—'
    return formatDateTime(iso)
  }

  return (
    <>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-gray-900">{t('nav.users')}</h1>
        <button
          onClick={() => setModal({ mode: 'create' })}
          className="bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
        >
          + {t('users.add_title')}
        </button>
      </div>

      <div className="bg-white rounded-xl border border-gray-200">
        {loading ? (
          <div className="p-12 text-center text-sm text-gray-400">{t('common.loading')}</div>
        ) : users.length === 0 ? (
          <div className="p-12 text-center text-sm text-gray-400">{t('users.empty')}</div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 bg-gray-50 [&>th:first-child]:rounded-tl-xl [&>th:last-child]:rounded-tr-xl">
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('auth.username')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('users.name')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('users.role')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('users.status')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('users.last_login')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr
                  key={u.id}
                  className="border-b border-gray-100 last:border-0 hover:bg-gray-50 transition-colors"
                >
                  <td className="px-4 py-3 font-medium text-gray-900">
                    {u.username}
                    {u.username === me?.username && <span className="text-gray-400 font-normal"> ({t('users.you')})</span>}
                  </td>
                  <td className="px-4 py-3 text-gray-500">
                    {u.full_name || <span className="text-gray-400">—</span>}
                  </td>
                  <td className="px-4 py-3"><RoleBadge role={u.role} /></td>
                  <td className="px-4 py-3"><StatusBadge active={u.is_active} /></td>
                  <td className="px-4 py-3 text-gray-400 text-xs">{formatDate(u.last_login_at)}</td>
                  <td className="px-4 py-3 text-right">
                    <KebabMenu items={menuItems(u)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {modal && (
        <UserFormModal
          mode={modal.mode}
          user={modal.user}
          isSelf={modal.user?.username === me?.username}
          onSave={handleSave}
          onClose={() => setModal(null)}
        />
      )}

      {resetTarget && (
        <ResetPasswordModal
          user={resetTarget}
          onSave={handleResetPassword}
          onClose={() => setResetTarget(null)}
        />
      )}

      {toast && <Toast message={toast.message} type={toast.type} onDismiss={dismissToast} />}
    </>
  )
}
