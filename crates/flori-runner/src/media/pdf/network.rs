use std::net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr};
use std::time::Duration;

use flori_core::ErrorCode;
use reqwest::{Client, Url, redirect};

pub(super) const MAX_REDIRECTS: usize = 5;

pub(super) fn parse_http_url(value: &str) -> Result<Url, ErrorCode> {
    let authority = value
        .strip_prefix("http://")
        .or_else(|| value.strip_prefix("https://"))
        .ok_or(ErrorCode::UnsupportedSource)?;
    if authority.starts_with('/') || value.trim() != value {
        return Err(ErrorCode::UnsupportedSource);
    }
    let url = Url::parse(value).map_err(|_| ErrorCode::UnsupportedSource)?;
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || !url.username().is_empty()
        || url.password().is_some()
        || url.fragment().is_some()
    {
        return Err(ErrorCode::UnsupportedSource);
    }
    Ok(url)
}

pub(super) async fn pinned_client(url: &Url, timeout: Duration) -> Result<Client, ErrorCode> {
    let host = url.host_str().ok_or(ErrorCode::UnsupportedSource)?;
    let port = url
        .port_or_known_default()
        .ok_or(ErrorCode::UnsupportedSource)?;
    let addresses = resolve(host, port).await?;
    let mut builder = Client::builder()
        .redirect(redirect::Policy::none())
        .no_proxy()
        .connect_timeout(timeout)
        .timeout(timeout);
    if host.parse::<IpAddr>().is_err() {
        builder = builder.resolve_to_addrs(host, &addresses);
    }
    builder.build().map_err(|_| ErrorCode::Internal)
}

async fn resolve(host: &str, port: u16) -> Result<Vec<SocketAddr>, ErrorCode> {
    let addresses = if let Ok(ip) = host.parse::<IpAddr>() {
        vec![SocketAddr::new(ip, port)]
    } else {
        tokio::net::lookup_host((host, port))
            .await
            .map_err(|_| ErrorCode::NetworkTemporary)?
            .collect::<Vec<_>>()
    };
    if addresses.is_empty() || addresses.iter().any(|address| !is_public(address.ip())) {
        return Err(ErrorCode::UnsupportedSource);
    }
    Ok(addresses)
}

fn is_public(address: IpAddr) -> bool {
    match address {
        IpAddr::V4(address) => is_public_v4(address),
        IpAddr::V6(address) => is_public_v6(address),
    }
}

fn is_public_v4(address: Ipv4Addr) -> bool {
    let [a, b, c, _] = address.octets();
    !matches!(
        (a, b, c),
        (0, _, _)
            | (10, _, _)
            | (100, 64..=127, _)
            | (127, _, _)
            | (169, 254, _)
            | (172, 16..=31, _)
            | (192, 0, 0 | 2)
            | (192, 88, 99)
            | (192, 168, _)
            | (198, 18 | 19 | 51, _)
            | (203, 0, 113)
            | (224..=255, _, _)
    )
}

fn is_public_v6(address: Ipv6Addr) -> bool {
    if let Some(v4) = address.to_ipv4_mapped() {
        return is_public_v4(v4);
    }
    let segments = address.segments();
    let first = segments[0];
    (0x2000..=0x3fff).contains(&first)
        && !(first == 0x2001 && segments[1] < 0x0200)
        && !(first == 0x2001 && segments[1] == 0x0db8)
        && !(first == 0x3fff && segments[1] <= 0x0fff)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn url_syntax_is_fail_closed() {
        for value in [
            "file:///tmp/paper.pdf",
            "https://user@example.com/paper.pdf",
            "https://example.com/paper.pdf#fragment",
            "http:///paper.pdf",
        ] {
            assert_eq!(parse_http_url(value), Err(ErrorCode::UnsupportedSource));
        }
        assert!(parse_http_url("https://example.com/paper.pdf?download=1").is_ok());
    }

    #[test]
    fn rejects_non_public_address_classes() {
        for value in [
            IpAddr::V4(Ipv4Addr::new(0, 0, 0, 0)),
            IpAddr::V4(Ipv4Addr::new(10, 1, 2, 3)),
            IpAddr::V4(Ipv4Addr::new(100, 64, 0, 1)),
            IpAddr::V4(Ipv4Addr::new(127, 0, 0, 1)),
            IpAddr::V4(Ipv4Addr::new(169, 254, 1, 1)),
            IpAddr::V4(Ipv4Addr::new(172, 31, 1, 1)),
            IpAddr::V4(Ipv4Addr::new(192, 0, 2, 1)),
            IpAddr::V4(Ipv4Addr::new(192, 168, 1, 1)),
            IpAddr::V4(Ipv4Addr::new(198, 18, 0, 1)),
            IpAddr::V4(Ipv4Addr::new(198, 51, 100, 1)),
            IpAddr::V4(Ipv4Addr::new(203, 0, 113, 1)),
            IpAddr::V4(Ipv4Addr::new(224, 0, 0, 1)),
            IpAddr::V4(Ipv4Addr::new(240, 0, 0, 1)),
            "::".parse().expect("unspecified IPv6"),
            "::1".parse().expect("loopback IPv6"),
            "::ffff:127.0.0.1".parse().expect("mapped IPv6"),
            "fc00::1".parse().expect("private IPv6"),
            "fe80::1".parse().expect("link-local IPv6"),
            "2001:db8::1".parse().expect("documentation IPv6"),
            "3fff::1".parse().expect("documentation IPv6"),
        ] {
            assert!(!is_public(value), "{value}");
        }
        assert!(is_public("8.8.8.8".parse().expect("public IPv4")));
        assert!(is_public(
            "2606:4700:4700::1111".parse().expect("public IPv6")
        ));
    }
}
