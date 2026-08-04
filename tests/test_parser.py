import unittest

from proxy_node_collector.cli import Source, parse_candidates


class ParserTest(unittest.TestCase):
    def test_parse_plain_host_port_uses_source_scheme(self):
        source = Source(name="plain", url="https://example.com/plain.txt", scheme="socks5")

        result = parse_candidates("127.0.0.1:1080\n", source, max_candidates=10)

        self.assertEqual([item.url for item in result], ["socks5://127.0.0.1:1080"])

    def test_parse_url_keeps_declared_scheme(self):
        source = Source(name="mixed", url="https://example.com/mixed.txt", scheme="http")

        result = parse_candidates(
            "socks4://10.0.0.1:9050\nhttp://10.0.0.2:8080\n",
            source,
            max_candidates=10,
        )

        self.assertEqual(
            [item.url for item in result],
            [
                "socks4://10.0.0.1:9050",
                "http://10.0.0.2:8080",
            ],
        )

    def test_skip_authenticated_proxy_lines(self):
        source = Source(name="auth", url="https://example.com/auth.txt", scheme="http")

        result = parse_candidates("http://user:pass@example.com:8080\n", source, max_candidates=10)

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
