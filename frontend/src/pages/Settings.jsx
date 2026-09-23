import { useState, useEffect, useCallback } from 'react'
import { api, saveBlobAs } from '../api'
import { useAuth } from '../auth'

function Field({ label, hint, children }) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-700 mb-1">
        {label}
        {hint && <span className="ml-1.5 text-xs font-normal text-gray-400">{hint}</span>}
      </label>
      {children}
    </div>
  )
}

function StatusRow({ label, value, mono, editable, onEdit }) {
  return (
    <div className="flex justify-between items-center py-2.5 border-b border-gray-100 last:border-0 text-sm">
      <span className="text-gray-500">{label}</span>
      <div className="flex items-center gap-2">
        <span className={`text-gray-900 ${mono ? 'font-mono text-xs' : ''}`}>{value ?? '—'}</span>
        {editable && (
          <button
            onClick={onEdit}
            className="text-xs text-blue-500 hover:text-blue-700 underline"
          >
            Edit
          </button>
        )}
      </div>
    </div>
  )
}

function Toast({ message, type = 'success', onDismiss }) {
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

// Restoring attendance history out of a ZKTime .NET backup (F1).
//
// The shape of this panel follows the one constraint that makes the feature
// necessary: an `acc` terminal's transaction table cannot be queried, so a
// ZKTime backup is the only copy of history that predates this server. That
// makes the file precious and the operation one-way, which is why nothing
// here happens on a single click — the file is uploaded and described first,
// and a second, separate button is what writes.
const PART_LABELS = {
  employees: 'Employees',
  attendance: 'Attendance',
  templates: 'Fingerprint templates',
}

function partHint(part) {
  if (part === 'attendance') {
    return 'The punches themselves. This is the part that cannot be re-read from the terminal.'
  }
  if (part === 'employees') {
    return 'Names, PINs and cards. Fills in blanks; never overwrites what you typed.'
  }
  return 'Stored as a copy only. Whether a ZKTime template can be pushed back to a terminal has not been verified, so nothing sends them automatically.'
}

function Count({ label, value, muted }) {
  return (
    <div className="flex justify-between py-1.5 text-sm">
      <span className="text-gray-500">{label}</span>
      <span className={muted ? 'text-gray-400' : 'font-medium text-gray-900'}>
        {typeof value === 'number' ? value.toLocaleString() : value ?? '—'}
      </span>
    </div>
  )
}

// One stroke-based glyph per panel, so Backup and Restore are told apart at a
// glance rather than by reading two similar headings. Paths are inline
// because public/icons.svg is a social-link sprite and has nothing like these.
const PANEL_ICONS = {
  backup: 'M12 16.5V3m0 13.5-4-4m4 4 4-4M3.75 15.75v2.25a2.25 2.25 0 0 0 2.25 2.25h12a2.25 2.25 0 0 0 2.25-2.25v-2.25',
  restore: 'M12 3v13.5m0-13.5 4 4m-4-4-4 4M3.75 15.75v2.25a2.25 2.25 0 0 0 2.25 2.25h12a2.25 2.25 0 0 0 2.25-2.25v-2.25',
  sync: 'M16.023 9.348h4.992V4.356m-4.992 4.992-2.65-2.65a7.5 7.5 0 0 0-11.667 2.65m.01 5.304H2.985v4.992m0-4.992 2.65 2.65a7.5 7.5 0 0 0 11.667-2.65',
  audit: 'M9 12h6m-6 4h6m2 5H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5.586a1 1 0 0 1 .707.293l5.414 5.414a1 1 0 0 1 .293.707V19a2 2 0 0 1-2 2Z',
}

function PanelIcon({ name, tone = 'text-gray-400' }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"
         className={`w-5 h-5 shrink-0 ${tone}`}>
      <path d={PANEL_ICONS[name]} />
    </svg>
  )
}

function PanelHeader({ icon, title, subtitle, tone, right }) {
  return (
    <div className="flex items-start justify-between gap-3 px-5 py-4 border-b border-gray-200">
      <div className="flex items-start gap-2.5 min-w-0">
        <PanelIcon name={icon} tone={tone} />
        <div className="min-w-0">
          <p className="font-medium text-gray-900 leading-5">{title}</p>
          <p className="text-xs text-gray-400 mt-0.5">{subtitle}</p>
        </div>
      </div>
      {right}
    </div>
  )
}

