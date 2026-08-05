from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
    attempts = int(settings["source_retry_count"]) + 1
    response: Any | None = None
    error: str | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(source.url)
            response.raise_for_status()
            break
        except Exception as exc:
            detail = str(exc).strip()
            error = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
            response = None
            if attempt + 1 < attempts:
                await asyncio.sleep(1.5 * (attempt + 1))
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
