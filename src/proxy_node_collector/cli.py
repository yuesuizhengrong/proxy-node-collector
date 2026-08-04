from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

SUPPORTED_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
DEFAULT_SETTINGS: dict[str, Any] = {
    "source_timeout_seconds": 20,
    "test_timeout_seconds": 8,
    "concurrency": 120,
    "max_candidates_per_source": 5000,
    "max_alive": 1000,
    "user_agent": "proxy-node-collector/0.1",
    "test_urls": ["https://www.gstatic.com/generate_204"],
}

CANDIDATE_RE = re.compile(
    r"(?:(?P<scheme>https?|socks4a?|socks5h?)://)?"
    r"(?P<host>\[[0-9a-fA-F:.]+\]|(?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9][A-Za-z0-9.-]{0,253}[A-Za-z0-9])"
    r":(?P<port>\d{2,5})",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Source:
    name: str
    url: str
    scheme: str = "auto"
    enabled: bool = True


@dataclass(slots=True)
class ProxyCandidate:
    scheme: str
    host: str
    port: int
    sources: set[str] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.scheme, self.host, self.port)

    @property
    def url(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"{self.scheme}://{host}:{self.port}"


@dataclass(slots=True)
class SourceResult:
    source: Source
    ok: bool
    parsed: int = 0
    error: str | None = None


@dataclass(slots=True)
class AliveProxy:
    candidate: ProxyCandidate
    latency_ms: int
    status_code: int
    test_url: str
    checked_at: str


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect public proxy nodes and publish only tested working proxies."
    )
    parser.add_argument(
        "--config",
        default="config/sources.yaml",
        help="Path to source YAML configuration.",
    )
    parser.add_argument(
        "--out-dir",
        default="data",
        help="Directory for proxies.txt, proxies.json, and summary.md.",
    )
    parser.add_argument(
        "--skip-test",
        action="store_true",
        help="Only fetch and parse candidates; do not test availability.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Override settings.max_alive.",
    )
    return parser.parse_args(argv)


