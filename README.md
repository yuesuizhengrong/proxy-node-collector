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
2. Parses `ss`, `ssr`, `vmess`, `vless`, and `trojan` nodes.
3. Removes duplicate nodes and keeps source metadata.
4. Launches Mihomo in small batches and calls its controller delay endpoint for every candidate.
5. Writes only successful probes to `data/` and commits the updated subscriptions.

## Schedule

The workflow runs four times daily at `00:15`, `06:15`, `12:15`, and `18:15` UTC. In China Standard Time (UTC+8), this is `08:15`, `14:15`, `20:15`, and `02:15` the following day.

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

`config/sources.yaml` supports source formats `uri`, `base64`, `clash`, and `auto`. The test controls are:

- `max_tested_nodes`: maximum number of deduplicated candidates tested in a run.
- `mihomo_batch_size`: candidates loaded into each temporary Mihomo process.
- `test_timeout_seconds`: delay probe timeout for a single node.
- `probe_concurrency`: simultaneous controller probe requests.

If an upstream source becomes unreliable, disable it or replace its URL in `config/sources.yaml`.
