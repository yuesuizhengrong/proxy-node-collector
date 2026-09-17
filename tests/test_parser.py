import base64
import gzip
import importlib.util
import unittest
from pathlib import Path

from proxy_node_collector.cli import (
    DEFAULT_SETTINGS,
    OUTPUT_FILES,
    build_subscriptions,
    ensure_publishable_results,
    extract_page_links,
    fetch_web_page_source,
    fetch_url,
    validate_settings,
)
from proxy_node_collector.cli import load_config
from proxy_node_collector.formats import (
    parse_source_content,
    parse_uri,
    ssr_uri_from_proxy,
    uri_for_node,
    vmess_uri_from_proxy,
)


SSR_PROXY = {
    "type": "ssr",
    "server": "ssr.example.com",
    "port": 443,
    "cipher": "aes-256-cfb",
    "password": "test-password",
    "protocol": "auth_aes128_sha1",
    "obfs": "plain",
    "udp": True,
}

VMESS_PROXY = {
    "type": "vmess",
    "server": "vmess.example.com",
    "port": 443,
    "uuid": "11111111-1111-1111-1111-111111111111",
    "alterId": 0,
    "cipher": "auto",
    "udp": True,
    "network": "ws",
    "tls": True,
    "servername": "cdn.example.com",
    "ws-opts": {"path": "/socket", "headers": {"Host": "cdn.example.com"}},
}


class SubscriptionFormatTest(unittest.TestCase):
    def test_rejects_invalid_runtime_settings(self):
        settings = DEFAULT_SETTINGS.copy()
        settings["source_concurrency"] = 0
        with self.assertRaisesRegex(ValueError, "source_concurrency"):
            validate_settings(settings)

        settings["source_concurrency"] = 1
        settings["source_retry_count"] = 0
        validate_settings(settings)

    def test_extracts_same_page_subscription_links(self):
        html = """
        <a href="/post/20260824/">latest</a>
        <pre>https://free.datiya.com/uploads/20260824-clash.yaml</pre>
        <a href="https://other.example.invalid/node.txt">external</a>
        """

        links = extract_page_links(html, "https://free.datiya.com/")

        self.assertIn("https://free.datiya.com/post/20260824/", links)
        self.assertIn("https://free.datiya.com/uploads/20260824-clash.yaml", links)

    def test_refuses_to_publish_empty_test_results(self):
        with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite subscriptions"):
            ensure_publishable_results([], [], skip_test=False)

        ensure_publishable_results([], [], skip_test=True)

    def test_config_includes_non_github_sources(self):
        _, sources = load_config(Path(__file__).parents[1] / "config" / "sources.yaml")
        external_hosts = {
            source.url.split("/", 3)[2].lower()
            for source in sources
            if "github.com" not in source.url.lower()
        }

        self.assertIn("clashnodefree.com", external_hosts)
        self.assertIn("www.xrayvip.com", external_hosts)
        self.assertTrue(any(source.format == "page" for source in sources))

    def test_ssr_uri_round_trip(self):
        uri = ssr_uri_from_proxy(SSR_PROXY, "SSR test")

        node = parse_uri(uri, "unit")

        self.assertIsNotNone(node)
        self.assertEqual(node.protocol, "ssr")
        self.assertEqual(node.proxy["server"], "ssr.example.com")
        self.assertEqual(node.proxy["password"], "test-password")

    def test_vmess_uri_round_trip(self):
        uri = vmess_uri_from_proxy(VMESS_PROXY, "VMess test")

        node = parse_uri(uri, "unit")

        self.assertIsNotNone(node)
        self.assertEqual(node.protocol, "vmess")
        self.assertEqual(node.proxy["uuid"], VMESS_PROXY["uuid"])
        self.assertEqual(node.proxy["ws-opts"]["path"], "/socket")

    def test_parses_base64_subscription(self):
        uri = (
            "vless://22222222-2222-2222-2222-222222222222@vless.example.com:443"
            "?security=tls&type=ws&path=%2Fws&sni=cdn.example.com#VLESS%20test"
        )
        content = base64.b64encode(f"{uri}\n".encode("utf-8")).decode("ascii")

        nodes = parse_source_content(content, "unit", "base64", 10)

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].protocol, "vless")
        self.assertEqual(uri_for_node(nodes[0]).split("://", 1)[0], "vless")

    def test_skips_malformed_uri_without_aborting_source(self):
        valid = vmess_uri_from_proxy(VMESS_PROXY, "valid")
        nodes = parse_source_content(
            "vmess://not-valid\n" + valid,
            "unit",
            "uri",
            10,
        )

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].label, "valid")

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_clash_limit_applies_before_node_construction_finishes(self):
        content = "proxies:\n" + "".join(
            f"  - name: node-{index}\n    type: vmess\n    server: node-{index}.example.com\n    port: 443\n    uuid: 11111111-1111-1111-1111-111111111111\n"
            for index in range(4)
        )

        nodes = parse_source_content(content, "unit", "clash", 2)

        self.assertEqual([node.label for node in nodes], ["node-0", "node-1"])

