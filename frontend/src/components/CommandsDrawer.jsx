import { useState, useEffect, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import i18n, { serverMessage } from '../i18n'
import { api } from '../api'
import { formatDuration } from '../format'
import Drawer from './Drawer'
import RevocationCard from './RevocationCard'
import DeviceWriteHint from './DeviceWriteHint'

const PRESETS = [
  { key: 'reboot', value: 'REBOOT' },
  { key: 'sync_time', value: 'DATE' },
  { key: 'enable', value: 'ENABLE' },
  { key: 'disable', value: 'DISABLE' },
]

// The two commands E8 sends to revoke one person (`DATA DELETE user` and
// `DATA DELETE userauthorize`) — the exact shape `GET /devices/{sn}/revocations`
// groups server-side (E13), and the two this section pulls out of the plain
// outbox list so they get the grouped, loud treatment instead of sitting in
// "Outstanding" as two unrelated rows. Narrower than "any DATA DELETE" on
// purpose: a hand-typed delete for some other table is a real command this
// drawer has never seen before and should stay visible in "Outstanding"
// rather than silently vanish because it happened to start the same way.
const isGroupedRevocation = (command) =>
  /^DATA DELETE (user|userauthorize)\s+Pin=/i.test(String(command || '').trim())

// For the History Pill and the retry-warning copy below, where "was this a
// revocation" only needs to be roughly right, not grouped.
const isRevocation = (command) => /^DATA DELETE\b/i.test(String(command || '').trim())

// How long ago (past) or how long until (future) a timestamp is, in words an
// operator can act on without doing the arithmetic themselves.
function relativeTime(iso) {
  if (!iso) return null
  // Timestamps come back naive-UTC from the API; anchor them before diffing,
  // or a UTC+4 browser reads a fresh command as hours old (or not-yet-due).
  const stamp = /(Z|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`
  return Date.now() - new Date(stamp).getTime() // positive = past, negative = future
}

const since = (iso) => {
  const d = relativeTime(iso)
  return d == null ? '' : i18n.t('time.ago', { time: formatDuration(d / 1000) })
}

const until = (iso) => {
  const d = relativeTime(iso)
  if (d == null) return ''
  return d > 0 ? i18n.t('commands.due_now') : i18n.t('time.in', { time: formatDuration(d / 1000) })
}

const PILL_TONES = {
  gray: 'bg-gray-100 text-gray-600',
  blue: 'bg-blue-50 text-blue-700',
  green: 'bg-green-50 text-green-700',
  amber: 'bg-amber-100 text-amber-800',
  red: 'bg-red-100 text-red-700',
}

function Pill({ tone = 'gray', children }) {
  return (
    <span className={`inline-flex px-2 py-0.5 rounded-full text-[11px] font-semibold whitespace-nowrap ${PILL_TONES[tone]}`}>
      {children}
    </span>
  )
}

// What a row in device_command_log actually means. `outcome=failed` covers
// three different stories that must not be run together: the device
// understood the command and refused it (return_code set), the operator
// called it off (last_error says so), or nobody ever heard back at all
// (return_code null, no cancellation). Read off the row, never inferred from
// the outbox being empty — an empty outbox means "concluded", not "succeeded"
// (E8's browser gate caught exactly that confusion).
// How a concluded command is labelled. The judgement itself is the server's
// (`row.verdict`, from commands.history_verdict) so that this panel cannot call
// something a refusal that the server does not — the fallback below is only for
// a server too old to send one.
//
// `Unconfirmed` is not a softer word for refused. It means the device answered
// with a code this system cannot read, on firmware that has returned exactly
// such a code on commands that demonstrably WORKED. Saying "Refused — code 3"
// there would be a wrong verdict on a door command, which is the whole reason
// this distinction exists (E11).
function historyOutcome(row) {
  const t = i18n.t
  const verdict =
    row.verdict ||
    (row.outcome === 'acknowledged'
      ? 'acknowledged'
      : row.return_code != null
      ? row.return_code < 0
        ? 'refused'
        : 'unconfirmed'
      : (row.last_error || '').startsWith('cancelled by')
      ? 'cancelled'
      : 'abandoned')

  if (verdict === 'acknowledged') {
    // `verdict_detail` is the server's plain-language reading of the device's
    // number — for a DATA QUERY that is a record count, and "no records
    // matched" is a real answer to a real query, not an absence. Showing it is
    // the difference between an operator seeing that a query ran and returned
    // nothing, and them seeing a bare "Acknowledged" and assuming data arrived.
    return {
      verdict,
      label: row.verdict_detail
        ? t('commands.outcome.acknowledged_detail', { detail: row.verdict_detail })
        : t('commands.outcome.acknowledged'),
      tone: 'green',
    }
  }
  if (verdict === 'refused') {
    return { verdict, label: t('commands.outcome.refused', { code: row.return_code }), tone: 'red' }
  }
  if (verdict === 'unconfirmed') {
    return { verdict, label: t('commands.outcome.unconfirmed', { code: row.return_code }), tone: 'amber' }
  }
  if (verdict === 'cancelled') return { verdict, label: t('commands.outcome.cancelled'), tone: 'gray' }
  return { verdict, label: t('commands.outcome.gave_up'), tone: 'amber' }
}

function Section({ title, hint, children }) {
  return (
    <div className="mb-5">
      <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-1">{title}</h3>
      {hint && <p className="text-xs text-gray-400 mb-2">{hint}</p>}
      {children}
    </div>
  )
}

export default function CommandsDrawer({ device, onClose, showToast, onChange }) {
  const { t } = useTranslation()
  const sn = device.serial_number

  const [command, setCommand] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')

  const [outbox, setOutbox] = useState([])
  const [history, setHistory] = useState([])
  const [revocationGroups, setRevocationGroups] = useState([])
  const [loadingLists, setLoadingLists] = useState(true)

  const [busy, setBusy] = useState({}) // { [key]: true } while an action is in flight
  const [confirmCancelId, setConfirmCancelId] = useState(null) // outbox id awaiting a second click
  const [confirmRetryId, setConfirmRetryId] = useState(null)   // log id awaiting a second click
  // Persistent until dismissed or replaced — a 3.5s toast is not enough time
  // to read the honest wording a cancel or retry can carry.
  const [notice, setNotice] = useState(null)

  const refresh = useCallback(async () => {
    try {
      const [ob, hs, rv] = await Promise.all([
        api.devices.listCommands(sn),
        api.devices.commandHistory(sn),
        api.devices.listRevocations(sn),
      ])
      setOutbox(ob)
      setHistory(hs)
      setRevocationGroups(rv)
    } catch {
      showToast(t('commands.load_failed'), 'error')
    } finally {
      setLoadingLists(false)
    }
  }, [sn, showToast, t])

  useEffect(() => {
    refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sn])

  async function handleSend(e) {
    e.preventDefault()
    if (!command.trim()) return
    setError('')
    setSending(true)
    try {
      await api.devices.queueCommand(sn, command.trim())
      showToast(t('commands.queued'))
      setCommand('')
      refresh()
      onChange?.()
    } catch (err) {
      setError(err.message)
    } finally {
      setSending(false)
    }
  }

  async function doCancel(row) {
    setConfirmCancelId(null)
    setBusy((b) => ({ ...b, [`out-${row.id}`]: true }))
    try {
      const result = await api.devices.cancelCommand(sn, row.id)
      showToast(result.was_sent ? t('commands.cancelled_record_only') : t('commands.cancelled_before_delivery'))
      setNotice({ type: result.was_sent ? 'warning' : 'success', text: serverMessage(result) })
      await refresh()
      onChange?.()
    } catch (err) {
      showToast(err.message, 'error')
    } finally {
      setBusy((b) => { const n = { ...b }; delete n[`out-${row.id}`]; return n })
    }
  }

  function handleCancelClick(row) {
    // A `pending` command has never left this server — cancelling it is the
    // whole truth, so one click is enough. A `sent` command has already been
    // handed to the device at least once, so cancelling here only edits our
    // own bookkeeping; that needs a second, informed click before it happens,
    // not a toast explaining it after the fact.
    if (row.status === 'sent') {
      setConfirmCancelId(row.id)
      return
    }
    doCancel(row)
  }

  async function doRetry(row) {
    setConfirmRetryId(null)
    setBusy((b) => ({ ...b, [`log-${row.id}`]: true }))
    try {
      const result = await api.devices.retryCommand(sn, row.id)
      showToast(t('commands.requeued'))
      setNotice({ type: result.was_device_refusal ? 'warning' : 'info', text: serverMessage(result) })
      await refresh()
      onChange?.()
    } catch (err) {
      showToast(err.message, 'error')
    } finally {
      setBusy((b) => { const n = { ...b }; delete n[`log-${row.id}`]; return n })
    }
  }

  function handleRetryClick(row) {
    // A device refusal is the device having understood the command and said
    // no. Nothing changed at the device in between, so retrying will very
    // likely earn the identical refusal — that deserves a second click, not
    // a silent requeue of the same rejected bytes.
    if (row.return_code != null) {
      setConfirmRetryId(row.id)
      return
    }
    doRetry(row)
  }

  const otherOutbox = outbox.filter((r) => !isGroupedRevocation(r.command))

  // RevocationCard owns the request and its own busy state — these only
  // react to the outcome. Calls E8's revocation-level DELETE, which cancels
  // BOTH `DATA DELETE` commands atomically — never the per-command cancel
  // above, which could leave one half of a revocation behind (E13).
  function handleRevocationCancelled(res) {
    setNotice({ type: 'success', text: serverMessage(res, t('commands.revocation_cancelled')) })
    refresh()
    onChange?.()
  }

  function handleRevocationCancelError(err) {
    showToast(err.message, 'error')
  }

  return (
    <Drawer title={t('commands.title')} onClose={onClose} width="max-w-lg">
      {notice && (
        <div
          className={`mb-4 text-xs rounded-lg px-3 py-2 border flex items-start justify-between gap-2 ${
            notice.type === 'warning'
              ? 'border-amber-300 bg-amber-50 text-amber-800'
              : notice.type === 'success'
                ? 'border-green-200 bg-green-50 text-green-800'
                : 'border-blue-200 bg-blue-50 text-blue-800'
          }`}
        >
          <span>{notice.text}</span>
          <button
            type="button"
            onClick={() => setNotice(null)}
            aria-label={t('common.close')}
            className="opacity-60 hover:opacity-100 shrink-0"
          >
            ✕
          </button>
        </div>
      )}

      <Section title={t('commands.queue_title')} hint={t('commands.queue_hint')}>
        <div className="flex flex-wrap gap-2 mb-3">
          {PRESETS.map((p) => (
            <button
              key={p.value}
              type="button"
              onClick={() => setCommand(p.value)}
              className="text-xs border border-gray-300 text-gray-600 hover:bg-gray-50 px-3 py-1 rounded-full transition-colors"
            >
              {t(`commands.presets.${p.key}`)}
            </button>
          ))}
        </div>

        <form onSubmit={handleSend} className="space-y-3">
          <input
            type="text"
            required
            value={command}
            onChange={(e) => setCommand(e.target.value)}
            placeholder="REBOOT"
            className="input w-full font-mono text-sm"
          />

          {error && (
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
              {error}
            </p>
          )}

          <div className="flex items-center gap-2">
            <button
              type="submit"
              disabled={sending}
              className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium py-2 rounded-lg transition-colors"
            >
              {sending ? t('commands.queuing') : t('commands.queue_button')}
            </button>
            <DeviceWriteHint />
          </div>
        </form>
      </Section>

      {/* Revocations pulled out and coloured as the hazard they are — an
          outstanding `DATA DELETE` means somebody may still be able to open
          this door, exactly the distinction E8 exists to keep visible.
          Grouped server-side (E13), one card per person rather than one per
          `DATA DELETE` command — the duplication that used to let an
          operator cancel half a revocation with one click. */}
      {revocationGroups.length > 0 && (
        <Section title={t('commands.revocations_title')}>
          <div className="border-2 border-red-300 bg-red-50 rounded-lg p-3 space-y-2">
            <p className="text-xs font-semibold text-red-800">
              {t('commands.revocations_warning')}
            </p>
            {revocationGroups.map((group) => (
              <RevocationCard
                key={`${group.device_sn}:${group.user_id}`}
                group={group}
                title={t('commands.pin', { pin: group.user_id })}
                cancelLabel={
                  group.still_open
                    ? t('commands.cancel_keeps_access', { pin: group.user_id })
                    : group.user?.outstanding || group.userauthorize?.outstanding
                      ? t('commands.cancel_leftover')
                      : null
                }
                onCancelled={handleRevocationCancelled}
                onError={handleRevocationCancelError}
              />
            ))}
          </div>
        </Section>
      )}

      <Section
        title={t('commands.outstanding_title')}
        hint={t('commands.outstanding_hint')}
      >
        {loadingLists ? (
          <p className="text-xs text-gray-400">{t('common.loading')}</p>
        ) : otherOutbox.length === 0 ? (
          <p className="text-xs text-gray-400">{t('commands.nothing_outstanding')}</p>
        ) : (
          <div className="space-y-2">
            {otherOutbox.map((row) => (
              <div key={row.id} className="bg-gray-50 rounded-lg px-3 py-2.5 text-sm">
                <div className="flex items-center gap-2 mb-1">
                  <Pill tone={row.status === 'sent' ? 'blue' : 'gray'}>
                    {row.status === 'sent' ? t('commands.status_sent') : t('commands.status_pending')}
                  </Pill>
                  <span className="text-xs text-gray-500 flex-1">
                    {row.status === 'sent'
                      ? t('commands.sent_line', { attempts: row.attempts, retry: until(row.next_attempt_at), sent: since(row.sent_at) })
                      : t('commands.pending_line', { queued: since(row.created_at) })}
                  </span>
                </div>
                <p className="text-xs font-mono text-gray-500 truncate">{row.command}</p>
                {confirmCancelId === row.id ? (
                  <div className="mt-2 text-xs bg-amber-50 border border-amber-200 rounded px-2 py-1.5 text-amber-800">
                    {t('commands.cancel_sent_warning')}
                    <div className="flex gap-3 mt-1.5">
                      <button
                        onClick={() => doCancel(row)}
                        className="text-red-700 font-semibold hover:underline"
                      >
                        {t('commands.cancel_anyway')}
                      </button>
                      <button
                        onClick={() => setConfirmCancelId(null)}
                        className="text-gray-500 hover:underline"
                      >
                        {t('commands.never_mind')}
                      </button>
                    </div>
                  </div>
                ) : (
                  <button
                    onClick={() => handleCancelClick(row)}
                    disabled={!!busy[`out-${row.id}`]}
                    className="mt-1.5 text-xs text-gray-500 hover:text-gray-800 px-2 py-1 rounded hover:bg-gray-100 disabled:opacity-40 transition-colors"
                  >
                    {busy[`out-${row.id}`]
                      ? t('common.cancelling')
                      : row.status === 'sent'
                        ? t('commands.cancel_sent')
                        : t('commands.cancel_pending')}
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section
        title={t('commands.history_title')}
        hint={t('commands.history_hint')}
      >
        {loadingLists ? (
          <p className="text-xs text-gray-400">{t('common.loading')}</p>
        ) : history.length === 0 ? (
          <p className="text-xs text-gray-400">{t('commands.no_history')}</p>
        ) : (
          <div className="space-y-2">
            {history.map((row) => {
              const outcome = historyOutcome(row)
              const revocation = isRevocation(row.command)
              const canRetry = row.outcome === 'failed'
              return (
                <div key={row.id} className="bg-gray-50 rounded-lg px-3 py-2.5 text-sm">
                  <div className="flex items-center gap-2 mb-1">
                    <Pill tone={outcome.tone}>{outcome.label}</Pill>
                    {revocation && <Pill tone="red">{t('commands.revocation_pill')}</Pill>}
                    <span className="text-xs text-gray-400 flex-1 text-right">
                      {t('commands.concluded', { time: since(row.concluded_at) })}
                    </span>
                  </div>
                  <p className="text-xs font-mono text-gray-500 truncate">{row.command}</p>
                  {row.last_error && (
                    <p className="text-xs text-gray-500 mt-1">{row.last_error}</p>
                  )}
                  {canRetry && (
                    confirmRetryId === row.id ? (
                      <div className="mt-2 text-xs bg-amber-50 border border-amber-200 rounded px-2 py-1.5 text-amber-800">
                        {outcome.verdict === 'refused'
                          ? t('commands.retry_refused_warning', { code: row.return_code })
                          : t('commands.retry_unconfirmed_warning', { code: row.return_code })}
                        <div className="flex gap-3 mt-1.5">
                          <button
                            onClick={() => doRetry(row)}
                            className="text-amber-900 font-semibold hover:underline"
                          >
                            {t('commands.retry_anyway')}
                          </button>
                          <button
                            onClick={() => setConfirmRetryId(null)}
                            className="text-gray-500 hover:underline"
                          >
                            {t('commands.never_mind')}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <span className="inline-flex items-center gap-1 mt-1.5">
                        <button
                          onClick={() => handleRetryClick(row)}
                          disabled={!!busy[`log-${row.id}`]}
                          className="text-xs text-blue-600 hover:text-blue-800 px-2 py-1 rounded hover:bg-blue-50 disabled:opacity-40 transition-colors"
                        >
                          {busy[`log-${row.id}`] ? t('commands.requeuing') : t('commands.retry')}
                        </button>
                        <DeviceWriteHint />
                      </span>
                    )
                  )}
                </div>
              )
            })}
          </div>
        )}
      </Section>
    </Drawer>
  )
}
