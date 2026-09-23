import logo from '../assets/brand/wata-logo.png'

// The name the product is known by inside WATA, in one place: the header, the
// sign-in card and the browser tab all have to say the same thing, and the tab
// is set in index.html where nothing can import from here.
export const APP_NAME = 'WTS ZKTeco Sync'

/**
 * The WATA Software wordmark beside — or above — the product name.
 *
 * The horizontal lockup is used rather than the square one: at the height a
 * header row allows, the square version's "WATA SOFTWARE" line ends up smaller
 * than the text next to it and stops being readable. The square logo is kept
 * in assets/brand and is what the favicon is cut from.
 *
 * `stacked` puts the logo above the name, for the sign-in card: side by side
 * the pair is about 310px wide, which is the whole width that card has, and it
 * would sit on the edge of overflowing at the first longer name.
 *
 * The image is decorative — `alt=""` and aria-hidden. The company name is in
 * the picture and the product name is in the text beside it, so announcing
 * both would read the brand twice.
 */
export default function Brand({
  stacked = false,
  className = '',
  logoClassName = 'h-6',
  nameClassName = '',
}) {
  return (
    <span
      className={`inline-flex items-center ${stacked ? 'flex-col gap-3' : 'gap-2.5'} ${className}`}
    >
      <img src={logo} alt="" aria-hidden="true" className={`${logoClassName} w-auto`} />
      {!stacked && <span className="h-5 w-px bg-gray-200" aria-hidden="true" />}
      <span className={nameClassName}>{APP_NAME}</span>
    </span>
  )
}
