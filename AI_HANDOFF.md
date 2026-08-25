# AI Handoff

This file gives an AI coding assistant the context needed to continue this project on another computer.

## Project

- GitHub repository: https://github.com/yuesuizhengrong/proxy-node-collector
- Default branch: `main`
- Purpose: collect public SSR, Shadowrocket-compatible, Clash, and V2Ray-compatible proxy nodes, test them with Mihomo, and publish working subscriptions.
- Source of truth: the Python code, `config/sources.yaml`, and `.github/workflows/update-proxies.yml`.
- `data/` is generated output and may be empty in a fresh package.

## Current behavior

- Supported input formats: `uri`, `base64`, `clash`, and `auto`.
- Supported node protocols: `ss`, `ssr`, `vmess`, `vless`, and `trojan`.
- Sources include GitHub-hosted files plus public non-GitHub websites (`clashnodefree.com`, `xrayvip.com`, and `free.datiya.com`).
- Mihomo tests candidate nodes before publishing them.
- GitHub Actions runs every two hours at 15 minutes past the hour in UTC:
  `15 */2 * * *`.
- The workflow downloads Mihomo, runs the collector, and commits changed files under `data/`.

When adding sources, prefer stable direct subscription endpoints over scraping arbitrary HTML pages. The `page` source type is intentionally limited to same-origin article and subscription URLs. Keep source formats explicit when the endpoint is known (`base64` or `clash`), and verify that the URL is not a GitHub URL when the goal is to expand non-GitHub coverage.

## Subscription URLs

- Main: https://raw.githubusercontent.com/yuesuizhengrong/proxy-node-collector/main/data/subscription.txt
- SSR: https://raw.githubusercontent.com/yuesuizhengrong/proxy-node-collector/main/data/ssr.txt
- Shadowrocket: https://raw.githubusercontent.com/yuesuizhengrong/proxy-node-collector/main/data/shadowrocket.txt
- Clash / Mihomo: https://raw.githubusercontent.com/yuesuizhengrong/proxy-node-collector/main/data/clash.yaml
- V2Ray / V2RayNG: https://raw.githubusercontent.com/yuesuizhengrong/proxy-node-collector/main/data/v2ray.txt

## Local validation

Use Python 3.10 or newer:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -s tests -v
```

The full tested collection requires a Mihomo executable. GitHub Actions installs it automatically.

## Publishing from another computer

The archive intentionally contains no GitHub token. Before asking an AI assistant to publish changes, provide a new token through the assistant's secure credential flow or configure GitHub authentication locally. The token must have permission to read and write repository contents; workflow dispatch also needs Actions permission when required by the authentication method.

Never commit a token into source files, README files, workflow files, the archive, or command output. Keep unrelated changes intact, update the local files first, run the tests, then publish to `yuesuizhengrong/proxy-node-collector` on `main`.