def load_config(path: Path) -> tuple[dict[str, Any], list[Source]]:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load config files. Run: python -m pip install -e .") from exc

    with path.open("r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}

    settings = DEFAULT_SETTINGS | (raw.get("settings") or {})
    sources = [
        Source(
            name=str(item["name"]),
            url=str(item["url"]),
            scheme=str(item.get("scheme", "auto")).lower(),
            enabled=bool(item.get("enabled", True)),
        )
        for item in raw.get("sources", [])
    ]

    if not sources:
        raise ValueError(f"No sources configured in {path}")

    bad_schemes = [
        source.scheme
        for source in sources
        if source.scheme != "auto" and source.scheme not in SUPPORTED_SCHEMES
    ]
    if bad_schemes:
        raise ValueError(f"Unsupported source schemes: {', '.join(sorted(set(bad_schemes)))}")

    if not settings["test_urls"]:
        raise ValueError("settings.test_urls must contain at least one URL")

    return settings, sources


def parse_candidates(text: str, source: Source, max_candidates: int) -> list[ProxyCandidate]:
    candidates: list[ProxyCandidate] = []
    seen: set[tuple[str, str, int]] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "@" in line:
            continue

        for match in CANDIDATE_RE.finditer(line):
            scheme = (match.group("scheme") or source.scheme or "http").lower()
            if scheme == "auto":
                scheme = "http"
            if scheme not in SUPPORTED_SCHEMES:
                continue

            host = clean_host(match.group("host"))
            if host is None:
                continue

            port = int(match.group("port"))
            if not 1 <= port <= 65535:
                continue

            candidate = ProxyCandidate(scheme=scheme, host=host, port=port, sources={source.name})
            if candidate.key in seen:
                continue
            seen.add(candidate.key)
            candidates.append(candidate)

            if len(candidates) >= max_candidates:
                return candidates

    return candidates


def clean_host(raw_host: str) -> str | None:
    host = raw_host.strip().strip("[]").lower()
    if not host or len(host) > 255:
        return None

    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    if not re.fullmatch(r"[a-z0-9.-]+", host):
        return None
    if "." not in host:
        return None
    if ".." in host or host.startswith(".") or host.endswith("."):
        return None

    return host


async def fetch_source(
    client: Any,
    source: Source,
    settings: dict[str, Any],
) -> tuple[SourceResult, list[ProxyCandidate]]:
    try:
        response = await client.get(source.url)
        response.raise_for_status()
    except Exception as exc:
        return SourceResult(source=source, ok=False, error=str(exc)), []

    candidates = parse_candidates(
        response.text,
        source,
        max_candidates=int(settings["max_candidates_per_source"]),
    )
    return SourceResult(source=source, ok=True, parsed=len(candidates)), candidates


async def collect_candidates(
    sources: list[Source],
    settings: dict[str, Any],
) -> tuple[list[ProxyCandidate], list[SourceResult]]:
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx is required to fetch source URLs. Run: python -m pip install -e .") from exc

    headers = {"User-Agent": str(settings["user_agent"])}
    timeout = httpx.Timeout(float(settings["source_timeout_seconds"]))
    enabled_sources = [source for source in sources if source.enabled]

    async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
        results = await asyncio.gather(
            *(fetch_source(client, source, settings) for source in enabled_sources)
        )

    merged: dict[tuple[str, str, int], ProxyCandidate] = {}
    source_results: list[SourceResult] = []

    for source_result, candidates in results:
        source_results.append(source_result)
        for candidate in candidates:
            existing = merged.get(candidate.key)
            if existing is None:
                merged[candidate.key] = candidate
            else:
                existing.sources.update(candidate.sources)

    return list(merged.values()), source_results


async def test_proxy(
    candidate: ProxyCandidate,
    test_urls: list[str],
    settings: dict[str, Any],
    semaphore: asyncio.Semaphore,
) -> AliveProxy | None:
    try:
        import httpx
        from httpx_socks import AsyncProxyTransport
    except ImportError as exc:
        raise RuntimeError("httpx and httpx-socks are required to test proxies. Run: python -m pip install -e .") from exc

    timeout = httpx.Timeout(float(settings["test_timeout_seconds"]))
    headers = {"User-Agent": str(settings["user_agent"])}

    async with semaphore:
        start = perf_counter()
        try:
            client_options: dict[str, Any] = {
                "timeout": timeout,
                "follow_redirects": True,
                "trust_env": False,
                "headers": headers,
            }
            if candidate.scheme.startswith("socks"):
                client_options["transport"] = AsyncProxyTransport.from_url(candidate.url)
            else:
                client_options["proxy"] = candidate.url

            async with httpx.AsyncClient(**client_options) as client:
                for test_url in test_urls:
                    response = await client.get(test_url)
                    if response.status_code < 400:
                        return AliveProxy(
                            candidate=candidate,
                            latency_ms=max(1, round((perf_counter() - start) * 1000)),
                            status_code=response.status_code,
                            test_url=test_url,
                            checked_at=utc_now(),
                        )
        except Exception:
            return None

    return None


async def test_candidates(
    candidates: list[ProxyCandidate],
    settings: dict[str, Any],
    max_alive: int,
) -> list[AliveProxy]:
    semaphore = asyncio.Semaphore(int(settings["concurrency"]))
    test_urls = [str(url) for url in settings["test_urls"]]
    alive: list[AliveProxy] = []

    tasks = [
        asyncio.create_task(test_proxy(candidate, test_urls, settings, semaphore))
        for candidate in candidates
    ]

    try:
        for task in asyncio.as_completed(tasks):
            result = await task
            if result is not None:
                alive.append(result)
                if len(alive) >= max_alive:
                    for pending in tasks:
                        pending.cancel()
                    break
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)

    alive.sort(key=lambda item: item.latency_ms)
    return alive


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_outputs(
    out_dir: Path,
    alive: list[AliveProxy],
    candidates: list[ProxyCandidate],
    source_results: list[SourceResult],
    settings: dict[str, Any],
    skip_test: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = utc_now()

    if skip_test:
        proxy_urls = sorted(candidate.url for candidate in candidates)
    else:
        proxy_urls = [item.candidate.url for item in alive]

    (out_dir / "proxies.txt").write_text("\n".join(proxy_urls) + ("\n" if proxy_urls else ""), encoding="utf-8")

    payload = {
        "generated_at": generated_at,
        "tested": not skip_test,
        "candidate_count": len(candidates),
        "alive_count": len(proxy_urls),
        "test_urls": settings["test_urls"],
        "sources": [
            {
                "name": result.source.name,
                "url": result.source.url,
                "ok": result.ok,
                "parsed": result.parsed,
                "error": result.error,
            }
            for result in source_results
        ],
        "proxies": [
            {
                "url": item.candidate.url,
                "scheme": item.candidate.scheme,
                "host": item.candidate.host,
                "port": item.candidate.port,
                "sources": sorted(item.candidate.sources),
                "latency_ms": item.latency_ms,
                "status_code": item.status_code,
                "test_url": item.test_url,
                "checked_at": item.checked_at,
            }
            for item in alive
        ]
        if not skip_test
        else [
            {
                "url": candidate.url,
                "scheme": candidate.scheme,
                "host": candidate.host,
                "port": candidate.port,
                "sources": sorted(candidate.sources),
            }
            for candidate in sorted(candidates, key=lambda item: item.url)
        ],
    }
    (out_dir / "proxies.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    summary = build_summary(generated_at, alive, candidates, source_results, skip_test)
    (out_dir / "summary.md").write_text(summary, encoding="utf-8")


def build_summary(
    generated_at: str,
    alive: list[AliveProxy],
    candidates: list[ProxyCandidate],
    source_results: list[SourceResult],
    skip_test: bool,
) -> str:
    lines = [
        "# Proxy Node Summary",
        "",
        f"- Generated at: `{generated_at}`",
        f"- Candidate count: `{len(candidates)}`",
        f"- Tested: `{'no' if skip_test else 'yes'}`",
        f"- Published proxy count: `{len(candidates) if skip_test else len(alive)}`",
        "",
        "## Sources",
        "",
        "| Source | Status | Parsed |",
        "| --- | --- | ---: |",
    ]

    for result in source_results:
        status = "ok" if result.ok else f"failed: {result.error}"
        lines.append(f"| {result.source.name} | {status} | {result.parsed} |")

    if not skip_test:
        lines.extend(
            [
                "",
                "## Fastest Proxies",
                "",
                "| Proxy | Latency | Status |",
                "| --- | ---: | ---: |",
            ]
        )
        for item in alive[:20]:
            lines.append(f"| `{item.candidate.url}` | {item.latency_ms} ms | {item.status_code} |")

    return "\n".join(lines) + "\n"


async def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config_path = Path(args.config)
    out_dir = Path(args.out_dir)
    settings, sources = load_config(config_path)
    max_alive = int(args.limit if args.limit is not None else settings["max_alive"])

    print(f"Loading sources from {config_path}", file=sys.stderr)
    candidates, source_results = await collect_candidates(sources, settings)
    print(f"Collected {len(candidates)} unique candidates", file=sys.stderr)

    if args.skip_test:
        alive: list[AliveProxy] = []
    else:
        alive = await test_candidates(candidates, settings, max_alive=max_alive)
        print(f"Found {len(alive)} working proxies", file=sys.stderr)

    write_outputs(out_dir, alive, candidates, source_results, settings, args.skip_test)
    print(f"Wrote outputs to {out_dir}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return asyncio.run(run(argv))
    except KeyboardInterrupt:
        return 130
