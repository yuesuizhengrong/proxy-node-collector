from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from .models import Node


SUPPORTED_PROTOCOLS = {"ss", "ssr", "vmess", "vless", "trojan"}
SHADOWROCKET_PROTOCOLS = {"ss", "ssr", "vmess", "vless", "trojan"}
V2RAY_PROTOCOLS = {"vmess", "vless", "trojan"}


def parse_source_content(
    content: str,
    source_name: str,
    source_format: str,
    max_nodes: int,
) -> list[Node]:
    format_name = source_format.lower()
    if format_name not in {"auto", "uri", "base64", "clash"}:
        raise ValueError(f"Unsupported source format: {source_format}")

    if format_name in {"auto", "clash"} and looks_like_clash(content):
        nodes = parse_clash_document(content, source_name)
        if nodes or format_name == "clash":
            return nodes[:max_nodes]

    if format_name == "clash":
        return []

    decoded = content
    if format_name == "base64" or (format_name == "auto" and looks_like_base64_subscription(content)):
        decoded = decode_subscription(content)

    return parse_uri_lines(decoded, source_name, max_nodes)


def parse_uri_lines(content: str, source_name: str, max_nodes: int) -> list[Node]:
    nodes: list[Node] = []
    for line in content.splitlines():
        value = line.strip()
        if not value or value.startswith("#") or "://" not in value:
            continue

        node = parse_uri(value, source_name)
        if node is not None:
            nodes.append(node)
        if len(nodes) >= max_nodes:
            break

    return nodes


def parse_uri(uri: str, source_name: str) -> Node | None:
    scheme = uri.split("://", 1)[0].lower()
    if scheme == "ssr":
        return parse_ssr_uri(uri, source_name)
    if scheme == "ss":
        return parse_ss_uri(uri, source_name)
    if scheme == "vmess":
        return parse_vmess_uri(uri, source_name)
    if scheme in {"vless", "trojan"}:
        return parse_xray_uri(uri, source_name, scheme)
    return None


def parse_ssr_uri(uri: str, source_name: str) -> Node | None:
    try:
        decoded = decode_urlsafe(uri.removeprefix("ssr://")).decode("utf-8")
        main, separator, raw_query = decoded.partition("/?")
        fields = main.rstrip("/").split(":", 5)
        if len(fields) != 6:
            return None

        server, port_text, protocol, cipher, obfs, encoded_password = fields
        port = valid_port(port_text)
        password = decode_urlsafe(encoded_password).decode("utf-8")
        if not server or port is None or not password:
            return None

        query = parse_qs(raw_query if separator else "", keep_blank_values=True)
        label = decode_ssr_parameter(query.get("remarks", [""])[0]) or f"SSR {server}:{port}"
        proxy: dict[str, Any] = {
            "type": "ssr",
            "server": server,
            "port": port,
            "cipher": cipher,
            "password": password,
            "protocol": protocol,
            "obfs": obfs,
            "udp": True,
        }
        protocol_param = decode_ssr_parameter(query.get("protoparam", [""])[0])
        obfs_param = decode_ssr_parameter(query.get("obfsparam", [""])[0])
        if protocol_param:
            proxy["protocol-param"] = protocol_param
        if obfs_param:
            proxy["obfs-param"] = obfs_param

        return build_node(proxy, "ssr", label, source_name)
    except (UnicodeDecodeError, ValueError):
        return None


def parse_ss_uri(uri: str, source_name: str) -> Node | None:
    try:
        without_scheme = uri.removeprefix("ss://")
        before_fragment, _, fragment = without_scheme.partition("#")
        authority, _, raw_query = before_fragment.partition("?")
        if "@" in authority:
            encoded_auth, address = authority.rsplit("@", 1)
            credentials = decode_ss_credentials(unquote(encoded_auth))
        else:
            decoded = decode_urlsafe(unquote(authority)).decode("utf-8")
            credentials, address = decoded.rsplit("@", 1)

        cipher, password = credentials.split(":", 1)
        parsed = urlsplit(f"ss://{address}")
        server = parsed.hostname
        port = parsed.port
        if not server or port is None or not password:
            return None

        label = unquote(fragment) or f"SS {server}:{port}"
        proxy: dict[str, Any] = {
            "type": "ss",
            "server": server,
            "port": port,
            "cipher": cipher,
            "password": password,
            "udp": True,
        }
        plugin = parse_qs(raw_query).get("plugin", [""])[0]
        if plugin:
            plugin_name, *plugin_options = unquote(plugin).split(";")
            proxy["plugin"] = plugin_name
            if plugin_options:
                options: dict[str, Any] = {}
                for option in plugin_options:
                    key, separator, value = option.partition("=")
                    options[key] = value if separator else True
                proxy["plugin-opts"] = options

        return build_node(proxy, "ss", label, source_name)
    except (UnicodeDecodeError, ValueError):
        return None


