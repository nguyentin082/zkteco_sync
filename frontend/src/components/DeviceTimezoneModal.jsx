import { useState, useMemo } from 'react'
import { Trans, useTranslation } from 'react-i18next'

// The IANA zone list straight from the browser's own ICU data, which is the
// same tz database the server validates against. Chrome has had this since 99;
// if it is ever missing the field degrades to free text and the server still
// rejects anything it cannot resolve.
function knownZones() {
  try {
    if (typeof Intl.supportedValuesOf === 'function') {
      return Intl.supportedValuesOf('timeZone')
    }
  } catch {
    // fall through to free text
  }
  return []
}

/**
 * Changing a device's timezone is its own deliberate action, not a field on
 * the device form: it relabels every attendance record that device has ever
 * pushed. So it gets its own modal, its own endpoint, and says plainly what
 * it is about to do before it does it.
 */
export default function DeviceTimezoneModal({ device, onSave, onClose }) {
  const { t } = useTranslation()
  const [timezone, setTimezone] = useState(device.timezone || '')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const zones = useMemo(() => {
    const list = knownZones()
    // Whatever the device currently claims must be selectable even if this
    // browser's list does not carry it, or the form could not round-trip.
    if (device.timezone && list.length && !list.includes(device.timezone)) {
      return [device.timezone, ...list]
    }
    return list
  }, [device.timezone])

  const changed = timezone !== (device.timezone || '')

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setSaving(true)
    try {
      await onSave(timezone)
    } catch (err) {
      setError(err.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop — not clickable */}
      <div className="absolute inset-0 bg-black/40" />

      {/* Modal */}
      <div className="relative bg-white rounded-2xl shadow-xl w-full max-w-md mx-4 p-6">
        <div className="flex items-center justify-between mb-5">
          <h2 className="text-lg font-semibold text-gray-900">{t('device_timezone.title')}</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 transition-colors"
          >
            ✕
          </button>
        </div>

        <p className="text-sm text-gray-500 mb-4">
          <Trans
            i18nKey="device_timezone.intro"
            values={{ device: device.name || device.serial_number }}
            components={{ b: <span className="font-medium text-gray-700" /> }}
          />
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              {t('device_timezone.timezone')}
              <span className="ml-1.5 text-xs font-normal text-gray-400">
                {t('device_timezone.current', { tz: device.timezone || t('device_timezone.not_set') })}
              </span>
            </label>
            {zones.length > 0 ? (
              <select
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                className="input w-full text-sm"
                data-testid="device-timezone-select"
              >
                {!device.timezone && <option value="">{t('device_timezone.select')}</option>}
                {zones.map((z) => (
                  <option key={z} value={z}>{z}</option>
                ))}
              </select>
            ) : (
              <input
                type="text"
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                placeholder="Asia/Dubai"
                className="input w-full text-sm"
                data-testid="device-timezone-select"
              />
            )}
          </div>

          <div className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
            {t('device_timezone.notice')}
          </div>

          {error && (
            <p className="text-sm text-red-600 bg-red-50 border border-red-200 rounded-lg px-3 py-2">
              {error}
            </p>
          )}

          <div className="flex gap-3 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 border border-gray-300 text-gray-700 hover:bg-gray-50 text-sm font-medium py-2 rounded-lg transition-colors"
            >
              {t('common.cancel')}
            </button>
            <button
              type="submit"
              disabled={saving || !timezone || !changed}
              className="flex-1 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white text-sm font-medium py-2 rounded-lg transition-colors"
            >
              {saving ? t('common.saving') : t('common.update')}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}
