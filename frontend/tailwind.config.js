/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      // Inter is bundled (@fontsource-variable/inter, Vietnamese subset
      // included) so the app looks the same on a LAN with no internet.
      // Sampled from assets/brand/wata-logo.png, so the wordmark matches the logo.
      colors: {
        brand: { red: '#ED202E' },
      },
      fontFamily: {
        sans: ['"Inter Variable"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        // The product wordmark only: a geometric face so the name reads as a
        // name, not as one more line of UI text.
        display: ['"Plus Jakarta Sans Variable"', '"Inter Variable"', 'ui-sans-serif', 'sans-serif'],
      },
    },
  },
  plugins: [],
}