def parse_vmess_uri(uri: str, source_name: str) -> Node | None:
    try:
        payload = json.loads(decode_urlsafe(uri.removeprefix("vmess://")).decode("utf-8"))
        server = str(payload.get("add", "")).strip()
        port = valid_port(payload.get("port"))
        uuid = str(payload.get("id", "")).strip()
        if not server or port is None or not uuid:
            return None

        label = str(payload.get("ps", "")).strip() or f"VMess {server}:{port}"
        network = str(payload.get("net", "tcp")).lower()
        proxy: dict[str, Any] = {
            "type": "vmess",
            "server": server,
            "port": port,
            "uuid": uuid,
            "alterId": int_or_default(payload.get("aid"), 0),
            "cipher": str(payload.get("scy") or payload.get("cipher") or "auto"),
            "udp": True,
            "network": network,
        }
        apply_transport_options(proxy, network, payload)

        if str(payload.get("tls", "")).lower() in {"tls", "reality"}:
            proxy["tls"] = True
        server_name = str(payload.get("sni", "")).strip()
        if server_name:
            proxy["servername"] = server_name
        alpn = split_csv(payload.get("alpn"))
        if alpn:
            proxy["alpn"] = alpn
        fingerprint = str(payload.get("fp", "")).strip()
        if fingerprint:
            proxy["client-fingerprint"] = fingerprint

        return build_node(proxy, "vmess", label, source_name)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None


def parse_xray_uri(uri: str, source_name: str, protocol: str) -> Node | None:
    try:
        parsed = urlsplit(uri)
        server = parsed.hostname
        port = parsed.port
        credential = unquote(parsed.username or "")
        if not server or port is None or not credential:
            return None

        query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        label = unquote(parsed.fragment) or f"{protocol.upper()} {server}:{port}"
        network = query.get("type", "tcp").lower()
        proxy: dict[str, Any] = {
            "type": protocol,
            "server": server,
            "port": port,
            "udp": True,
            "network": network,
        }
        proxy["uuid" if protocol == "vless" else "password"] = credential
        if protocol == "vless" and query.get("flow"):
            proxy["flow"] = query["flow"]

        security = query.get("security", "none").lower()
        if security in {"tls", "reality"}:
            proxy["tls"] = True
        if query.get("sni"):
            proxy["servername"] = query["sni"]
        if query.get("alpn"):
            proxy["alpn"] = split_csv(query["alpn"])
        if query.get("fp"):
            proxy["client-fingerprint"] = query["fp"]
        if query.get("allowInsecure", query.get("insecure", "")).lower() in {"1", "true"}:
            proxy["skip-cert-verify"] = True
        if security == "reality":
            reality_opts = {
                "public-key": query.get("pbk", ""),
                "short-id": query.get("sid", ""),
            }
            proxy["reality-opts"] = {key: value for key, value in reality_opts.items() if value}

        apply_transport_options(proxy, network, query)
        return build_node(proxy, protocol, label, source_name)
    except ValueError:
        return None


def parse_clash_document(content: str, source_name: str) -> list[Node]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to parse Clash sources. Run: python -m pip install -e .") from exc

    try:
        document = yaml.safe_load(content) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(document, dict) or not isinstance(document.get("proxies"), list):
        return []

    nodes: list[Node] = []
    for entry in document["proxies"]:
        if not isinstance(entry, dict):
            continue
        proxy = copy.deepcopy(entry)
        protocol = str(proxy.get("type", "")).lower()
        if protocol not in SUPPORTED_PROTOCOLS:
            continue
        label = str(proxy.pop("name", "")).strip() or f"{protocol.upper()} node"
        node = build_node(proxy, protocol, label, source_name)
        if node is not None:
            nodes.append(node)
    return nodes


