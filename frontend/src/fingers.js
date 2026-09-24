import i18n from './i18n'

// The device's own finger numbering, 0–9, running left to right across both
// hands laid backs-up on a table. Shared by the hands diagram and the
// employee page so a template's `finger_id` is named the same way everywhere.
const FINGER_KEYS = [
  'left_little', 'left_ring', 'left_middle', 'left_index', 'left_thumb',
  'right_thumb', 'right_index', 'right_middle', 'right_ring', 'right_little',
]

export function fingerName(fingerId) {
  const key = FINGER_KEYS[fingerId]
  return key ? i18n.t(`fingers.${key}`) : String(fingerId)
}
