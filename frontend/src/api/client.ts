import createClient from "openapi-fetch";

import type { paths } from "../../.generated/api";

export const apiClient = createClient<paths>({
  headers: {
    "X-Flori-Protocol": "1",
  },
});