// A compact figure. Three of these in a row replace six label/value lines.
function Stat({ label, value }) {
  return (
    <div className="min-w-0">
      <p className="text-base font-semibold text-gray-900 tabular-nums truncate">
        {typeof value === 'number' ? value.toLocaleString() : value ?? '—'}
      </p>
      <p className="text-xs text-gray-400 truncate">{label}</p>
    </div>
  )
}

// Warnings are shown; notes are true, wanted occasionally, and folded away.
// Five paragraphs of equal weight is how the one that matters gets skipped.
function Messages({ warnings, notes }) {
  const [open, setOpen] = useState(false)
  if (!warnings?.length && !notes?.length) return null
  return (
    <div className="space-y-2">
      {warnings?.map((note, i) => (
        <p key={i} className="text-xs text-amber-900 bg-amber-50 border border-amber-100
                              rounded-lg px-3 py-2 leading-relaxed">
          {note}
        </p>
      ))}
      {notes?.length > 0 && (
        <div>
          <button
            onClick={() => setOpen((v) => !v)}
            className="text-xs text-gray-500 hover:text-gray-700 underline"
          >
            {open ? 'Hide details' : `Details (${notes.length})`}
          </button>
          {open && (
            <div className="mt-2 space-y-2">
              {notes.map((note, i) => (
                <p key={i} className="text-xs text-gray-500 bg-gray-50 rounded-lg
                                      px-3 py-2 leading-relaxed">
                  {note}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Backup — one button, out comes a .db the operator chooses a home for.
// ---------------------------------------------------------------------------
//
// No upload here on purpose. Most of a ZKTime database is ZKTime's own
// configuration and cannot be generated, so an export is built on a template
// the server holds; a restore saves its own file as that template, which is
// why Backup normally just works.
function BackupPanel({ showToast }) {
  const [template, setTemplate] = useState(null)
  const [pruneMissing, setPruneMissing] = useState(false)
  const [busy, setBusy] = useState(null)      // 'build'|'save'|'template'
  const [built, setBuilt] = useState(null)

  const load = useCallback(() => {
    api.backup.template().then(setTemplate).catch(() => setTemplate({ configured: false }))
  }, [])

  useEffect(() => { load() }, [load])

  async function handleBuild() {
    setBusy('build')
    setBuilt(null)
    try {
      setBuilt(await api.backup.buildExport({ prune_missing: pruneMissing }))
    } catch (err) {
      showToast(err.message, 'error')
    } finally {
      setBusy(null)
    }
  }

  async function handleSave() {
    setBusy('save')
    try {
      const { blob, filename } = await api.backup.downloadExport(built.token)
      if (await saveBlobAs(blob, filename || built.filename)) showToast('Backup saved')
    } catch (err) {
      showToast(err.message, 'error')
    } finally {
      setBusy(null)
    }
  }

  async function handleTemplateFile(event) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    setBusy('template')
    try {
      setTemplate(await api.backup.setTemplate(file))
      showToast('Template saved')
    } catch (err) {
      showToast(err.message, 'error')
    } finally {
      setBusy(null)
    }
  }

  const ready = template?.configured && template?.usable

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden flex flex-col">
      <PanelHeader
        icon="backup"
        tone="text-blue-500"
        title="Backup"
        subtitle="Build a ZKTime .db from this app's data and choose where to save it."
      />

      {!built && (
        <div className="px-5 py-4 space-y-4 flex-1">
          {ready && (
            <>
              <button
                onClick={handleBuild}
                disabled={busy !== null}
                className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white
                           text-sm font-medium px-4 py-2 rounded-lg transition-colors"
              >
                {busy === 'build' ? 'Building…' : 'Create backup'}
              </button>

              <label className="flex gap-2.5 items-center cursor-pointer">
                <input
                  type="checkbox"
                  checked={pruneMissing}
                  onChange={(e) => setPruneMissing(e.target.checked)}
                />
                <span className="text-xs text-gray-600">
                  Remove people this app no longer has
                </span>
              </label>

              {/* The one thing about an export most likely to surprise, in a
                  line rather than a paragraph: the data is this app's, the
                  configuration around it is not. */}
              <p className="text-xs text-gray-400 leading-relaxed">
                Built on the ZKTime backup saved{' '}
                {new Date(template.saved_at).toLocaleDateString()} — its settings,
                shifts, departments and logins are carried over untouched.
              </p>
            </>
          )}

          {template && !template.configured && (
            <div className="space-y-2.5">
              <p className="text-xs text-amber-900 bg-amber-50 border border-amber-100
                            rounded-lg px-3 py-2 leading-relaxed">
                No template yet. Most of a ZKTime database is its own configuration,
                which this app cannot invent — so a backup is built on a real one.
                Restore a ZKTime .db and this fills in by itself, or pick one here.
              </p>
              <input
                type="file"
                accept=".db,application/octet-stream"
                onChange={handleTemplateFile}
                disabled={busy === 'template'}
                className="block w-full text-xs text-gray-600 file:mr-3 file:py-1.5 file:px-3
                           file:rounded-lg file:border-0 file:text-xs file:font-medium
                           file:bg-gray-100 file:text-gray-700 hover:file:bg-gray-200
                           file:cursor-pointer disabled:opacity-50"
              />
            </div>
          )}

          {template?.configured && !template.usable && (
            <p className="text-xs text-red-900 bg-red-50 border border-red-100
                          rounded-lg px-3 py-2 leading-relaxed">
              The stored template can no longer be read: {template.problem}
            </p>
          )}
        </div>
      )}

      {built && (
        <div className="px-5 py-4 space-y-4 flex-1">
          <div className="flex items-baseline justify-between gap-3">
            <p className="text-sm font-medium text-gray-900 truncate">{built.filename}</p>
            <p className="text-xs text-gray-400 shrink-0">
              {(built.size / 1e6).toFixed(1)} MB
            </p>
          </div>

          <div className="grid grid-cols-3 gap-3 rounded-lg bg-gray-50 px-4 py-3">
            <Stat label="Punches" value={built.punches.written} />
            <Stat
              label="People"
              value={built.employees.updated + built.employees.created + built.employees.stubs}
            />
            <Stat label="Fingerprints" value={built.templates.written} />
          </div>

          <Messages warnings={built.warnings} notes={built.notes} />

          <div className="flex gap-2">
            <button
              onClick={handleSave}
              disabled={busy !== null}
              className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white
                         text-sm font-medium px-4 py-2 rounded-lg transition-colors"
            >
              {busy === 'save' ? 'Saving…' : 'Save as…'}
            </button>
            <button
              onClick={() => { setBuilt(null); load() }}
              className="border border-gray-300 text-gray-700 hover:bg-gray-50
                         text-sm font-medium px-4 py-2 rounded-lg transition-colors"
            >
              Done
            </button>
          </div>
        </div>
      )}
    </div>
  )
}


// ---------------------------------------------------------------------------
// Restore — upload a ZKTime .db and read it into this app.
// ---------------------------------------------------------------------------
function RestorePanel({ showToast, onRestored }) {
  const [devices, setDevices] = useState([])
  const [staged, setStaged] = useState(null)   // { token, size, preview }
  const [deviceSn, setDeviceSn] = useState('')
  const [terminalId, setTerminalId] = useState('')
  const [parts, setParts] = useState({ employees: true, attendance: true, templates: false })
  const [busy, setBusy] = useState(null)       // 'upload' | 'restore' | 'discard'
  const [result, setResult] = useState(null)

  useEffect(() => {
    api.devices.list().then(setDevices).catch(() => setDevices([]))
  }, [])

  function reset() {
    setStaged(null)
    setResult(null)
  }

  async function handleFile(event) {
    const file = event.target.files?.[0]
    // Clearing the input is what lets the same file be picked twice in a row
    // after a failed upload — without it the change event never fires again.
    event.target.value = ''
    if (!file) return

    setBusy('upload')
    setResult(null)
    try {
      const data = await api.backup.upload(file)
      setStaged(data)
      // Preselect only what the server could resolve for itself: the terminal
      // whose serial matches a device registered here. A guess would be worse
      // than an empty box, because punches restored onto the wrong device are
      // labelled with the wrong timezone.
      const match = data.preview.terminals.find((t) => t.registered_here)
      setTerminalId(data.preview.terminals.length === 1
        ? String(data.preview.terminals[0].terminal_id)
        : match ? String(match.terminal_id) : '')
      setDeviceSn(match?.serial || '')
    } catch (err) {
      showToast(err.message, 'error')
    } finally {
      setBusy(null)
    }
  }

  async function handleDiscard() {
    if (!staged) return
    setBusy('discard')
    try {
      await api.backup.discard(staged.token)
    } catch {
      // The file expires by itself; a failed discard is not worth an error
      // the operator can do nothing about.
    } finally {
      reset()
      setBusy(null)
    }
  }

  async function handleRestore() {
    const chosen = Object.keys(parts).filter((p) => parts[p])
    if (!deviceSn || chosen.length === 0) return

    setBusy('restore')
    try {
      const summary = await api.backup.restore({
        token: staged.token,
        device_sn: deviceSn,
        terminal_id: terminalId === '' ? null : Number(terminalId),
        parts: chosen,
      })
      setResult(summary)
      setStaged(null)       // the server deleted the staged file on success
      showToast('Restore finished')
      // The restored file has just become the backup template, so the panel
      // beside this one has something to build on now.
      if (summary.template_saved) onRestored?.()
    } catch (err) {
      showToast(err.message, 'error')
    } finally {
      setBusy(null)
    }
  }

  const preview = staged?.preview
  const chosenCount = Object.values(parts).filter(Boolean).length

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden flex flex-col">
      <PanelHeader
        icon="restore"
        tone="text-emerald-600"
        title="Restore"
        subtitle="Read a ZKTime .db into this app. Adds only; running it twice changes nothing."
      />

      {!staged && !result && (
        <div className="px-5 py-4 flex-1">
          <input
            type="file"
            accept=".db,application/octet-stream"
            onChange={handleFile}
            disabled={busy === 'upload'}
            className="block w-full text-sm text-gray-600 file:mr-3 file:py-2 file:px-4
                       file:rounded-lg file:border-0 file:text-sm file:font-medium
                       file:bg-emerald-50 file:text-emerald-700 hover:file:bg-emerald-100
                       file:cursor-pointer disabled:opacity-50"
          />
          {busy === 'upload' && (
            <p className="text-xs text-gray-500 mt-2">Uploading and reading the file…</p>
          )}
        </div>
      )}

      {preview && (
        <div className="px-5 py-4 space-y-4 flex-1">
          <div className="grid grid-cols-3 gap-3 rounded-lg bg-gray-50 px-4 py-3">
            <Stat label="People" value={preview.employees} />
            <Stat label="Punches" value={preview.punches.total} />
            <Stat
              label="Covering"
              value={preview.punches.first
                ? `${preview.punches.first.slice(0, 7)} → ${preview.punches.last.slice(0, 7)}`
                : '—'}
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Terminal in file
              </label>
              <select
                value={terminalId}
                onChange={(e) => {
                  setTerminalId(e.target.value)
                  const t = preview.terminals.find((x) => String(x.terminal_id) === e.target.value)
                  if (t?.registered_here) setDeviceSn(t.serial)
                }}
                className="w-full border border-gray-300 rounded-lg px-2.5 py-2 text-sm"
              >
                <option value="">All terminals</option>
                {preview.terminals.map((t) => (
                  <option key={t.terminal_id} value={t.terminal_id}>
                    {t.name || t.serial || 'Unnamed'} ({t.punches.toLocaleString()})
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">
                Onto device
              </label>
              <select
                value={deviceSn}
                onChange={(e) => setDeviceSn(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-2.5 py-2 text-sm"
              >
                <option value="">Choose…</option>
                {devices.map((d) => (
                  <option key={d.serial_number} value={d.serial_number}>
                    {d.name || d.serial_number} ({d.timezone})
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Labelled, because condensing this to a bare row of checkboxes
              made it easy to miss — and one of the three (templates) is off
              by default, so an operator who does not see the row gets a
              backup with no fingerprints in it and no idea why. */}
          <div>
            <p className="text-xs font-medium text-gray-600 mb-1.5">What to restore</p>
            <div className="flex flex-wrap gap-x-4 gap-y-2">
              {Object.keys(PART_LABELS).map((part) => (
                <label key={part} className="flex gap-2 items-center cursor-pointer"
                       title={partHint(part)}>
                  <input
                    type="checkbox"
                    checked={parts[part]}
                    onChange={(e) => setParts({ ...parts, [part]: e.target.checked })}
                  />
                  <span className="text-xs text-gray-700">{PART_LABELS[part]}</span>
                </label>
              ))}
            </div>
          </div>

          {/* The failure this warns about is invisible rather than loud: the
              punches import fine and the Attendance screen then hides every
              one whose PIN it cannot put a name to. */}
          {parts.attendance && !parts.employees && (
            <p className="text-xs text-amber-900 bg-amber-50 border border-amber-100
                          rounded-lg px-3 py-2 leading-relaxed">
              Without Employees, punches for anyone off the roster are stored but
              hidden on the Attendance screen.
            </p>
          )}

          {/* Said once, near the device picker it explains. */}
          <p className="text-xs text-gray-400">
            Times are stored exactly as the file records them, labelled with the
            chosen device&rsquo;s timezone. Nothing is shifted.
          </p>

          <div className="flex gap-2">
            <button
              onClick={handleRestore}
              disabled={busy !== null || !deviceSn || chosenCount === 0}
              className="bg-emerald-600 hover:bg-emerald-700 disabled:opacity-50 text-white
                         text-sm font-medium px-4 py-2 rounded-lg transition-colors"
            >
              {busy === 'restore' ? 'Restoring…' : 'Restore'}
            </button>
            <button
              onClick={handleDiscard}
              disabled={busy !== null}
              className="border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50
                         text-sm font-medium px-4 py-2 rounded-lg transition-colors"
            >
              Discard
            </button>
          </div>
        </div>
      )}

      {result && (
        <div className="px-5 py-4 space-y-4 flex-1">
          <div className="grid grid-cols-3 gap-3 rounded-lg bg-gray-50 px-4 py-3">
            <Stat label="Punches added" value={result.attendance?.inserted ?? '—'} />
            <Stat label="People added" value={result.employees?.created ?? '—'} />
            <Stat
              label="Already present"
              value={result.attendance?.already_present ?? '—'}
            />
          </div>

          <Messages warnings={result.warnings} notes={result.notes} />

          <button onClick={reset} className="text-sm text-blue-600 hover:text-blue-800 underline">
            Restore another file
          </button>
        </div>
      )}
    </div>
  )
}


const AUDIT_PAGE_SIZE = 20

function AuditLog() {
  const [filters, setFilters] = useState({ actor: '', action: '', from_date: '', to_date: '' })
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback((currentFilters, currentOffset) => {
    setLoading(true)
    api.audit
      .list({ ...currentFilters, limit: AUDIT_PAGE_SIZE, offset: currentOffset })
      .then(setData)
      .catch(() => setData(null))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load(filters, offset)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [offset])

  function applyFilters(e) {
    e.preventDefault()
    setOffset(0)
    load(filters, 0)
  }

  const items = data?.items || []
  const total = data?.total || 0

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden mt-6">
      <div className="px-5 py-4 border-b border-gray-200">
        <p className="font-medium text-gray-900">Audit Log</p>
        <p className="text-xs text-gray-400 mt-0.5">
          Privileged and physical actions, attributed to an actor and source IP.
        </p>
      </div>

      <form onSubmit={applyFilters} className="px-5 py-4 border-b border-gray-100 grid grid-cols-2 sm:grid-cols-5 gap-3">
        <input
          type="text"
          placeholder="Actor"
          value={filters.actor}
          onChange={(e) => setFilters((f) => ({ ...f, actor: e.target.value }))}
          className="input text-sm"
        />
        <input
          type="text"
          placeholder="Action"
          value={filters.action}
          onChange={(e) => setFilters((f) => ({ ...f, action: e.target.value }))}
          className="input text-sm"
        />
        <input
          type="date"
          aria-label="From date"
          value={filters.from_date}
          onChange={(e) => setFilters((f) => ({ ...f, from_date: e.target.value }))}
          className="input text-sm"
        />
        <input
          type="date"
          aria-label="To date"
          value={filters.to_date}
          onChange={(e) => setFilters((f) => ({ ...f, to_date: e.target.value }))}
          className="input text-sm"
        />
        <button
          type="submit"
          className="bg-gray-900 hover:bg-gray-800 text-white text-xs font-medium py-2 rounded-lg transition-colors"
        >
          Filter
        </button>
      </form>

      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-400 border-b border-gray-100">
              <th className="px-5 py-2 font-medium">Time</th>
              <th className="px-5 py-2 font-medium">Actor</th>
              <th className="px-5 py-2 font-medium">Action</th>
              <th className="px-5 py-2 font-medium">Target</th>
              <th className="px-5 py-2 font-medium">IP</th>
              <th className="px-5 py-2 font-medium">Detail</th>
            </tr>
          </thead>
          <tbody>
            {items.map((row) => (
              <tr key={row.id} className="border-b border-gray-50 last:border-0">
                <td className="px-5 py-2 text-xs text-gray-500 whitespace-nowrap">
                  {new Date(row.created_at).toLocaleString()}
                </td>
                <td className="px-5 py-2 text-xs text-gray-900">{row.actor}</td>
                <td className="px-5 py-2 text-xs font-mono text-gray-700">{row.action}</td>
                <td className="px-5 py-2 text-xs text-gray-500 font-mono">{row.target || '—'}</td>
                <td className="px-5 py-2 text-xs text-gray-500 font-mono">{row.ip || '—'}</td>
                <td className="px-5 py-2 text-xs text-gray-500 break-all">{row.detail || '—'}</td>
              </tr>
            ))}
            {!loading && items.length === 0 && (
              <tr>
                <td colSpan={6} className="px-5 py-6 text-center text-xs text-gray-400">
                  No matching entries
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between px-5 py-3 border-t border-gray-100 text-xs text-gray-500">
        <span>{total} total</span>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - AUDIT_PAGE_SIZE))}
            className="border border-gray-300 disabled:opacity-40 text-gray-600 px-3 py-1 rounded-lg"
          >
            Prev
          </button>
          <button
            type="button"
            disabled={offset + AUDIT_PAGE_SIZE >= total}
            onClick={() => setOffset(offset + AUDIT_PAGE_SIZE)}
            className="border border-gray-300 disabled:opacity-40 text-gray-600 px-3 py-1 rounded-lg"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  )
}

export default function Settings() {
  const { user } = useAuth()
  const [cfg, setCfg] = useState(null)
  const [form, setForm] = useState(null)        // null = view mode, object = edit mode
  const [editId, setEditId] = useState(null)    // editing last_synced_id inline
  const [running, setRunning] = useState(false)
  const [saving, setSaving] = useState(false)
  const [toggling, setToggling] = useState(false)
  const [toast, setToast] = useState(null)

  const [backupKey, setBackupKey] = useState(0)

  const showToast = (msg, type = 'success') => setToast({ message: msg, type })

  const load = useCallback(() => {
    api.hrmSync.status().then((data) => {
      setCfg(data)
    }).catch(() => {})
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 15_000)
    return () => clearInterval(t)
  }, [load])

  function startEdit() {
    setForm({
      endpoint:         cfg.endpoint || '',
      // Write-only: the server never tells the browser what the secret is,
      // only whether one is set (cfg.secret_set). Blank here means "leave
      // it alone" — see handleSave.
      secret:           '',
      location_id:      cfg.location_id || '1',
      interval_seconds: cfg.interval_seconds ?? 300,
      // `enabled` is deliberately not here. Pausing the sync is not a
      // configuration detail to be discovered inside a form — it is the one
      // control an operator reaches for in a hurry, so it lives in the header
      // as its own button (see handleToggleEnabled).
    })
  }

  function cancelEdit() {
    setForm(null)
  }

  async function handleSave(e) {
    e.preventDefault()
    setSaving(true)
    try {
      const payload = { ...form }
      // Don't send an empty secret — the backend would ignore it anyway,
      // but keep the intent explicit here too: blank means unchanged.
      if (!payload.secret) delete payload.secret
      const updated = await api.hrmSync.update(payload)
      setCfg(updated)
      setForm(null)
      showToast('Configuration saved')
    } catch (err) {
      showToast(err.message, 'error')
    } finally {
      setSaving(false)
    }
  }

  async function handleToggleEnabled() {
    const next = !cfg.enabled
    setToggling(true)
    try {
      const updated = await api.hrmSync.update({ enabled: next })
      setCfg(updated)
      showToast(next ? 'Sync resumed' : 'Sync paused')
    } catch (err) {
      showToast(err.message, 'error')
    } finally {
      setToggling(false)
    }
  }

  async function handleSaveLastId(e) {
    e.preventDefault()
    const val = parseInt(editId, 10)
    if (isNaN(val) || val < 0) return
    try {
      const updated = await api.hrmSync.update({ last_synced_id: val })
      setCfg(updated)
      setEditId(null)
      showToast('Last synced ID updated')
    } catch (err) {
      showToast(err.message, 'error')
    }
  }

  async function handleRunNow() {
    setRunning(true)
    try {
      await api.hrmSync.run()
      showToast('Sync started')
      setTimeout(load, 3000)
    } catch (err) {
      showToast(err.message, 'error')
    } finally {
      setRunning(false)
    }
  }

  const isConfigured = cfg?.endpoint && cfg?.secret_set

  return (
    <div className="max-w-6xl">
      <h1 className="text-xl font-semibold text-gray-900 mb-6">Settings</h1>

      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">

        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-gray-200">
          <div>
            <p className="font-medium text-gray-900">HRM Attendance Sync</p>
            <p className="text-xs text-gray-400 mt-0.5">
              Pushes new attendance records to your HRM server on a schedule.
            </p>
          </div>
          <div className="flex items-center gap-2">
            {cfg && (
              <span className={`text-xs font-medium px-2.5 py-1 rounded-full ${
                cfg.enabled && isConfigured
                  ? 'bg-green-100 text-green-700'
                  : isConfigured
                  ? 'bg-yellow-100 text-yellow-700'
                  : 'bg-gray-100 text-gray-500'
              }`}>
                {cfg.enabled && isConfigured ? 'Active' : isConfigured ? 'Paused' : 'Not configured'}
              </span>
            )}
            {/* Pause/Resume is the control an operator needs to find fast —
                it stops records leaving for the HRM. It was a checkbox buried
                in the config form; it is now the obvious button next to the
                status it changes. Admin-only, matching the PUT it calls. */}
            {cfg && !form && isConfigured && user?.role === 'admin' && (
              <button
                onClick={handleToggleEnabled}
                disabled={toggling}
                data-testid="hrm-toggle"
                title={cfg.enabled
                  ? 'Stop pushing records to the HRM until resumed'
                  : 'Start pushing records to the HRM again'}
                className={`text-sm font-medium px-3 py-1.5 rounded-lg border transition-colors disabled:opacity-50 ${
                  cfg.enabled
                    ? 'border-amber-300 text-amber-800 hover:bg-amber-50'
                    : 'border-green-300 text-green-800 hover:bg-green-50'
                }`}
              >
                {toggling ? 'Working…' : cfg.enabled ? 'Pause' : 'Resume'}
              </button>
            )}
            {cfg && !form && (
              <button
                onClick={startEdit}
                className="text-sm text-blue-600 hover:text-blue-800 font-medium"
              >
                Configure
              </button>
            )}
          </div>
        </div>

        {cfg === null && (
          <div className="p-6 text-sm text-gray-400">Loading…</div>
        )}

        {/* Config form */}
        {form && (
          <form onSubmit={handleSave} className="p-5 space-y-4 border-b border-gray-100 max-w-2xl">
            <Field label="Endpoint URL">
              <input
                type="url"
                value={form.endpoint}
                onChange={(e) => setForm((f) => ({ ...f, endpoint: e.target.value }))}
                placeholder="http://hrm.server/sync_attendance/server.php"
                className="input w-full text-sm"
              />
            </Field>

            <Field
              label="Secret Key"
              hint={cfg.secret_set ? 'leave blank to keep the current secret' : undefined}
            >
              <input
                type="password"
                autoComplete="new-password"
                value={form.secret}
                onChange={(e) => setForm((f) => ({ ...f, secret: e.target.value }))}
                placeholder={cfg.secret_set ? '••••••••' : 'Shared secret configured in server.php'}
                className="input w-full text-sm"
              />
            </Field>

            <div className="grid grid-cols-2 gap-3">
              <Field label="Location ID">
                <input
                  type="text"
                  value={form.location_id}
                  onChange={(e) => setForm((f) => ({ ...f, location_id: e.target.value }))}
                  className="input w-full text-sm"
                />
              </Field>

              <Field label="Interval" hint="seconds">
                <input
                  type="number"
                  min={60}
                  value={form.interval_seconds}
                  onChange={(e) => setForm((f) => ({ ...f, interval_seconds: Number(e.target.value) }))}
                  className="input w-full text-sm"
                />
              </Field>
            </div>

            <div className="flex gap-3 pt-1">
              <button
                type="button"
                onClick={cancelEdit}
                className="flex-1 border border-gray-300 text-gray-700 hover:bg-gray-50 text-sm font-medium py-2 rounded-lg transition-colors"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={saving}
                className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium py-2 rounded-lg transition-colors"
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
            </div>
          </form>
        )}

        {/* Status. Same measure as the form above: a label-left/value-right
            row is unreadable when the two ends are 1100px apart. */}
        {cfg && !form && (
          <div className="px-5 max-w-2xl">
            <div className="flex justify-between items-center py-2.5 border-b border-gray-100 text-sm">
              <span className="text-gray-500">Secret</span>
              <span
                className={`inline-flex px-2.5 py-0.5 rounded-full text-xs font-medium ${
                  cfg.secret_set ? 'bg-blue-50 text-blue-700' : 'bg-gray-100 text-gray-500'
                }`}
              >
                {cfg.secret_set ? 'Set' : 'Not set'}
              </span>
            </div>
            <StatusRow
              label="Last run"
              // A server event, genuinely UTC, so the viewer's own locale is
              // the right lens for it — unlike a punch time, which is the
              // device's wall-clock and is never re-zoned.
              value={cfg.last_run_at ? new Date(cfg.last_run_at).toLocaleString() : null}
            />
            <StatusRow
              label="Last synced ID"
              value={cfg.last_synced_id ?? 0}
              mono
              editable={editId === null}
              onEdit={() => setEditId(String(cfg.last_synced_id ?? 0))}
            />
            {editId !== null && (
              <form onSubmit={handleSaveLastId} className="py-3 flex gap-2 border-b border-gray-100">
                <input
                  type="number"
                  min={0}
                  value={editId}
                  onChange={(e) => setEditId(e.target.value)}
                  className="input flex-1 text-sm font-mono"
                  autoFocus
                />
                <button
                  type="submit"
                  className="bg-blue-600 text-white text-xs font-medium px-3 rounded-lg"
                >
                  Update
                </button>
                <button
                  type="button"
                  onClick={() => setEditId(null)}
                  className="border border-gray-300 text-gray-600 text-xs font-medium px-3 rounded-lg"
                >
                  Cancel
                </button>
              </form>
            )}
            <StatusRow label="Records pushed (last run)" value={cfg.records_last_push?.toLocaleString()} />
            <StatusRow label="Total records pushed" value={cfg.total_pushed?.toLocaleString()} />
            <StatusRow label="Interval" value={cfg.interval_seconds ? `${cfg.interval_seconds}s` : null} />
            <StatusRow label="Location ID" value={cfg.location_id} />
            {cfg.last_error && (
              <div className="py-3 border-b border-gray-100">
                <p className="text-xs font-medium text-red-600 mb-1">Last error</p>
                <p className="text-xs text-red-500 font-mono break-all">{cfg.last_error}</p>
              </div>
            )}
          </div>
        )}

        {/* Actions */}
        {cfg && !form && isConfigured && (
          <div className="px-5 py-4 border-t border-gray-100">
            <button
              onClick={handleRunNow}
              disabled={running}
              className="bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium px-4 py-2 rounded-lg transition-colors"
            >
              {running ? 'Starting…' : 'Sync Now'}
            </button>
          </div>
        )}
      </div>

      {user?.role === 'admin' && (
        <div className="grid gap-6 md:grid-cols-2 items-start mt-6">
          <BackupPanel key={backupKey} showToast={showToast} />
          <RestorePanel showToast={showToast} onRestored={() => setBackupKey((n) => n + 1)} />
        </div>
      )}

      {user?.role === 'admin' && <AuditLog />}

      {toast && (
        <Toast message={toast.message} type={toast.type} onDismiss={() => setToast(null)} />
      )}
    </div>
  )
}
