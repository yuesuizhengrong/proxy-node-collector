# Proxy Subscription Collector

Collects public SSR, Shadowrocket, Clash, and V2Ray-compatible nodes, removes duplicates, tests candidates with Mihomo, and publishes only nodes that pass the probe.

Use only for lawful, authorized network testing. Public nodes are untrusted and unstable. Do not send credentials, personal data, private keys, cookies, or other sensitive information through them.

## Subscription Links

All files are updated by GitHub Actions after each successful probe run.

| Client / format | Subscription URL |
| --- | --- |
| Main subscription | https://raw.githubusercontent.com/yuesuizhengrong/proxy-node-collector/main/data/subscription.txt |
| SSR | https://raw.githubusercontent.com/yuesuizhengrong/proxy-node-collector/main/data/ssr.txt |
| Shadowrocket | https://raw.githubusercontent.com/yuesuizhengrong/proxy-node-collector/main/data/shadowrocket.txt |
| Clash / Mihomo | https://raw.githubusercontent.com/yuesuizhengrong/proxy-node-collector/main/data/clash.yaml |
| V2Ray / V2RayNG | https://raw.githubusercontent.com/yuesuizhengrong/proxy-node-collector/main/data/v2ray.txt |

`subscription.txt`, `ssr.txt`, `shadowrocket.txt`, and `v2ray.txt` contain Base64 subscriptions. `clash.yaml` is a standard Clash-compatible YAML profile.

Shadowrocket is a client, not a protocol. Its subscription includes the supported `ss`, `ssr`, `vmess`, `vless`, and `trojan` URI node types.

## How It Works

1. Downloads URI, Base64, and Clash YAML sources from `config/sources.yaml`.
2. Collects from both GitHub-hosted sources and public non-GitHub websites.
3. Parses `ss`, `ssr`, `vmess`, `vless`, and `trojan` nodes.
4. Removes duplicate nodes and keeps source metadata.
5. Launches Mihomo in small batches and calls its controller delay endpoint for every candidate.
6. Writes only successful probes to `data/` and commits the updated subscriptions.

The collector caches URLs during a run, streams source responses with an 8 MiB
limit, fetches discovered website pages and payloads concurrently, and stops
parsing a source at its candidate limit. A malformed individual URI is skipped
without discarding the rest of that source. Mihomo probing uses two temporary
batches in parallel and detects an exited process immediately.

If a run tests zero working nodes, it fails without overwriting the previous subscription files. This prevents a temporary source outage or Mihomo/network failure from publishing an empty subscription.

## External Websites

The configured non-GitHub sources currently include:

- ClashNodeFree: `https://clashnodefree.com/`
- XrayVIP: `https://www.xrayvip.com/`
- FreeDatiya: `https://free.datiya.com/` (the `page` source follows the latest article and discovers its subscription URLs)
- ClashGitHub: `https://clashgithub.com/`
- FreeClashNode: `https://www.freeclashnode.com/`
- JCNode: `https://jcnode.com/`
- Yoyapai: `https://yoyapai.com/`
- FreeNode.biz: `https://freenode.biz/`
- FreeV2rayNode: `https://www.freev2raynode.com/`
- YouNeed: `https://www.youneed.win/category/nodeshare`

Direct subscription endpoints and the website search batch are listed in `config/sources.yaml`. Every scheduled update scans each enabled `page` source, follows a bounded number of likely node articles, discovers subscription files, and extracts supported node URIs embedded in page markup. Website failures are isolated, so an unavailable, CAPTCHA-protected, or password-protected site does not stop other sources. The collector does not trust a node merely because it came from a website: every parsed node still has to pass the Mihomo connectivity test before it is published.

## Schedule

The workflow runs every three hours at 15 minutes past the hour in UTC (`00:15`, `03:15`, `06:15`, and so on). In China Standard Time (UTC+8), it runs at `02:15`, `05:15`, `08:15`, `11:15`, `14:15`, `17:15`, `20:15`, and `23:15`.

Manual updates are available from the repository Actions page under `Update tested subscriptions`.

## Local Run

Install dependencies and provide a Mihomo executable:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m proxy_node_collector --config config/sources.yaml --out-dir data --mihomo-bin C:\path\to\mihomo.exe
```

To validate source parsing and generate untested files only:

```powershell
python -m proxy_node_collector --config config/sources.yaml --out-dir data --skip-test
```

## Configuration

`config/sources.yaml` supports source formats `uri`, `base64`, `clash`, `page`, and `auto`. The test controls are:

- `max_tested_nodes`: maximum number of deduplicated candidates tested in a run.
- `mihomo_batch_size`: candidates loaded into each temporary Mihomo process.
- `mihomo_batch_concurrency`: maximum number of temporary Mihomo processes running at once.
- `test_timeout_seconds`: delay probe timeout for a single node.
- `probe_concurrency`: simultaneous controller probe requests.
- `page_link_limit` and `page_payload_limit`: limits for discovering same-site article pages and subscription files from a `page` source.
- `max_source_bytes`: maximum response size accepted from one source.

If an upstream source becomes unreliable, disable it or replace its URL in `config/sources.yaml`.
