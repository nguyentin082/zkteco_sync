import { useState, useEffect, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import i18n, { serverMessage } from '../i18n'
import { api } from '../api'
import { formatDate as formatDay, formatDateTime, formatDuration, formatTime as formatClock } from '../format'
import { useAuth } from '../auth'
import DeviceFormModal from '../components/DeviceFormModal'
import DeviceTimezoneModal from '../components/DeviceTimezoneModal'
import DeviceProtocolModal from '../components/DeviceProtocolModal'
import DeviceSecurityDrawer from '../components/DeviceSecurityDrawer'
import KebabMenu from '../components/KebabMenu'
import DeviceInfoDrawer from '../components/DeviceInfoDrawer'
import SetClockDrawer from '../components/SetClockDrawer'
import WriteLcdDrawer from '../components/WriteLcdDrawer'
import CommandsDrawer from '../components/CommandsDrawer'
import DeviceUsersDrawer from '../components/DeviceUsersDrawer'
import PasswordConfirmModal from '../components/PasswordConfirmModal'

function StatusBadge({ isOnline }) {
  const { t } = useTranslation()
  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-medium whitespace-nowrap ${
        isOnline ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'
      }`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${isOnline ? 'bg-green-500' : 'bg-gray-400'}`} />
      {isOnline ? t('devices.online') : t('devices.offline')}
    </span>
  )
}

const TRUST_STYLES = {
  approved: 'bg-blue-50 text-blue-700',
  pending: 'bg-amber-100 text-amber-800',
  rejected: 'bg-red-100 text-red-700',
}

function TrustBadge({ status, ipLocked }) {
  const { t } = useTranslation()
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        className={`inline-flex px-2.5 py-0.5 rounded-full text-xs font-medium whitespace-nowrap ${
          TRUST_STYLES[status] || 'bg-gray-100 text-gray-500'
        }`}
      >
        {t(`devices.trust.${status || 'unknown'}`, { defaultValue: status })}
      </span>
      {ipLocked && (
        <span
          title={t('devices.ip_locked_title')}
          className="inline-flex px-1.5 py-0.5 rounded text-[10px] font-medium bg-gray-100 text-gray-600"
        >
          IP
        </span>
      )}
    </span>
  )
}

function Toast({ message, type, onDismiss }) {
  useEffect(() => {
    // An error is something to read, not glimpse — "timed out" at 3.5s reads
    // as a flicker. It also stays until dismissed by the next toast.
    const t = setTimeout(onDismiss, type === 'error' ? 10_000 : 3500)
    return () => clearTimeout(t)
  }, [onDismiss, type])

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

const PULL_KINDS = ['employees', 'attendance', 'templates']
const pullKindLabel = (kind) => i18n.t(`devices.pull_kinds.${kind}`)

function formatRelative(iso) {
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 60) return i18n.t('time.just_now')
  if (seconds < 86_400) return i18n.t('time.ago', { time: formatDuration(seconds) })
  return formatDay(iso)
}