class AsyncCollectorTest(unittest.IsolatedAsyncioTestCase):
    async def test_streamed_gzip_response_is_not_decoded_twice(self):
        import httpx

        payload = "vmess://example\n".encode()

        def handler(request):
            return httpx.Response(
                200,
                content=gzip.compress(payload),
                headers={"Content-Encoding": "gzip"},
                request=request,
            )

        settings = {"source_retry_count": 0, "max_source_bytes": 1024}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response, error = await fetch_url(client, "https://site.example/sub.txt", settings)

        self.assertIsNone(error)
        self.assertIsNotNone(response)
        self.assertEqual(response.text, payload.decode())

    async def test_page_payloads_are_fetched_concurrently_and_deduplicated(self):
        import httpx

        responses = {
            "https://site.example/": '<a href="/sub/a.txt">a</a><a href="/sub/a.txt">duplicate</a><a href="/sub/b.txt">b</a>',
            "https://site.example/sub/a.txt": base64.b64encode(
                (vmess_uri_from_proxy(VMESS_PROXY, "a") + "\n").encode()
            ).decode(),
            "https://site.example/sub/b.txt": base64.b64encode(
                (vmess_uri_from_proxy(VMESS_PROXY, "b") + "\n").encode()
            ).decode(),
        }
        calls = []

        async def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, text=responses[str(request.url)], request=request)

        settings = {
            "source_retry_count": 0,
            "max_source_bytes": 1024 * 1024,
            "page_link_limit": 1,
            "page_payload_limit": 8,
            "max_candidates_per_source": 10,
        }
        source = load_config(Path(__file__).parents[1] / "config" / "sources.yaml")[1][-1]
        source = source.__class__("fixture", "https://site.example/", "page", True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result, nodes = await fetch_web_page_source(client, source, settings)

        self.assertTrue(result.ok)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(calls.count("https://site.example/sub/a.txt"), 1)

    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML is not installed")
    def test_builds_client_subscription_outputs(self):
        ssr = parse_uri(ssr_uri_from_proxy(SSR_PROXY, "duplicate"), "unit")
        vmess = parse_uri(vmess_uri_from_proxy(VMESS_PROXY, "duplicate"), "unit")
        self.assertIsNotNone(ssr)
        self.assertIsNotNone(vmess)

        outputs = build_subscriptions([ssr, vmess])

        self.assertEqual(
            set(outputs),
            {
                OUTPUT_FILES["subscription"],
                OUTPUT_FILES["ssr"],
                OUTPUT_FILES["shadowrocket"],
                OUTPUT_FILES["clash"],
                OUTPUT_FILES["v2ray"],
            },
        )
        main = base64.b64decode(outputs["subscription.txt"]).decode("utf-8")
        ssr_only = base64.b64decode(outputs["ssr.txt"]).decode("utf-8")
        v2ray_only = base64.b64decode(outputs["v2ray.txt"]).decode("utf-8")
        self.assertIn("ssr://", main)
        self.assertIn("vmess://", main)
        self.assertIn("ssr://", ssr_only)
        self.assertNotIn("vmess://", ssr_only)
        self.assertIn("vmess://", v2ray_only)
        self.assertNotIn("ssr://", v2ray_only)

        import yaml

        clash = yaml.safe_load(outputs["clash.yaml"])
        names = [proxy["name"] for proxy in clash["proxies"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(clash["proxy-groups"][0]["proxies"], names)


if __name__ == "__main__":
    unittest.main()
