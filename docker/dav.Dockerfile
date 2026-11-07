# flori-dav: Caddy + WebDAV + AliDNS. Built from local base images because
# Docker Hub is unreachable through the NAS proxy; modules come from goproxy.cn.
FROM golang:1.26.5-alpine AS build
ENV GOPROXY=https://goproxy.cn,direct CGO_ENABLED=0 GOFLAGS=-trimpath
RUN go install github.com/caddyserver/xcaddy/cmd/xcaddy@v0.4.7
RUN xcaddy build v2.11.7 --output /caddy \
      --with github.com/mholt/caddy-webdav@v0.0.0-20260127042217-fa2f366b0d75 \
      --with github.com/caddy-dns/alidns@v1.0.29

FROM caddy:2-alpine
COPY --from=build /caddy /usr/bin/caddy
