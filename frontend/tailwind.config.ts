import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Apple 官网色系
        ink: "#1d1d1f", // 主文字
        muted: "#6e6e73", // 次要文字
        faint: "#86868b", // 更弱文字
        line: "#d2d2d7", // 分隔线
        hairline: "#ebebed", // 极浅分隔
        canvas: "#f5f5f7", // Apple 标志性浅灰底
        apple: {
          DEFAULT: "#0071e3",
          hover: "#0077ed",
          active: "#006edb",
          soft: "#e8f1fd",
          tint: "#f0f6ff",
        },
        success: {
          DEFAULT: "#1f8a4c",
          soft: "#e7f6ee",
          dot: "#34c759",
        },
        warning: {
          DEFAULT: "#b25f00",
          soft: "#fdf3e3",
          dot: "#ff9500",
        },
        danger: {
          DEFAULT: "#d70015",
          soft: "#fde8ea",
          dot: "#ff3b30",
        },
        violet: {
          DEFAULT: "#7c4dff",
          soft: "#f1ecff",
        },
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          "SF Pro Display",
          "SF Pro Text",
          "PingFang SC",
          "Hiragino Sans GB",
          "Microsoft YaHei",
          "Segoe UI",
          "sans-serif",
        ],
        mono: [
          "SF Mono",
          "ui-monospace",
          "Menlo",
          "Monaco",
          "Consolas",
          "monospace",
        ],
      },
      borderRadius: {
        "4xl": "2rem",
      },
      boxShadow: {
        card: "0 2px 16px rgba(0, 0, 0, 0.05), 0 1px 3px rgba(0, 0, 0, 0.04)",
        lift: "0 16px 48px rgba(0, 0, 0, 0.10), 0 2px 8px rgba(0, 0, 0, 0.05)",
        pop: "0 24px 80px rgba(0, 0, 0, 0.18)",
        ring: "0 0 0 4px rgba(0, 113, 227, 0.15)",
      },
      transitionTimingFunction: {
        apple: "cubic-bezier(0.25, 0.1, 0.25, 1)",
        "apple-out": "cubic-bezier(0.16, 1, 0.3, 1)",
      },
      keyframes: {
        "fade-up": {
          "0%": { opacity: "0", transform: "translateY(16px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        "scale-in": {
          "0%": { opacity: "0", transform: "scale(0.97)" },
          "100%": { opacity: "1", transform: "scale(1)" },
        },
        "slide-in-right": {
          "0%": { opacity: "0", transform: "translateX(40px)" },
          "100%": { opacity: "1", transform: "translateX(0)" },
        },
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
      },
      animation: {
        "fade-up": "fade-up 0.7s cubic-bezier(0.16, 1, 0.3, 1) both",
        "fade-in": "fade-in 0.5s ease both",
        "scale-in": "scale-in 0.4s cubic-bezier(0.16, 1, 0.3, 1) both",
        "slide-in-right":
          "slide-in-right 0.45s cubic-bezier(0.16, 1, 0.3, 1) both",
      },
    },
  },
  plugins: [],
};

export default config;
