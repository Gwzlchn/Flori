import createClient from "openapi-fetch";

import type { paths } from "./openapi";

// Same origin; the browser supplies Basic Auth credentials.
export const api = createClient<paths>({ baseUrl: "" });
