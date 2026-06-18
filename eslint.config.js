// ESLint Flat-Config (ESLint 9+) für den SOL Analyzer.
// Lintet das in die HTML-Dashboards eingebettete Browser-JS sowie .js-Dateien.
// Bewusst rauscharm: nur Regeln, die echte Bugs finden (keine Stil-Schikane).
//
// Fängt u. a. die Fehlerklassen, die hier real aufgetreten sind:
//   • no-irregular-whitespace  → unsichtbare Zero-Width-Spaces im Markup
//   • no-dupe-keys             → doppelte Objekt-Schlüssel
//   • no-unreachable / no-dupe-args / no-cond-assign → klassische Tippfehler

const globals = require("globals");
const html    = require("eslint-plugin-html");

const bugRules = {
  "no-irregular-whitespace": "error",
  "no-dupe-keys":      "error",
  "no-dupe-args":      "error",
  "no-unreachable":    "warn",
  "no-cond-assign":    ["error", "always"],
  "no-constant-condition": ["warn", { checkLoops: false }],
  "no-unused-vars":    ["warn", { args: "none", varsIgnorePattern: "^_" }],
  "valid-typeof":      "error",
  "use-isnan":         "error",
};

module.exports = [
  {
    ignores: [".venv/**", "node_modules/**", "**/*.min.js", "charts/**"],
  },
  // Reine .js-Dateien (falls vorhanden)
  {
    files: ["**/*.js"],
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: { ...globals.browser, ...globals.node },
    },
    rules: bugRules,
  },
  // In .html eingebettetes Browser-JS (via eslint-plugin-html extrahiert).
  // no-undef AUS: Funktionen/Variablen sind über mehrere <script>-Blöcke
  // global verteilt — sonst nur Falsch-Positive.
  {
    files: ["**/*.html"],
    plugins: { html },
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: { ...globals.browser },
    },
    rules: {
      ...bugRules,
      "no-undef": "off",
    },
  },
];
