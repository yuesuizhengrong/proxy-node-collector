from __future__ import annotations

import asyncio
import copy
import os
import socket
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import Node, TestedNode


async def test_nodes_with_mihomo(
    nodes: list[Node],
    mihomo_bin: Path,
    settings: dict[str, Any],
) -> list[TestedNode]:
    try:
        import httpx
        import yaml
    except ImportError as exc:
        raise RuntimeError("httpx and PyYAML are required to test nodes. Run: python -m pip install -e .") from exc

    if not mihomo_bin.is_file():
        raise FileNotFoundError(f"Mihomo binary not found: {mihomo_bin}")

    ordered = round_robin_protocol_order(nodes)
    max_tested = int(settings["max_tested_nodes"])
    batch_size = int(settings["mihomo_batch_size"])
    selected = ordered[:max_tested]
    results: list[TestedNode] = []

    for offset in range(0, len(selected), batch_size):
        batch = selected[offset : offset + batch_size]
        results.extend(
            await test_batch_resilient(batch, mihomo_bin, settings, httpx, yaml)
        )

    results.sort(key=lambda item: item.latency_ms)
    return results


async def test_batch_resilient(
    nodes: list[Node],
    mihomo_bin: Path,
    settings: dict[str, Any],
    httpx: Any,
    yaml: Any,
) -> list[TestedNode]:
    result = await test_single_batch(nodes, mihomo_bin, settings, httpx, yaml)
    if result is not None:
        return result
    if len(nodes) <= 1:
        return []

    middle = len(nodes) // 2
    left = await test_batch_resilient(nodes[:middle], mihomo_bin, settings, httpx, yaml)
    right = await test_batch_resilient(nodes[middle:], mihomo_bin, settings, httpx, yaml)
    return left + right


async def test_single_batch(
    nodes: list[Node],
    mihomo_bin: Path,
    settings: dict[str, Any],
    httpx: Any,
    yaml: Any,
) -> list[TestedNode] | None:
    if not nodes:
        return []

    with tempfile.TemporaryDirectory(prefix="mihomo-probe-") as temp_dir:
        work_dir = Path(temp_dir)
        controller_port = reserve_port()
        mixed_port = reserve_port()
        named_nodes: list[tuple[str, Node]] = []
        proxies: list[dict[str, Any]] = []

        for index, node in enumerate(nodes):
            name = f"probe-{index:03d}"
            proxy = copy.deepcopy(node.proxy)
            proxy["name"] = name
            named_nodes.append((name, node))
            proxies.append(proxy)

        config = {
            "mixed-port": mixed_port,
            "external-controller": f"127.0.0.1:{controller_port}",
            "log-level": "silent",
            "mode": "rule",
            "proxies": proxies,
            "proxy-groups": [
                {
                    "name": "Probe",
                    "type": "select",
                    "proxies": [name for name, _ in named_nodes],
                }
            ],
            "rules": ["MATCH,Probe"],
        }
        config_path = work_dir / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

        process = subprocess.Popen(
            [str(mihomo_bin), "-d", str(work_dir), "-f", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creation_flags(),
        )
        controller_url = f"http://127.0.0.1:{controller_port}"

        try:
            ready = await wait_for_controller(
                controller_url,
                float(settings["controller_start_timeout_seconds"]),
                httpx,
            )
            if not ready:
                return None
            return await probe_nodes(
                controller_url,
                named_nodes,
                settings,
                httpx,
            )
        finally:
            stop_process(process)


async def wait_for_controller(
    controller_url: str,
    timeout_seconds: float,
    httpx: Any,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        while asyncio.get_running_loop().time() < deadline:
            try:
                response = await client.get(f"{controller_url}/version")
                if response.status_code == 200:
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
    return False


async def probe_nodes(
    controller_url: str,
    named_nodes: list[tuple[str, Node]],
    settings: dict[str, Any],
    httpx: Any,
) -> list[TestedNode]:
    semaphore = asyncio.Semaphore(int(settings["probe_concurrency"]))
    timeout_ms = int(float(settings["test_timeout_seconds"]) * 1000)
    test_url = str(settings["test_url"])

    async with httpx.AsyncClient(
        timeout=float(settings["test_timeout_seconds"]) + 2,
        trust_env=False,
    ) as client:
        tasks = [
            asyncio.create_task(
                probe_node(
                    client,
                    controller_url,
                    name,
                    node,
                    test_url,
                    timeout_ms,
                    semaphore,
                )
            )
            for name, node in named_nodes
        ]
        results = await asyncio.gather(*tasks)
    return [result for result in results if result is not None]


async def probe_node(
    client: Any,
    controller_url: str,
    name: str,
    node: Node,
    test_url: str,
    timeout_ms: int,
    semaphore: asyncio.Semaphore,
) -> TestedNode | None:
    async with semaphore:
        try:
            response = await client.get(
                f"{controller_url}/proxies/{quote(name, safe='')}/delay",
                params={"url": test_url, "timeout": timeout_ms},
            )
            response.raise_for_status()
            delay = int(response.json().get("delay", 0))
            if delay <= 0:
                return None
            return TestedNode(
                node=node,
                latency_ms=delay,
                checked_at=utc_now(),
            )
        except Exception:
            return None


def round_robin_protocol_order(nodes: list[Node]) -> list[Node]:
    by_protocol: dict[str, list[Node]] = {}
    for node in sorted(nodes, key=lambda item: (item.protocol, item.identity)):
        by_protocol.setdefault(node.protocol, []).append(node)

    ordered: list[Node] = []
    protocols = sorted(by_protocol)
    while protocols:
        next_protocols: list[str] = []
        for protocol in protocols:
            queue = by_protocol[protocol]
            if queue:
                ordered.append(queue.pop(0))
            if queue:
                next_protocols.append(protocol)
        protocols = next_protocols
    return ordered


def reserve_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

