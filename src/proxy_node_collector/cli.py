from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from .formats import (
    dedupe_nodes,
    parse_source_content,
    uri_for_node,
)
from .mihomo import test_nodes_with_mihomo
from .models import Node, Source, SourceResult, TestedNode


DEFAULT_SETTINGS: dict[str, Any] = {
    "source_timeout_seconds": 40,
    "source_concurrency": 3,
    "source_retry_count": 1,
    "test_timeout_seconds": 8,
    "controller_start_timeout_seconds": 12,
    "mihomo_batch_size": 30,
    "max_candidates_per_source": 3000,
    "max_tested_nodes": 500,
    "probe_concurrency": 20,
    "page_link_limit": 4,
    "page_payload_limit": 8,
    "user_agent": "proxy-node-collector/0.2",
    "test_url": "https://www.gstatic.com/generate_204",
}

OUTPUT_FORMATS = ("ssr", "shadowrocket", "clash", "v2ray")
OUTPUT_FILES = {
    "subscription": "subscription.txt",
    "ssr": "ssr.txt",
    "shadowrocket": "shadowrocket.txt",
    "clash": "clash.yaml",
    "v2ray": "v2ray.txt",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect SSR/Shadowrocket/Clash/V2Ray nodes, test them, and publish subscriptions."
    )
    parser.add_argument("--config", default="config/sources.yaml", help="Path to source YAML configuration.")
    parser.add_argument("--out-dir", default="data", help="Directory for generated subscription files.")
    parser.add_argument(
        "--mihomo-bin",
        default=os.environ.get("MIHOMO_BIN", ""),
        help="Path to the Mihomo binary used for node testing. Falls back to MIHOMO_BIN.",
    )
    parser.add_argument("--skip-test", action="store_true", help="Only parse and write subscriptions.")
    parser.add_argument("--limit", type=int, default=None, help="Override settings.max_tested_nodes.")
    return parser.parse_args(argv)


def load_config(path: Path) -> tuple[dict[str, Any], list[Source]]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required. Run: python -m pip install -e .") from exc

    with path.open("r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}

    settings = DEFAULT_SETTINGS | (raw.get("settings") or {})
    sources = [
        Source(
            name=str(item["name"]),
            url=str(item["url"]),
            format=str(item.get("format", "auto")).lower(),
            enabled=bool(item.get("enabled", True)),
        )
        for item in raw.get("sources", [])
    ]
    if not sources:
        raise ValueError(f"No sources configured in {path}")
    if not settings.get("test_url"):
        raise ValueError("settings.test_url must be set")
    return settings, sources


async def fetch_sources(
    sources: list[Source],
    settings: dict[str, Any],
) -> tuple[list[Node], list[SourceResult]]:
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx is required. Run: python -m pip install -e .") from exc

    headers = {"User-Agent": str(settings["user_agent"])}
    timeout = httpx.Timeout(float(settings["source_timeout_seconds"]))
    enabled_sources = [source for source in sources if source.enabled]
    semaphore = asyncio.Semaphore(int(settings["source_concurrency"]))

    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True, trust_env=False) as client:
        async def fetch_limited(source: Source) -> tuple[SourceResult, list[Node]]:
            async with semaphore:
                return await fetch_source(client, source, settings)

        results = await asyncio.gather(*(fetch_limited(source) for source in enabled_sources))

    nodes: list[Node] = []
    source_results: list[SourceResult] = []
    for result, parsed_nodes in results:
        source_results.append(result)
        nodes.extend(parsed_nodes)
    return dedupe_nodes(nodes), source_results


async def fetch_source(
    client: Any,
    source: Source,
    settings: dict[str, Any],
) -> tuple[SourceResult, list[Node]]:
    if source.format == "page":
        return await fetch_web_page_source(client, source, settings)

    response, error = await fetch_url(client, source.url, settings)
    if response is None:
        return SourceResult(source=source, ok=False, error=error), []

    try:
        parsed = parse_source_content(
            response.text,
            source.name,
            source.format,
            int(settings["max_candidates_per_source"]),
        )
    except Exception as exc:
        return SourceResult(source=source, ok=False, error=str(exc)), []

    return SourceResult(source=source, ok=True, parsed=len(parsed)), parsed


async def fetch_url(
    client: Any,
    url: str,
    settings: dict[str, Any],
) -> tuple[Any | None, str | None]:
    attempts = int(settings["source_retry_count"]) + 1
    response: Any | None = None
    error: str | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(url)
            response.raise_for_status()
            break
        except Exception as exc:
            detail = str(exc).strip()
            error = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            response = None
            if attempt + 1 < attempts:
                await asyncio.sleep(1.5 * (attempt + 1))
    if response is None:
        return None, error

    return response, None