def build_node(
    proxy: dict[str, Any],
    protocol: str,
    label: str,
    source_name: str,
) -> Node | None:
    protocol = protocol.lower()
    if protocol not in SUPPORTED_PROTOCOLS:
        return None

    normalized = copy.deepcopy(proxy)
    normalized.pop("name", None)
    normalized["type"] = protocol
    server = str(normalized.get("server", "")).strip()
    port = valid_port(normalized.get("port"))
    if not server or port is None:
        return None
    normalized["server"] = server
    normalized["port"] = port

    identity_payload = canonicalize(normalized)
    identity = hashlib.sha256(
        json.dumps(identity_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    clean_label = " ".join(str(label).split())[:120] or f"{protocol.upper()} {server}:{port}"
    return Node(
        identity=identity,
        protocol=protocol,
        label=clean_label,
        proxy=normalized,
        sources={source_name},
    )


def dedupe_nodes(nodes: list[Node]) -> list[Node]:
    merged: dict[str, Node] = {}
    for node in nodes:
        existing = merged.get(node.identity)
        if existing is None:
            merged[node.identity] = node
        else:
            existing.sources.update(node.sources)
    return list(merged.values())


def uri_for_node(node: Node) -> str | None:
    return uri_from_proxy(node.proxy, node.protocol, node.label)


def uri_from_proxy(proxy: dict[str, Any], protocol: str, label: str) -> str | None:
    protocol = protocol.lower()
    try:
        if protocol == "ssr":
            return ssr_uri_from_proxy(proxy, label)
        if protocol == "ss":
            return ss_uri_from_proxy(proxy, label)
        if protocol == "vmess":
            return vmess_uri_from_proxy(proxy, label)
        if protocol in {"vless", "trojan"}:
            return xray_uri_from_proxy(proxy, protocol, label)
    except (KeyError, TypeError, ValueError):
        return None
    return None


def ssr_uri_from_proxy(proxy: dict[str, Any], label: str) -> str:
    required = ["server", "port", "protocol", "cipher", "obfs", "password"]
    if any(not proxy.get(key) for key in required):
        raise ValueError("Incomplete SSR proxy")

    core = ":".join(
        [
            str(proxy["server"]),
            str(proxy["port"]),
            str(proxy["protocol"]),
            str(proxy["cipher"]),
            str(proxy["obfs"]),
            encode_urlsafe(str(proxy["password"]).encode("utf-8")),
        ]
    )
    parameters = {"remarks": encode_urlsafe(label.encode("utf-8"))}
    if proxy.get("protocol-param"):
        parameters["protoparam"] = encode_urlsafe(str(proxy["protocol-param"]).encode("utf-8"))
    if proxy.get("obfs-param"):
        parameters["obfsparam"] = encode_urlsafe(str(proxy["obfs-param"]).encode("utf-8"))
    payload = f"{core}/?{urlencode(parameters)}"
    return f"ssr://{encode_urlsafe(payload.encode('utf-8'))}"


def ss_uri_from_proxy(proxy: dict[str, Any], label: str) -> str:
    server = format_host(str(proxy["server"]))
    port = int(proxy["port"])
    cipher = str(proxy["cipher"])
    password = str(proxy["password"])
    encoded_auth = encode_urlsafe(f"{cipher}:{password}".encode("utf-8"))
    query = ""
    if proxy.get("plugin"):
        options = proxy.get("plugin-opts", {})
        parts = [str(proxy["plugin"])]
        if isinstance(options, dict):
            for key, value in options.items():
                parts.append(str(key) if value is True else f"{key}={value}")
        query = f"?{urlencode({'plugin': ';'.join(parts)})}"
    return f"ss://{encoded_auth}@{server}:{port}{query}#{quote(label, safe='')}"


def vmess_uri_from_proxy(proxy: dict[str, Any], label: str) -> str:
    network = str(proxy.get("network", "tcp"))
    payload: dict[str, Any] = {
        "v": "2",
        "ps": label,
        "add": str(proxy["server"]),
        "port": str(proxy["port"]),
        "id": str(proxy["uuid"]),
        "aid": str(proxy.get("alterId", 0)),
        "scy": str(proxy.get("cipher", "auto")),
        "net": network,
        "type": "none",
        "host": "",
        "path": "",
        "tls": "tls" if proxy.get("tls") else "",
    }
    apply_proxy_transport_to_vmess_payload(payload, proxy, network)
    if proxy.get("servername"):
        payload["sni"] = proxy["servername"]
    if proxy.get("alpn"):
        payload["alpn"] = ",".join(proxy["alpn"])
    if proxy.get("client-fingerprint"):
        payload["fp"] = proxy["client-fingerprint"]
    return f"vmess://{encode_urlsafe(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))}"


def xray_uri_from_proxy(proxy: dict[str, Any], protocol: str, label: str) -> str:
    credential_key = "uuid" if protocol == "vless" else "password"
    credential = quote(str(proxy[credential_key]), safe="")
    server = format_host(str(proxy["server"]))
    port = int(proxy["port"])
    network = str(proxy.get("network", "tcp"))
    parameters: dict[str, str] = {
        "security": "reality" if proxy.get("reality-opts") else ("tls" if proxy.get("tls") else "none"),
        "type": network,
    }
    if protocol == "vless":
        parameters["encryption"] = "none"
        if proxy.get("flow"):
            parameters["flow"] = str(proxy["flow"])
    if proxy.get("servername"):
        parameters["sni"] = str(proxy["servername"])
    if proxy.get("alpn"):
        parameters["alpn"] = ",".join(str(item) for item in proxy["alpn"])
    if proxy.get("client-fingerprint"):
        parameters["fp"] = str(proxy["client-fingerprint"])
    if proxy.get("skip-cert-verify"):
        parameters["allowInsecure"] = "1"
    reality_opts = proxy.get("reality-opts", {})
    if isinstance(reality_opts, dict):
        if reality_opts.get("public-key"):
            parameters["pbk"] = str(reality_opts["public-key"])
        if reality_opts.get("short-id"):
            parameters["sid"] = str(reality_opts["short-id"])
    apply_proxy_transport_to_xray_parameters(parameters, proxy, network)
    return (
        f"{protocol}://{credential}@{server}:{port}?{urlencode(parameters)}"
        f"#{quote(label, safe='')}"
    )


def apply_transport_options(proxy: dict[str, Any], network: str, values: dict[str, Any]) -> None:
    network = network.lower()
    if network == "ws":
        headers: dict[str, str] = {}
        host = str(values.get("host", "")).strip()
        if host:
            headers["Host"] = host.split(",")[0]
        options: dict[str, Any] = {"path": str(values.get("path", "/") or "/")}
        if headers:
            options["headers"] = headers
        proxy["ws-opts"] = options
    elif network == "grpc":
        service_name = str(values.get("serviceName") or values.get("service-name") or "").strip()
        if service_name:
            proxy["grpc-opts"] = {"grpc-service-name": service_name}
    elif network in {"h2", "http"}:
        host = split_csv(values.get("host"))
        options: dict[str, Any] = {"path": str(values.get("path", "/") or "/")}
        if host:
            options["host"] = host
        proxy["network"] = "http"
        proxy["http-opts"] = options


def apply_proxy_transport_to_vmess_payload(
    payload: dict[str, Any],
    proxy: dict[str, Any],
    network: str,
) -> None:
    if network == "ws":
        options = proxy.get("ws-opts", {})
        if isinstance(options, dict):
            payload["path"] = str(options.get("path", "/"))
            headers = options.get("headers", {})
            if isinstance(headers, dict) and headers.get("Host"):
                payload["host"] = str(headers["Host"])
    elif network == "grpc":
        options = proxy.get("grpc-opts", {})
        if isinstance(options, dict):
            payload["path"] = str(options.get("grpc-service-name", ""))
    elif network == "http":
        options = proxy.get("http-opts", {})
        if isinstance(options, dict):
            payload["path"] = str(options.get("path", "/"))
            hosts = options.get("host", [])
            if isinstance(hosts, list) and hosts:
                payload["host"] = str(hosts[0])


def apply_proxy_transport_to_xray_parameters(
    parameters: dict[str, str],
    proxy: dict[str, Any],
    network: str,
) -> None:
    if network == "ws":
        options = proxy.get("ws-opts", {})
        if isinstance(options, dict):
            parameters["path"] = str(options.get("path", "/"))
            headers = options.get("headers", {})
            if isinstance(headers, dict) and headers.get("Host"):
                parameters["host"] = str(headers["Host"])
    elif network == "grpc":
        options = proxy.get("grpc-opts", {})
        if isinstance(options, dict) and options.get("grpc-service-name"):
            parameters["serviceName"] = str(options["grpc-service-name"])
    elif network == "http":
        options = proxy.get("http-opts", {})
        if isinstance(options, dict):
            parameters["path"] = str(options.get("path", "/"))
            hosts = options.get("host", [])
            if isinstance(hosts, list) and hosts:
                parameters["host"] = str(hosts[0])


def looks_like_clash(content: str) -> bool:
    sample = content.lstrip()[:2048]
    return sample.startswith("proxies:") or "\nproxies:" in sample


def looks_like_base64_subscription(content: str) -> bool:
    compact = "".join(content.split())
    if len(compact) < 32 or "://" in compact:
        return False
    try:
        decoded = decode_urlsafe(compact).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return False
    return "://" in decoded


def decode_subscription(content: str) -> str:
    return decode_urlsafe("".join(content.split())).decode("utf-8")


def decode_ss_credentials(value: str) -> str:
    if ":" in value:
        return value
    return decode_urlsafe(value).decode("utf-8")


def decode_ssr_parameter(value: str) -> str:
    if not value:
        return ""
    try:
        return decode_urlsafe(value).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return ""


def encode_urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_urlsafe(value: str) -> bytes:
    normalized = value.strip()
    padding = "=" * (-len(normalized) % 4)
    return base64.urlsafe_b64decode((normalized + padding).encode("ascii"))


def valid_port(value: Any) -> int | None:
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def split_csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def format_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): canonicalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, str):
        return value.strip()
    return value

