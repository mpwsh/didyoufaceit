tailwind.config = {
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        brand: { DEFAULT: "#ff5500", dim: "#cc4400" },
        surface: { DEFAULT: "#141414", alt: "#1a1a1a" },
        border: { DEFAULT: "#2a2a2a", strong: "#3a3a3a" },
        win: "#22c55e",
        lose: "#ef4444",
        muted: "#666666",
        blue: "#60a5fa",
        gold: "#f59e0b",
      },
      fontFamily: {
        display: ['"Bebas Neue"', "sans-serif"],
        mono: ['"DM Mono"', "monospace"],
        sans: ['"DM Sans"', "sans-serif"],
      },
    },
  },
};
