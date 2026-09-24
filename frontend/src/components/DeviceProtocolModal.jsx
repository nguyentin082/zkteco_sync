import { useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'

const PROTOCOLS = ['att', 'acc']

/**
 * Correcting a device's protocol is its own deliberate action, not a field on
 * the device form — matching DeviceTimezoneModal's precedent. It gets its own
 * modal and its own endpoint (PATCH /devices/{sn}/protocol), because a manual
 * change here pins the value against the automatic DeviceType/ATTLOG
 * classification in adms.py until the device itself proves otherwise.
 */
export default function DeviceProtocolModal({ device, onSave, onClose }) {
  const { t } = useTranslation()
  const [protocol, setProtocol] = useState(device.protocol || 'att')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  const changed = protocol !== (device.protocol || 'att')

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')
    setSaving(true)
    try {
      await onSave(protocol)
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
          <h2 className="text-lg font-semibold text-gray-900">{t('device_protocol.title')}</h2>
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
            i18nKey="device_protocol.intro"
            values={{ device: device.name || device.serial_number }}
            components={{ b: <span className="font-medium text-gray-700" /> }}
          />
        </p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              {t('device_protocol.protocol')}
              <span className="ml-1.5 text-xs font-normal text-gray-400">
                {device.protocol_pinned
                  ? t('device_protocol.current_pinned', { protocol: device.protocol || 'att' })
                  : t('device_protocol.current_auto', { protocol: device.protocol || 'att' })}
              </span>
            </label>
            <select
              value={protocol}
              onChange={(e) => setProtocol(e.target.value)}
              className="input w-full text-sm"
              data-testid="device-protocol-select"
            >
              {PROTOCOLS.map((p) => (
                <option key={p} value={p}>{t(`device_protocol.options.${p}`)}</option>
              ))}
            </select>
          </div>

          <div className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
            {t('device_protocol.pin_notice')}
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
              disabled={saving || !changed}
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