async def fetch_web_page_source(
    client: Any,
    source: Source,
    settings: dict[str, Any],
) -> tuple[SourceResult, list[Node]]:
    root_response, error = await fetch_url(client, source.url, settings)
    if root_response is None:
        return SourceResult(source=source, ok=False, error=error), []

    page_limit = max(1, int(settings["page_link_limit"]))
    payload_limit = max(1, int(settings["page_payload_limit"]))
    page_urls = [source.url]
    page_contents = {source.url: root_response.text}
    seen_pages = {canonical_http_url(source.url)}
    payload_urls: list[str] = []
    seen_payloads: set[str] = set()
    errors: list[str] = []

    page_index = 0
    while page_index < len(page_urls) and len(page_urls) <= page_limit:
        page_url = page_urls[page_index]
        page_index += 1
        page_content = page_contents[page_url]
        for link in extract_page_links(page_content, page_url):
            if not same_origin(source.url, link):
                continue
            canonical = canonical_http_url(link)
            if is_subscription_link(link):
                if canonical not in seen_payloads and len(payload_urls) < payload_limit:
                    seen_payloads.add(canonical)
                    payload_urls.append(link)
                continue
            if (
                len(page_urls) < page_limit
                and canonical not in seen_pages
                and is_article_link(link)
            ):
                page_response, page_error = await fetch_url(client, link, settings)
                seen_pages.add(canonical)
                if page_response is None:
                    if page_error:
                        errors.append(f"{link}: {page_error}")
                    continue
                page_urls.append(link)
                page_contents[link] = page_response.text

    if not payload_urls:
        detail = "; ".join(errors) or "No subscription links found on page"
        return SourceResult(source=source, ok=False, error=detail), []

    nodes: list[Node] = []
    successful_payloads = 0
    for payload_url in payload_urls:
        payload_response, payload_error = await fetch_url(client, payload_url, settings)
        if payload_response is None:
            if payload_error:
                errors.append(f"{payload_url}: {payload_error}")
            continue
        successful_payloads += 1
        remaining = int(settings["max_candidates_per_source"]) - len(nodes)
        if remaining <= 0:
            break
        try:
            nodes.extend(parse_source_content(payload_response.text, source.name, "auto", remaining))
        except Exception as exc:
            errors.append(f"{payload_url}: {exc}")

    return (
        SourceResult(
            source=source,
            ok=successful_payloads > 0,
            parsed=len(nodes),
            error="; ".join(errors) or None,
        ),
        nodes,
    )


class PageLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self.text_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)
                break

    def handle_data(self, data: str) -> None:
        self.text_parts.append(data)


def extract_page_links(content: str, base_url: str) -> list[str]:
    parser = PageLinkParser()
    parser.feed(content)
    raw_links = list(parser.hrefs)
    text = " ".join(parser.text_parts)
    raw_links.extend(re.findall(r"https?://[^\s<>'\"]+", text))

    links: list[str] = []
    seen: set[str] = set()
    for raw_link in raw_links:
        cleaned = raw_link.strip().rstrip(".,;:!?)]}")
        if not cleaned:
            continue
        absolute = urljoin(base_url, cleaned)
        if urlsplit(absolute).scheme not in {"http", "https"}:
            continue
        canonical = canonical_http_url(absolute)
        if canonical in seen:
            continue
        seen.add(canonical)
        links.append(absolute)
    return links


def canonical_http_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def same_origin(left: str, right: str) -> bool:
    left_parts = urlsplit(left)
    right_parts = urlsplit(right)
    return (
        left_parts.scheme.lower() == right_parts.scheme.lower()
        and left_parts.hostname
        and left_parts.hostname.lower() == (right_parts.hostname or "").lower()
    )


def is_subscription_link(url: str) -> bool:
    parsed = urlsplit(url)
    path = parsed.path.lower()
    query = parsed.query.lower()
    return (
        path.endswith((".yaml", ".yml", ".txt"))
        or "/sub/" in path
        or "/subscribe" in path
        or "/uploads/" in path
        or "format=clash" in query
        or "format=base64" in query
    )


def is_article_link(url: str) -> bool:
    path = urlsplit(url).path.rstrip("/").lower()
    return path in {"", "/"} or path.startswith(("/post/", "/article/", "/archives/"))


def build_subscriptions(nodes: list[Node]) -> dict[str, str]:
    return {
        OUTPUT_FILES["subscription"]: render_base64_subscription(nodes),
        OUTPUT_FILES["ssr"]: render_ssr(nodes),
        OUTPUT_FILES["shadowrocket"]: render_shadowrocket(nodes),
        OUTPUT_FILES["clash"]: render_clash(nodes),
        OUTPUT_FILES["v2ray"]: render_v2ray(nodes),
    }


def render_base64_subscription(nodes: list[Node]) -> str:
    raw = render_uri_lines(nodes)
    if not raw:
        return ""
    return base64.b64encode(raw.encode("utf-8")).decode("ascii") + "\n"


