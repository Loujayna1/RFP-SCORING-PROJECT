/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        crimson:  { DEFAULT: '#ED1C24', light: '#FF3A42', dark: '#B31319', muted: '#FFE8EB' },
        ivory:    { DEFAULT: '#FAF8F5', warm: '#F5F0EC' },
        ink:      { DEFAULT: '#0F0A0B', soft: '#2A1A1D', muted: '#6B4B51' },
        steel:    { DEFAULT: '#D8CED0', light: '#EDE8E9' }
      },
      fontFamily: {
        display: ['"Bebas Neue"', 'sans-serif'],
        heading: ['"DM Serif Display"', 'serif'],
        body:    ['"DM Sans"', 'sans-serif'],
        mono:    ['"JetBrains Mono"', 'monospace']
      },
      animation: {
        'slide-up':   'slideUp .4s cubic-bezier(.16,1,.3,1) both',
        'fade-in':    'fadeIn .3s ease both',
        'pulse-slow': 'pulse 3s ease-in-out infinite',
        'spin-slow':  'spin 2s linear infinite',
        'score-fill': 'scoreFill .8s cubic-bezier(.16,1,.3,1) both'
      },
      keyframes: {
        slideUp:    { from: { opacity:0, transform:'translateY(16px)' }, to: { opacity:1, transform:'translateY(0)' } },
        fadeIn:     { from: { opacity:0 }, to: { opacity:1 } },
        scoreFill:  { from: { width:'0%' }, to: { width:'var(--target-width)' } }
      }
    }
  },
  plugins: []
}
