# build:前端依赖与静态资源构建。
FROM node:22-alpine AS build

RUN npm config set registry https://registry.npmmirror.com

WORKDIR /app
COPY package.json package-lock.json ./
# npm 缓存走 BuildKit cache mount(CI buildx / compose 内置 BuildKit 支持):re-build 不重下依赖。
RUN --mount=type=cache,target=/root/.npm npm ci
COPY . .
RUN npm run build

# production:nginx 托管构建产物。
FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
