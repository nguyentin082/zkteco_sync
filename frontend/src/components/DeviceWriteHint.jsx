import { useState, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import Icon from './Icon'

const TIP_WIDTH = 240

// Marks an action that writes, edits or deletes data on the terminal itself,
// as opposed to changing only this server's records. Sits beside the button,
// not inside it: most of these buttons stay disabled until a device is
// picked, and a disabled button swallows the hover in some browsers.
//
// The tooltip is portalled and fixed-positioned so a scrolling drawer or an
// overflow-clipped table cannot cut it off.
//
// `inline` is for use inside another control (a menu item): no tab stop of
// its own there, since the control already takes focus.
export default function DeviceWriteHint({ className = '', inline = false }) {
  const { t } = useTranslation()
  const ref = useRef(null)
  const [pos, setPos] = useState(null)
  const text = t('common.device_write_hint')

  function show() {
    const r = ref.current.getBoundingClientRect()
    const half = TIP_WIDTH / 2
    const x = Math.min(Math.max(r.left + r.width / 2, half + 8), window.innerWidth - half - 8)
    // Above the icon unless that would leave the viewport, then below.
    setPos(r.top > 80 ? { x, y: r.top - 6, above: true } : { x, y: r.bottom + 6, above: false })
  }

  return (
    <span
      ref={ref}
      role="img"
      aria-label={text}
      tabIndex={inline ? undefined : 0}
      onMouseEnter={show}
      onMouseLeave={() => setPos(null)}
      onFocus={show}
      onBlur={() => setPos(null)}
      className={`inline-flex shrink-0 cursor-help text-amber-500 hover:text-amber-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-amber-300 rounded-full ${className}`}
    >
      <Icon name="info" className="w-4 h-4" />
      {pos &&
        createPortal(
          <div
            role="tooltip"
            style={{
              position: 'fixed',
              left: pos.x,
              top: pos.y,
              width: TIP_WIDTH,
              transform: pos.above ? 'translate(-50%, -100%)' : 'translate(-50%, 0)',
            }}
            className="z-[100] pointer-events-none rounded-md bg-gray-900 px-2.5 py-1.5 text-xs font-normal leading-snug text-white shadow-lg normal-case tracking-normal text-left"
          >
            {text}
          </div>,
          document.body
        )}
    </span>
  )
}