// What the last SDK pull of each kind did (DeviceOut.pull_outcomes). The pull
// runs in the background after the server has already said "started", so
// this column is the ONLY place a failure — a timed-out connect, a refused
// comm key — is visible without reading the server log. Shown per kind and
// never collapsed to the latest one: Sync All runs three pulls, and a
// successful template read must not hide the attendance read that failed.
function LastSyncCell({ outcomes }) {
  const { t } = useTranslation()
  const kinds = PULL_KINDS.filter((k) => outcomes?.[k])
  if (kinds.length === 0) return <span className="text-gray-400">—</span>
  return (
    <ul className="space-y-0.5">
      {kinds.map((kind) => {
        const o = outcomes[kind]
        return (
          <li
            key={kind}
            title={`${pullKindLabel(kind)} · ${formatDateTime(o.at)}${o.seconds != null ? ` · ${t('devices.took', { seconds: o.seconds })}` : ''}\n${o.detail}`}
            className={`text-xs ${o.ok ? 'text-gray-600' : 'text-red-700 font-medium'}`}
          >
            <span className={o.ok ? 'text-green-600' : 'text-red-600'}>{o.ok ? '✓' : '✗'}</span>{' '}
            {pullKindLabel(kind)} · {formatRelative(o.at)}
            {o.seconds != null && <span className="text-gray-400"> · {Math.round(o.seconds)}s</span>}
            {!o.ok && <span className="block font-normal text-red-600 truncate max-w-[16rem]">{o.detail}</span>}
          </li>
        )
      })}
    </ul>
  )
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// Wait for the background pull(s) started by a Sync click to finish, by
// watching the device row for a NEWER outcome of each kind than the one it
// had before the click. Resolves to the fresh outcomes, or null when nothing
// arrived within the budget (an SDK connect times out after 30s; Sync All is
// three of those in a row, and a device with a year of backlog reads slowly).
async function waitForPullOutcomes(sn, kinds, before, budgetMs = 180_000) {
  const started = Date.now()
  while (Date.now() - started < budgetMs) {
    await sleep(2000)
    let device
    try {
      device = await api.devices.get(sn)
    } catch {
      continue
    }
    const outcomes = device.pull_outcomes || {}
    if (kinds.every((k) => outcomes[k] && outcomes[k].at !== before?.[k]?.at)) {
      return outcomes
    }
  }
  return null
}

export default function Devices() {
  const { t } = useTranslation()
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [devices, setDevices] = useState([])
  const [pairing, setPairing] = useState(null)
  const [loading, setLoading] = useState(true)
  const [modal, setModal] = useState(null)
  const [tzModal, setTzModal] = useState(null)   // device whose timezone is being changed
  const [protoModal, setProtoModal] = useState(null)   // device whose protocol is being changed
  const [drawer, setDrawer] = useState(null) // { type, device }
  const [pwConfirm, setPwConfirm] = useState(null) // { title, description, onConfirm }
  const [toast, setToast] = useState(null)

  const showToast = useCallback((message, type = 'success') => setToast({ message, type }), [])
  const dismissToast = useCallback(() => setToast(null), [])

  const loadDevices = useCallback(async () => {
    try {
      const [list, window] = await Promise.all([api.devices.list(), api.devices.getPairing()])
      setDevices(list)
      setPairing(window)
    } catch {
      showToast(t('devices.load_failed'), 'error')
    } finally {
      setLoading(false)
    }
  }, [showToast, t])

  useEffect(() => {
    loadDevices()
    const interval = setInterval(loadDevices, 10_000)
    return () => clearInterval(interval)
  }, [loadDevices])

  async function handleApprove(device) {
    try {
      await api.devices.approve(device.serial_number)
      showToast(t('devices.approved_toast', { device: device.name || device.serial_number }))
      loadDevices()
    } catch (err) {
      showToast(err.message, 'error')
    }
  }

  async function handleReject(device) {
    try {
      await api.devices.reject(device.serial_number)
      showToast(t('devices.rejected_toast', { device: device.name || device.serial_number }))
      loadDevices()
    } catch (err) {
      showToast(err.message, 'error')
    }
  }

  async function handlePairing(open) {
    try {
      const window = open ? await api.devices.openPairing() : await api.devices.closePairing()
      setPairing(window)
      showToast(open ? t('devices.pairing_opened') : t('devices.pairing_closed'))
      loadDevices()
    } catch (err) {
      showToast(err.message, 'error')
    }
  }

  async function handleSave(formData) {
    if (modal.mode === 'create') {
      await api.devices.create(formData)
      showToast(t('devices.added'))
    } else {
      await api.devices.update(modal.device.serial_number, formData)
      showToast(t('devices.updated'))
    }
    setModal(null)
    loadDevices()
  }

  async function handleSaveTimezone(timezone) {
    const updated = await api.devices.setTimezone(tzModal.serial_number, timezone)
    showToast(t('devices.timezone_set', { tz: updated.timezone }))
    setTzModal(null)
    loadDevices()
  }

  async function handleSaveProtocol(protocol) {
    const updated = await api.devices.setProtocol(protoModal.serial_number, protocol)
    showToast(t('devices.protocol_set', { protocol: updated.protocol }))
    setProtoModal(null)
    loadDevices()
  }

  async function handleDelete(device) {
    if (!confirm(t('devices.confirm_remove', { device: device.name || device.serial_number }))) return
    try {
      await api.devices.delete(device.serial_number)
      showToast(t('devices.removed'))
      loadDevices()
    } catch (err) {
      showToast(err.message, 'error')
    }
  }

  async function handleSync(device, type) {
    const label = t(`devices.sync.${type}`)
    const calls = {
      all: () => api.devices.pull(device.serial_number),
      employees: () => api.devices.pullEmployees(device.serial_number),
      attendance: () => api.devices.pullAttendance(device.serial_number),
      templates: () => api.devices.pullTemplates(device.serial_number),
    }
    // Which background pulls this click starts on an `att` device. The
    // manual template pull is synchronous and reports in its own response.
    const pullKinds = {
      all: ['employees', 'attendance', 'templates'],
      employees: ['employees'],
      attendance: ['attendance'],
      templates: [],
    }[type]
    const name = device.name || device.serial_number
    let result
    try {
      result = await calls[type]()
    } catch (err) {
      showToast(err.message, 'error')
      return
    }
    // An `acc` terminal is never dialled: the server queues a DATA QUERY and
    // the device answers on its next poll, so the response says what really
    // happened and that is what gets shown. Reporting "started" for work
    // that has only been enqueued is the thing this avoids.
    if (result?.status === 'queued' || pullKinds.length === 0) {
      showToast(serverMessage(result, t('devices.sync_done', { label, device: name })))
      return
    }
    // "started" is all the server can honestly say at this point — the pull
    // is a background task. So say it, then watch the device row for what
    // the pull actually did and report THAT, in red if it failed. Before
    // this, a timed-out connect was visible only in the server log.
    showToast(t('devices.sync_started', { label, device: name }))
    const outcomes = await waitForPullOutcomes(device.serial_number, pullKinds, device.pull_outcomes)
    loadDevices()
    if (!outcomes) {
      showToast(t('devices.sync_no_result', { label, device: name }), 'error')
      return
    }
    const failed = pullKinds.filter((k) => !outcomes[k].ok)
    if (failed.length > 0) {
      showToast(
        t('devices.sync_failed', {
          label,
          device: name,
          details: failed.map((k) => `${pullKindLabel(k)}: ${outcomes[k].detail}`).join('; '),
        }),
        'error'
      )
    } else {
      showToast(
        t('devices.sync_done_details', {
          label,
          device: name,
          details: pullKinds.map((k) => outcomes[k].detail).join('; '),
        })
      )
    }
  }

  function confirmAction(title, description, action) {
    setPwConfirm({ title, description, onConfirm: action })
  }

  // The three device-control actions share one rule, the same one the sync
  // actions follow: on an access-control terminal the server has QUEUED
  // something, not done it, and it says so in `message`. Where a message comes
  // back it is shown verbatim rather than being replaced with a cheerful past
  // tense that would be false.
  async function handleClearAttendance(device) {
    try {
      const result = await api.devices.clearAttendance(device.serial_number)
      showToast(
        serverMessage(result, t('devices.attendance_cleared', { device: device.name || device.serial_number }))
      )
    } catch (err) {
      showToast(err.message, 'error')
    }
  }

  async function handleRestart(device) {
    try {
      const result = await api.devices.restart(device.serial_number)
      showToast(
        serverMessage(result, t('devices.restarting', { device: device.name || device.serial_number }))
      )
    } catch (err) {
      showToast(err.message, 'error')
    }
  }

  async function handleUnlock(device) {
    try {
      const result = await api.devices.unlock(device.serial_number)
      // "Door unlocked" is only true on the SDK transport, where the call
      // returned because the door opened. On a queued unlock the honest
      // report is the server's, which says queued and states the delay.
      showToast(
        serverMessage(result, t('devices.door_unlocked', { device: device.name || device.serial_number }))
      )
    } catch (err) {
      showToast(err.message, 'error')
    }
  }

  function menuItems(device) {
    // Which sync actions exist at all depends on the protocol, because the two
    // families are read over different transports. An access-control terminal
    // has no pull for attendance — it pushes punches up by itself — so that
    // item is shown unavailable WITH THE REASON rather than hidden (an
    // operator cannot troubleshoot a menu entry that is not there) and rather
    // than left clickable (it would dial TCP 4370 and time out).
    const isAcc = (device.protocol || 'att') === 'acc'

    return [
      { label: t('devices.sync.all'), onClick: () => handleSync(device, 'all') },
      { label: t('devices.sync.employees'), onClick: () => handleSync(device, 'employees') },
      isAcc
        ? {
            label: t('devices.sync.attendance'),
            disabled: true,
            hint: t('devices.menu.sync_attendance_na'),
          }
        : { label: t('devices.sync.attendance'), onClick: () => handleSync(device, 'attendance') },
      { label: t('devices.sync.templates'), onClick: () => handleSync(device, 'templates') },
      'divider',
      { label: t('devices.menu.manage_users'), onClick: () => setDrawer({ type: 'users', device }) },
      { label: t('devices.menu.device_info'), onClick: () => setDrawer({ type: 'info', device }) },
      { label: t('devices.menu.set_clock'), onClick: () => setDrawer({ type: 'clock', device }) },
      // No command in the access-control protocol addresses the screen, so
      // this is shown unavailable with the reason rather than left clickable
      // (it would open a drawer whose only possible outcome is a 501).
      isAcc
        ? {
            label: t('devices.menu.write_lcd'),
            disabled: true,
            hint: t('devices.menu.write_lcd_na'),
          }
        : { label: t('devices.menu.write_lcd'), onClick: () => setDrawer({ type: 'lcd', device }) },
      // The door DOES work here, but it is not the same action it is on an
      // SDK device and the menu says so before it is clicked. See handleUnlock.
      isAcc
        ? {
            label: t('devices.menu.unlock_door'),
            onClick: () => handleUnlock(device),
            hint: t('devices.menu.unlock_door_acc'),
          }
        : { label: t('devices.menu.unlock_door'), onClick: () => handleUnlock(device) },
      { label: t('devices.menu.commands'), onClick: () => setDrawer({ type: 'commands', device }) },
      'divider',
      {
        label: t('devices.menu.clear_attendance'),
        danger: true,
        onClick: () => confirmAction(
          t('devices.menu.clear_attendance'),
          isAcc
            ? t('devices.menu.clear_attendance_acc')
            : t('devices.menu.clear_attendance_att'),
          () => { setPwConfirm(null); handleClearAttendance(device) }
        ),
      },
      {
        label: t('devices.menu.restart'),
        danger: true,
        onClick: () => confirmAction(
          t('devices.menu.restart'),
          isAcc
            ? t('devices.menu.restart_acc')
            : t('devices.menu.restart_att'),
          () => { setPwConfirm(null); handleRestart(device) }
        ),
      },
      'divider',
      { label: t('devices.menu.security'), onClick: () => setDrawer({ type: 'security', device }) },
      { label: t('common.edit'), onClick: () => setModal({ mode: 'edit', device }) },
      { label: t('common.delete'), danger: true, onClick: () => handleDelete(device) },
    ]
  }

  function formatDate(iso) {
    if (!iso) return '—'
    return formatDateTime(iso)
  }

  function formatTime(iso) {
    if (!iso) return '—'
    return formatClock(iso)
  }

  const pendingDevices = devices.filter((d) => d.status === 'pending')

  return (
    <>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-xl font-semibold text-gray-900">{t('nav.devices')}</h1>
        <div className="flex items-center gap-3">
          {isAdmin && (
            pairing?.is_open ? (
              <div className="flex items-center gap-2 text-sm">
                <span className="text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-1.5">
                  {t('devices.pairing_open_until', { time: formatTime(pairing.open_until) })}
                </span>
                <button
                  onClick={() => handlePairing(false)}
                  className="border border-gray-200 hover:bg-gray-50 text-gray-700 text-sm font-medium px-3 py-2 rounded-lg transition-colors"
                >
                  {t('devices.close_pairing')}
                </button>
              </div>
            ) : (
              <button
                onClick={() => handlePairing(true)}
                title={t('devices.open_pairing_title')}
                className="border border-gray-200 hover:bg-gray-50 text-gray-700 text-sm font-medium px-3 py-2 rounded-lg transition-colors"
              >
                {t('devices.open_pairing')}
              </button>
            )
          )}
          <button
            onClick={() => setModal({ mode: 'create' })}
            className="bg-blue-600 hover:bg-blue-700 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
          >
            + {t('device_form.add_title')}
          </button>
        </div>
      </div>

      {pendingDevices.length > 0 && (
        <div className="bg-amber-50 border border-amber-200 rounded-xl mb-6">
          <div className="px-4 py-3 border-b border-amber-200">
            <h2 className="text-sm font-semibold text-amber-900">{t('devices.waiting_approval')}</h2>
            <p className="text-xs text-amber-700 mt-0.5">
              {t('devices.waiting_approval_hint')}
            </p>
          </div>
          <table className="w-full text-sm">
            <tbody>
              {pendingDevices.map((device) => (
                <tr key={device.serial_number} className="border-b border-amber-100 last:border-0">
                  <td className="px-4 py-3 font-mono text-xs text-gray-700">{device.serial_number}</td>
                  <td className="px-4 py-3 text-gray-500">{t('devices.from_ip', { ip: device.last_ip || '—' })}</td>
                  <td className="px-4 py-3 text-gray-400 text-xs">{formatDate(device.last_seen || device.created_at)}</td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex gap-2 justify-end">
                      <button
                        onClick={() => handleApprove(device)}
                        disabled={!isAdmin}
                        className="bg-blue-600 hover:bg-blue-700 disabled:opacity-40 text-white text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
                      >
                        {t('devices.approve')}
                      </button>
                      <button
                        onClick={() => handleReject(device)}
                        disabled={!isAdmin}
                        className="border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-40 text-xs font-medium px-3 py-1.5 rounded-lg transition-colors"
                      >
                        {t('devices.reject')}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* A standing banner, not a toast. An access revocation that the door
          has not confirmed does not stop being true because somebody looked
          away, and it is the one queue state in this application where
          waiting is a hazard rather than a feature. */}
      {devices.some((d) => d.pending_revocations > 0) && (
        <div className="mb-4 border-2 border-red-300 bg-red-50 rounded-xl px-4 py-3">
          <p className="text-sm font-semibold text-red-800">
            {t('devices.revocations_banner_title')}
          </p>
          <p className="text-xs text-red-700 mt-1">
            {t('devices.revocations_banner_body')}
          </p>
          <ul className="mt-2 space-y-0.5">
            {devices
              .filter((d) => d.pending_revocations > 0)
              .map((d) => (
                <li key={d.serial_number} className="text-xs text-red-800">
                  <span className="font-medium">{d.name || d.serial_number}</span>
                  {' — '}
                  {t('devices.outstanding_count', { count: d.pending_revocations })}
                  {!d.is_online && ` · ${t('devices.device_offline')}`}
                </li>
              ))}
          </ul>
        </div>
      )}

      <div className="bg-white rounded-xl border border-gray-200">
        {loading ? (
          <div className="p-12 text-center text-sm text-gray-400">{t('common.loading')}</div>
        ) : devices.length === 0 ? (
          <div className="p-12 text-center text-sm text-gray-400">
            {t('devices.empty')}
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 bg-gray-50 [&>th:first-child]:rounded-tl-xl [&>th:last-child]:rounded-tr-xl">
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('devices.col.name')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('devices.col.serial')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('devices.col.address')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('devices.col.status')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('devices.col.trust')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('devices.col.timezone')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('devices.col.protocol')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('devices.col.last_seen')}</th>
                <th className="text-left px-4 py-3 font-medium text-gray-500">{t('devices.col.last_sync')}</th>
                <th className="px-4 py-3" />
              </tr>
            </thead>
            <tbody>
              {devices.map((device) => (
                <tr
                  key={device.serial_number}
                  className="border-b border-gray-100 last:border-0 hover:bg-gray-50 transition-colors"
                >
                  <td className="px-4 py-3 font-medium text-gray-900">
                    {device.name || <span className="text-gray-400">—</span>}
                    {/* An outstanding revocation is a safety state, not a
                        queue statistic: somebody has been taken off this door
                        in the system and the door has not been told. It is
                        surfaced here as well as on the person's page because
                        an operator scanning this table for "is anything
                        wrong" should not have to open every employee to find
                        it. */}
                    {device.pending_revocations > 0 && (
                      <span className="block mt-1 text-xs font-semibold text-red-700">
                        {t('devices.revocations_not_confirmed', { count: device.pending_revocations })}
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500 font-mono text-xs">{device.serial_number}</td>
                  <td className="px-4 py-3 text-gray-500">{device.ip_address}:{device.port}</td>
                  <td className="px-4 py-3"><StatusBadge isOnline={device.is_online} /></td>
                  <td className="px-4 py-3">
                    <TrustBadge status={device.status} ipLocked={device.ip_check_enabled} />
                  </td>
                  <td className="px-4 py-3">
                    {/* Read-only. Changing it relabels every record this device
                        pushed, so it is edited only through its own modal. */}
                    <span className="inline-flex items-center gap-2">
                      <span className="text-gray-600 text-xs">{device.timezone || '—'}</span>
                      {isAdmin && (
                        <button
                          onClick={() => setTzModal(device)}
                          title={t('devices.edit_timezone_title')}
                          aria-label={t('devices.edit_timezone_aria', { sn: device.serial_number })}
                          data-testid={`edit-timezone-${device.serial_number}`}
                          className="text-xs text-blue-500 hover:text-blue-700 underline"
                        >
                          {t('common.edit')}
                        </button>
                      )}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {/* Read-only. Normally set automatically from what the
                        device announces (D9); an operator corrects it only
                        through its own modal, which pins the value. */}
                    <span className="inline-flex items-center gap-2">
                      <span className="text-gray-600 text-xs">
                        {device.protocol || 'att'}
                        {device.protocol_pinned && (
                          <span
                            title={t('devices.pinned_title')}
                            className="ml-1 text-[10px] text-amber-700 bg-amber-50 border border-amber-200 rounded px-1 py-0.5"
                          >
                            {t('devices.pinned')}
                          </span>
                        )}
                      </span>
                      {isAdmin && (
                        <button
                          onClick={() => setProtoModal(device)}
                          title={t('devices.edit_protocol_title')}
                          aria-label={t('devices.edit_protocol_aria', { sn: device.serial_number })}
                          data-testid={`edit-protocol-${device.serial_number}`}
                          className="text-xs text-blue-500 hover:text-blue-700 underline"
                        >
                          {t('common.edit')}
                        </button>
                      )}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-400 text-xs">{formatDate(device.last_seen)}</td>
                  <td className="px-4 py-3"><LastSyncCell outcomes={device.pull_outcomes} /></td>
                  <td className="px-4 py-3 text-right">
                    <KebabMenu items={menuItems(device)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {modal && (
        <DeviceFormModal
          mode={modal.mode}
          device={modal.device}
          onSave={handleSave}
          onClose={() => setModal(null)}
        />
      )}

      {tzModal && (
        <DeviceTimezoneModal
          device={tzModal}
          onSave={handleSaveTimezone}
          onClose={() => setTzModal(null)}
        />
      )}

      {protoModal && (
        <DeviceProtocolModal
          device={protoModal}
          onSave={handleSaveProtocol}
          onClose={() => setProtoModal(null)}
        />
      )}

      {drawer?.type === 'users' && (
        <DeviceUsersDrawer
          device={drawer.device}
          onClose={() => setDrawer(null)}
          showToast={showToast}
        />
      )}
      {drawer?.type === 'info' && (
        <DeviceInfoDrawer
          device={drawer.device}
          onClose={() => setDrawer(null)}
          showToast={showToast}
        />
      )}
      {drawer?.type === 'clock' && (
        <SetClockDrawer
          device={drawer.device}
          onClose={() => setDrawer(null)}
          showToast={showToast}
        />
      )}
      {drawer?.type === 'lcd' && (
        <WriteLcdDrawer
          device={drawer.device}
          onClose={() => setDrawer(null)}
          showToast={showToast}
        />
      )}
      {drawer?.type === 'security' && (
        <DeviceSecurityDrawer
          device={drawer.device}
          onClose={() => setDrawer(null)}
          onSaved={loadDevices}
          showToast={showToast}
        />
      )}
      {drawer?.type === 'commands' && (
        <CommandsDrawer
          device={drawer.device}
          onClose={() => setDrawer(null)}
          showToast={showToast}
          onChange={loadDevices}
        />
      )}

      {pwConfirm && (
        <PasswordConfirmModal
          title={pwConfirm.title}
          description={pwConfirm.description}
          onConfirm={pwConfirm.onConfirm}
          onClose={() => setPwConfirm(null)}
        />
      )}

      {toast && <Toast message={toast.message} type={toast.type} onDismiss={dismissToast} />}
    </>
  )
}
