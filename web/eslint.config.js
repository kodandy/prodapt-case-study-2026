import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "node_modules", "test-results", "playwright-report"] },
  js.configs.recommended,
  ...tseslint.configs.strict,
  {
    files: ["src/**/*.{ts,tsx}", "e2e/**/*.ts"],
    plugins: { "react-hooks": reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-explicit-any": "error",
      "no-restricted-syntax": [
        "error",
        { selector: "ExportDefaultDeclaration", message: "Named exports only." },
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: "Render text only (docs/specs/web.md).",
        },
      ],
      "no-restricted-properties": [
        "error",
        { object: "window", property: "localStorage", message: "Credentials stay in memory." },
        { object: "window", property: "sessionStorage", message: "Credentials stay in memory." },
        { object: "document", property: "cookie", message: "Credentials stay in memory." },
      ],
      "no-restricted-globals": [
        "error",
        { name: "localStorage", message: "Credentials stay in memory." },
        { name: "sessionStorage", message: "Credentials stay in memory." },
        { name: "indexedDB", message: "Credentials stay in memory." },
      ],
    },
  },
  {
    files: ["src/**/__tests__/**"],
    rules: { "no-restricted-properties": "off", "no-restricted-globals": "off" },
  },
);
