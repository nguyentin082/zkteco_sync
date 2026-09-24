import { useState, useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import Icon from './Icon'

function MenuItem({ label, icon, onClick, danger, disabled, hint, primary }) {
  // `hint` is rendered, not hovered: an action that is unavailable has to say
  // why on the face of it, or an operator cannot tell "does not apply to this
  // device" from "broken". Disabled elements swallow mouse events in some
  // browsers, so a title tooltip alone would be no explanation at all.
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => {
        if (!disabled) onClick()
      }}
      className={`w-full text-left px-3 py-2 text-sm rounded-md transition-colors flex items-start gap-2.5 ${
        disabled
          ? 'text-gray-400 cursor-default'
          : danger
            ? 'text-red-600 hover:bg-red-50'
            : primary
              ? 'bg-blue-50 text-blue-700 font-semibold hover:bg-blue-100'
              : 'text-gray-700 hover:bg-gray-100'
      }`}
    >
      {/* Icons are optional per item; when a menu uses them, an item
          without one still keeps the text column aligned. */}
      {icon !== undefined && (
        <span className={`mt-0.5 ${danger || disabled ? '' : primary ? 'text-blue-600' : 'text-gray-500'}`}>
          <Icon name={icon} />
        </span>
      )}
      <span className="min-w-0">
        {label}
        {hint && (
          <span className={`block text-xs leading-snug mt-0.5 font-normal ${primary ? 'text-blue-600/80' : 'text-gray-400'}`}>
            {hint}
          </span>
        )}
      </span>
    </button>
  )
}

function Divider() {
  return <div className="my-1 border-t border-gray-100" />
}

function Heading({ label }) {
  return (
    <div className="px-3 pt-2 pb-1 text-[11px] font-semibold uppercase tracking-wide text-gray-400">
      {label}
    </div>
  )
}

function renderItem(item, key, close) {
  return (
    <MenuItem
      key={key}
      label={item.label}
      icon={item.icon}
      danger={item.danger}
      disabled={item.disabled}
      hint={item.hint}
      primary={item.primary}
      onClick={() => {
        close()
        item.onClick()
      }}
    />
  )
}

export default function KebabMenu({ items }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)

  useEffect(() => {
    if (!open) return
    function handleClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false)
    }
    document.addEventListener('mousedown', handleClick)
    return () => document.removeEventListener('mousedown', handleClick)
  }, [open])

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="p-1.5 rounded-md text-gray-400 hover:text-gray-700 hover:bg-gray-100 transition-colors"
        aria-label={t('common.more_actions')}
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
          <circle cx="8" cy="3" r="1.5" />
          <circle cx="8" cy="8" r="1.5" />
          <circle cx="8" cy="13" r="1.5" />
        </svg>
      </button>

      {open && (
        <div className="absolute right-0 top-full mt-1 w-60 bg-white rounded-xl shadow-lg border border-gray-200 p-1 z-40">
          {/* Entries: 'divider', { heading }, { group: [items] } — a group is
              indented under a rule, to read as the parts of what is above
              it — or a plain item. */}
          {items.map((item, i) =>
            item === 'divider' ? (
              <Divider key={i} />
            ) : item.heading ? (
              <Heading key={i} label={item.heading} />
            ) : item.group ? (
              <div key={i} className="ml-4 pl-1 border-l border-gray-200">
                {item.group.map((sub) => renderItem(sub, sub.label, () => setOpen(false)))}
              </div>
            ) : (
              renderItem(item, item.label, () => setOpen(false))
            )
          )}
        </div>
      )}
    </div>
  )
}
