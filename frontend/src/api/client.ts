import createClient from "openapi-fetch";

import type { components, paths } from "../../.generated/api";

export type { components };

export const apiClient = createClient<paths>({
  headers: {
    "X-Flori-Protocol": "1",
  },
});

export function apiError(
  response: components["schemas"]["ErrorResponse"] | undefined,
  fallback: string,
): string {
  return response ? `${response.error.code}: ${response.error.message}` : fallback;
}