def render_clash(nodes: list[Node]) -> str:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required. Run: python -m pip install -e .") from exc

    proxies = []
    used_names: set[str] = set()
    for node in nodes:
        proxy = json.loads(json.dumps(node.proxy, ensure_ascii=False))
        base_name = node.label
        name = base_name
        suffix = 2
        while name in used_names:
            name = f"{base_name} {suffix}"
            suffix += 1
        used_names.add(name)
        proxy["name"] = name
        proxies.append(proxy)
    document = {
        "mixed-port": 7890,
        "mode": "rule",
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "AUTO",
                "type": "select",
                "proxies": [proxy["name"] for proxy in proxies] or ["DIRECT"],
            }
        ],
        "rules": ["MATCH,AUTO"],
    }
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def render_shadowrocket(nodes: list[Node]) -> str:
    return render_base64_subscription(nodes)


def render_uri_lines(nodes: list[Node]) -> str:
    lines = []
    for node in nodes:
        uri = uri_for_node(node)
        if uri:
            lines.append(uri)
    return "\n".join(lines) + ("\n" if lines else "")


def render_v2ray(nodes: list[Node]) -> str:
    return render_base64_subscription(
        [node for node in nodes if node.protocol in {"vmess", "vless", "trojan"}]
    )


def render_ssr(nodes: list[Node]) -> str:
    return render_base64_subscription([node for node in nodes if node.protocol == "ssr"])


def write_outputs(
    out_dir: Path,
    tested_nodes: list[TestedNode],
    all_nodes: list[Node],
    source_results: list[SourceResult],
    settings: dict[str, Any],
    skip_test: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    remove_obsolete_outputs(out_dir)
    generated_at = utc_now()
    selected_nodes = [item.node for item in tested_nodes] if not skip_test else all_nodes
    subs = build_subscriptions(selected_nodes)
    for name, content in subs.items():
        (out_dir / name).write_text(content, encoding="utf-8")

    metadata = {
        "generated_at": generated_at,
        "tested": not skip_test,
        "candidate_count": len(all_nodes),
        "tested_count": len(tested_nodes),
        "formats": OUTPUT_FORMATS,
        "sources": [
            {
                "name": result.source.name,
                "url": result.source.url,
                "format": result.source.format,
                "ok": result.ok,
                "parsed": result.parsed,
                "error": result.error,
            }
            for result in source_results
        ],
        "nodes": [
            {
                "identity": node.identity,
                "protocol": node.protocol,
                "label": node.label,
                "sources": sorted(node.sources),
                "uri": uri_for_node(node),
            }
            for node in selected_nodes
        ],
        "tested_nodes": [
            {
                "identity": item.node.identity,
                "protocol": item.node.protocol,
                "label": item.node.label,
                "sources": sorted(item.node.sources),
                "latency_ms": item.latency_ms,
                "checked_at": item.checked_at,
                "uri": uri_for_node(item.node),
            }
            for item in tested_nodes
        ],
    }
    (out_dir / "subscription.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (out_dir / "subscription-link.txt").write_text(subscription_links_text(), encoding="utf-8")


def remove_obsolete_outputs(out_dir: Path) -> None:
    for name in ("proxies.txt", "proxies.json", "summary.md"):
        path = out_dir / name
        if path.exists():
            path.unlink()


def subscription_links_text() -> str:
    base_url = os.environ.get("SUBSCRIPTION_BASE_URL", "").rstrip("/")
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if not base_url and repo:
        branch = os.environ.get("GITHUB_REF_NAME", "main").strip() or "main"
        base_url = f"https://raw.githubusercontent.com/{repo}/{branch}/data"
    if not base_url:
        base_url = "."

    lines = [
        f"main={base_url}/{OUTPUT_FILES['subscription']}",
        f"ssr={base_url}/{OUTPUT_FILES['ssr']}",
        f"shadowrocket={base_url}/{OUTPUT_FILES['shadowrocket']}",
        f"clash={base_url}/{OUTPUT_FILES['clash']}",
        f"v2ray={base_url}/{OUTPUT_FILES['v2ray']}",
    ]
    return "\n".join(lines) + "\n"


async def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings, sources = load_config(Path(args.config))
    max_nodes = int(args.limit if args.limit is not None else settings["max_tested_nodes"])
    settings["max_tested_nodes"] = max_nodes
    print(f"Loading sources from {args.config}", file=sys.stderr)
    nodes, source_results = await fetch_sources(sources, settings)
    print(f"Parsed {len(nodes)} unique nodes", file=sys.stderr)

    if args.skip_test:
        tested_nodes: list[TestedNode] = []
    else:
        if not args.mihomo_bin:
            raise RuntimeError("mihomo binary path is required. Set --mihomo-bin or MIHOMO_BIN.")
        mihomo_bin = Path(args.mihomo_bin).expanduser()
        tested_nodes = await test_nodes_with_mihomo(nodes, mihomo_bin, settings)
        print(f"Tested {len(tested_nodes)} working nodes", file=sys.stderr)

    write_outputs(Path(args.out_dir), tested_nodes, nodes, source_results, settings, args.skip_test)
    print(f"Wrote outputs to {args.out_dir}", file=sys.stderr)
    return 0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run(argv))
    except KeyboardInterrupt:
        return 130
