import js from "@eslint/js";
import globals from "globals";
import react from "eslint-plugin-react";
import reactHooks from "eslint-plugin-react-hooks";

export default [
  { ignores: ["build/**", "dist/**", "node_modules/**", "plugins/**", "public/**"] },
  {
    files: ["src/**/*.{js,jsx}"],
    plugins: { react, "react-hooks": reactHooks },
    languageOptions: {
      ecmaVersion: 2024,
      sourceType: "module",
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: { ...globals.browser, ...globals.es2021, process: "readonly" },
    },
    settings: { react: { version: "detect" } },
    rules: {
      ...js.configs.recommended.rules,
      ...react.configs.recommended.rules,
      ...reactHooks.configs.recommended.rules,
      "react/react-in-jsx-scope": "off",
      "react/prop-types": "off",
      "react/no-unescaped-entities": "off",
      "react/display-name": "off",
      // the terminal aesthetic renders literal "// LABEL" strings by design
      "react/jsx-no-comment-textnodes": "off",
      "react/no-unknown-property": ["error", { ignore: ["cmdk-input-wrapper"] }],
      "no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^[A-Z_]" }],
      "no-empty": ["warn", { allowEmptyCatch: true }],
      "react-hooks/exhaustive-deps": "off",
    },
  },
];
