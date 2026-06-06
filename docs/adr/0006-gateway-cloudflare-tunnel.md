# ADR-0006: Cloudflare Tunnel 做公网入口

## 背景

主机在内网（如运营商 NAT），无公网 IP。需要方案让手机/外网访问。

## 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| Cloudflare Tunnel | 免费、自带 WAF/DDoS/Access、主机零端口 | 依赖 Cloudflare、延迟加 20-50ms |
| frp (自建中转) | 自建可控、延迟低 | 中转服务器存密钥、frp 有安全风险、多一跳 |
| ZeroTier / Tailscale | P2P 直连 | 手机需装 App、不适合浏览器访问 |
| 内网穿透 (ngrok/花生壳) | 简单 | 免费版不稳定/限流 |

## 决定

Cloudflare Tunnel。

## 理由

1. **安全**：主机零公网端口，纯出站连接。Cloudflare 自带 DDoS/WAF
2. **认证**：Cloudflare Access 免费支持邮箱验证，不需要自建认证
3. **独立于中转**：用户访问不经过中转服务器。中转被攻破不影响用户访问
4. **免费**：Tunnel + Access 基础版免费
5. **延迟可接受**：API 请求加 20-50ms，对个人工具无感。视频是主机本地下载，不走 Tunnel 上传

frp 方案的问题：中转服务器同时存了 frp token 和 API token，攻破中转等于拿到全部钥匙。Cloudflare Tunnel 避免了这个风险。

## 影响

- 主机 docker-compose 加 `cloudflared` 容器
- 需要一个域名托管在 Cloudflare
- 中转服务器（如有）只做远程 Worker 中转，不参与用户访问
