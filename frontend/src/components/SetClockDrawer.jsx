import { useState, useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { api } from '../api'
import { serverMessage } from '../i18n'
import { formatDateTime } from '../format'
import Drawer from './Drawer'

export default function SetClockDrawer({ device, onClose, showToast }) {
  const { t } = useTranslation()
  const [deviceTime, setDeviceTime] = useState(null)
  const [mode, setMode] = useState('sync')
  const [customDt, setCustomDt] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  // Setting the clock works on both transports; READING it only works over the
  // SDK. The access-control protocol has no command for asking a terminal what
  // time it holds — the traffic runs the other way, with the device fetching
  // the time from the server. So on `acc` the read is not attempted at all,
  // and the reason is shown where the value would have been. Attempting it and
  // swallowing the 501 would leave an em dash that looks like a broken device.
  const isAcc = (device.protocol || 'att') === 'acc'

  useEffect(() => {
    if (isAcc) return
    api.devices.getTime(device.serial_number)
      .then((d) => setDeviceTime(d.time))
      .catch(() => setDeviceTime(null))
  }, [device.serial_number, isAcc])

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setSaving(true)
    try {
      const payload = mode === 'sync'
        ? { sync: true }
        : { sync: false, dt: customDt }
      const result = await api.devices.setTime(device.serial_number, payload)
      // Queued is not updated. On `acc` the server returns what it actually
      // did, and that is what the operator is told.
      showToast(serverMessage(result, t('set_clock.updated')))
      onClose()
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <Drawer title={t('set_clock.title')} onClose={onClose}>
      <div className="mb-4 text-sm">
        <p className="text-gray-500 mb-1">{t('set_clock.current_time')}</p>
        {isAcc ? (
          <p className="text-gray-500 leading-snug">
            {t('set_clock.acc_unreadable')}
          </p>
        ) : (
          <p className="font-mono text-gray-900">
            {deviceTime ? formatDateTime(deviceTime) : '—'}
          </p>
        )}
      </div>

      {isAcc && (
        <p className="text-xs text-gray-500 leading-snug mb-4">
          {device.timezone
            ? t('set_clock.acc_note_tz', { tz: device.timezone })
            : t('set_clock.acc_note')}
        </p>
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        <div className="space-y-2">
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input
              type="radio"
              name="mode"
              value="sync"
              checked={mode === 'sync'}
              onChange={() => setMode('sync')}
            />
            {t('set_clock.sync_now')}
          </label>
          <label className="flex items-center gap-2 text-sm cursor-pointer">
            <input
              type="radio"
              name="mode"
              value="custom"
              checked={mode === 'custom'}
              onChange={() => setMode('custom')}
            />
            {t('set_clock.custom')}
          </label>
        </div>

        {mode === 'custom' && (
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">{t('set_clock.date_time')}</label>
            <input
              type="datetime-local"
              required
              value={customDt}
              onChange={(e) => setCustomDt(e.target.value)}
              className="input w-full"
            />
          </div>
        )}

        {error && (
          <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={saving}
          className="w-full bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium py-2 rounded-lg transition-colors"
        >
          {saving ? t('set_clock.setting') : t('set_clock.submit')}
        </button>
      </form>
    </Drawer>
  )
}
