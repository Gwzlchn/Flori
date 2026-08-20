import eslint from "@eslint/js";
import vue from "eslint-plugin-vue";
import tseslint from "typescript-eslint";
import vueParser from "vue-eslint-parser";

export default tseslint.config(
  {
    ignores: [".generated/", "dist/", "node_modules/"],
  },
  eslint.configs.recommended,
  ...vue.configs["flat/recommended"],
  {
    files: ["**/*.ts"],
    extends: [...tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      parser: tseslint.parser,
      parserOptions: {
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
  {
    files: ["**/*.vue"],
    extends: [...tseslint.configs.recommendedTypeChecked],
    languageOptions: {
      parser: vueParser,
      parserOptions: {
        extraFileExtensions: [".vue"],
        parser: tseslint.parser,
        projectService: true,
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
    },
  },
  {
    files: ["src/main.ts"],
    rules: {
      // vue-tsc validates the SFC import that plain ESLint sees as an error type.
      "@typescript-eslint/no-unsafe-argument": "off",
    },
  },
  {
    files: ["src/**/*.{ts,vue}"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "TSInterfaceDeclaration[id.name=/(Api|Dto|DTO|Request|Response)$/], TSTypeAliasDeclaration[id.name=/(Api|Dto|DTO|Request|Response)$/]",
          message: "API DTOs must come from frontend/.generated.",
        },
      ],
    },
  },
  {
    files: ["src/api/**/*.ts"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "TSInterfaceDeclaration, TSTypeAliasDeclaration, TSTypeLiteral",
          message: "API types must come from frontend/.generated.",
        },
      ],
    },
  },
);
