# syntax=docker/dockerfile:1.7

FROM node:24-alpine AS dependencies
WORKDIR /app

COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci

FROM dependencies AS check
COPY tsconfig.json eslint.config.js vite.config.ts index.html ./
COPY src ./src
COPY .generated ./.generated
CMD ["npm", "run", "check"]

FROM dependencies AS build
COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src
COPY .generated ./.generated
RUN npm run build

FROM caddy:2.10-alpine AS edge
COPY --from=build /app/dist /usr/share/caddy
