import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import i18n from '../i18n'
import { api } from '../api'
import { formatDuration } from '../format'

// One revocation, one card (E13). `group` is a RevocationGroupOut from
// GET /devices/{sn}/revocations: the two `DATA DELETE` commands E8 sends
// already merged server-side, by (device_sn, pin) — not re-derived here, so
// Employees.jsx and CommandsDrawer.jsx cannot drift into grouping two
// commands two different ways, which is how a UI ended up offering a
// "Cancel" per command instead of per revocation in the first place.
//
// `group.split` is the one case tidiness must lose to honesty: the two
// commands genuinely in different states (one acknowledged, one still
// outstanding; one refused, one pending). Averaging that into one status
// line would hide exactly the half-revocation this unit exists to surface.

function roleLine(role, entry) {
  const t = i18n.t
  const name = t(`revocation.role.${role}`)
  const line = (key) => t(`revocation.line.${key}`, { name, code: entry?.return_code })
  if (!entry) return line('never_queued')
  if (entry.outstanding) {
    return entry.state === 'sent' ? line('delivered_waiting') : line('waiting_poll')
  }
  switch (entry.state) {
    case 'acknowledged':
      return line('confirmed')
    case 'refused':
      return line('refused')
    case 'unconfirmed':
      // E11: a positive code this system cannot read. Not a refusal, not a
      // confirmation — say exactly that, nothing stronger.
      return line('unconfirmed')
    case 'cancelled':
      return line('cancelled')
    default:
      return line('gave_up')
  }
}

// Timestamps come back naive-UTC from the API; anchor them before diffing,
// or a UTC+4 browser reads a fresh command as hours old.
function since(iso) {
  if (!iso) return ''
  const stamp = /(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`
  return formatDuration((Date.now() - new Date(stamp).getTime()) / 1000)
}

export default function RevocationCard({ group, title, cancelLabel, onCancelled, onError }) {
  const { t } = useTranslation()
  const [busy, setBusy] = useState(false)
  const { user, userauthorize, split, still_open } = group

  // Cancelling calls E8's revocation-level DELETE, atomically, for BOTH
  // commands — never a per-command cancel. That is the actual correctness
  // fix here: two "Cancel" buttons on two cards each cancelling only their
  // own command is exactly the half-revocation this unit exists to close.
  async function handleCancel() {
    setBusy(true)
    try {
      const res = await api.devices.cancelRevocation(group.device_sn, group.user_id)
      onCancelled?.(res)
    } catch (err) {
      onError?.(err)
    } finally {
      setBusy(false)
    }
  }

  const outstanding = user?.outstanding ? user : userauthorize?.outstanding ? userauthorize : null

  return (
    <div className="bg-white rounded-lg px-3 py-2.5 text-sm">
      <div className="flex items-center gap-2">
        <p className="text-gray-900 font-semibold flex-1 min-w-0 truncate">{title}</p>
        <span
          className={`text-xs px-1.5 py-0.5 rounded-full font-semibold whitespace-nowrap ${
            still_open ? 'bg-red-600 text-white' : 'bg-amber-100 text-amber-800'
          }`}
        >
          {still_open ? t('revocation.still_open') : t('revocation.clearing_up')}
        </span>
      </div>

      {split ? (
        <div className={`text-xs mt-1 space-y-0.5 ${still_open ? 'text-red-700' : 'text-amber-800'}`}>
          <p>{roleLine('user', user)}</p>
          <p>{roleLine('userauthorize', userauthorize)}</p>
        </div>
      ) : (
        <p className={`text-xs mt-1 ${still_open ? 'text-red-700' : 'text-amber-800'}`}>
          {user.state === 'sent'
            ? t('revocation.handed_over')
            : t('revocation.waiting_poll')}
          {outstanding?.created_at && <> · {t('revocation.outstanding_for', { time: since(outstanding.created_at) })}</>}
        </p>
      )}

      {cancelLabel && (
        <button
          onClick={handleCancel}
          disabled={busy}
          className="mt-2 text-xs text-gray-600 hover:text-gray-900 px-2 py-1 rounded hover:bg-gray-100 disabled:opacity-40 transition-colors"
        >
          {busy ? t('common.cancelling') : cancelLabel}
        </button>
      )}
    </div>
  )
}
