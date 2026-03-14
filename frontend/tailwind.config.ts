import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./components/**/*.{js,ts,jsx,tsx,mdx}",
    "./app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        cinema: {
          black:        "#06060F",   // deep space background
          dark:         "#0C0C1A",   // slightly raised surface
          card:         "#0F0F1E",   // card background
          border:       "#1C1C32",   // default border
          // primary accent — electric violet (replaces gold)
          gold:         "#9333EA",
          "gold-light": "#C084FC",
          // text
          text:         "#F0EEFF",   // near-white with slight violet tint
          muted:        "#7070A0",   // muted — violet-tinted grey
          // secondary accent — cyan
          accent:       "#22D3EE",
          // tertiary accent — hot pink (for gradients)
          pink:         "#EC4899",
        },
      },
      fontFamily: {
        // Modern system-font stack — SF Pro on macOS, Segoe UI on Windows, etc.
        sans:    ["-apple-system", "BlinkMacSystemFont", '"Segoe UI"', "system-ui", "sans-serif"],
        // Display stack uses tighter tracking + heavier weight defined in CSS
        display: ['"Segoe UI"', "-apple-system", "BlinkMacSystemFont", "system-ui", "sans-serif"],
      },
      animation: {
        "pulse-slow": "pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        "fade-in":    "fadeIn 0.5s ease-in",
        "slide-up":   "slideUp 0.4s ease-out",
        "gradient-x": "gradientX 4s ease infinite",
      },
      keyframes: {
        fadeIn: {
          "0%":   { opacity: "0" },
          "100%": { opacity: "1" },
        },
        slideUp: {
          "0%":   { transform: "translateY(20px)", opacity: "0" },
          "100%": { transform: "translateY(0)",    opacity: "1" },
        },
        gradientX: {
          "0%, 100%": { backgroundPosition: "0% 50%"   },
          "50%":       { backgroundPosition: "100% 50%" },
        },
      },
    },
  },
  plugins: [],
};

export default config;
