# Proxy Node Collector

公开代理节点采集、去重、可用性测试和每日自动发布项目。项目默认采集 HTTP/HTTPS/SOCKS4/SOCKS5 公开代理列表，并只把测试通过的节点写入 `data/`。

> 只用于合法、授权的网络测试和开发用途。公开代理不稳定且不可信，不要通过公开代理传输账号、密码、密钥、Cookie 或其他敏感信息。

## 功能

- 从 `config/sources.yaml` 配置的公开 URL 采集代理节点。
- 自动解析 `host:port`、`http://host:port`、`socks4://host:port`、`socks5://host:port` 格式。
- 自动去重，并保留节点来源信息。
- 异步测试节点可用性和延迟。
- 每日 GitHub Actions 自动更新 `data/proxies.txt`、`data/proxies.json`、`data/summary.md`。

## 输出文件

- `data/proxies.txt`: 测试通过的代理 URL，一行一个。
- `data/proxies.json`: 带来源、延迟、状态码、检测时间的结构化结果。
- `data/summary.md`: 本次采集摘要和最快节点预览。

## 本地运行

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m proxy_node_collector --config config/sources.yaml --out-dir data
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m proxy_node_collector --config config/sources.yaml --out-dir data
```

只采集不测试:

```bash
python -m proxy_node_collector --config config/sources.yaml --out-dir data --skip-test
```

## 每日自动更新

项目内置 `.github/workflows/update-proxies.yml`，推送到 GitHub 后会：

1. 每天 `03:15 UTC` 自动运行。
2. 安装 Python 依赖。
3. 采集并测试代理节点。
4. 如果 `data/` 有变化，自动提交并推送。

你也可以在 GitHub 仓库页面进入 `Actions -> Update proxy nodes -> Run workflow` 手动触发。

## 发布到 GitHub

先在 GitHub 创建一个空仓库，例如 `proxy-node-collector`，然后在本项目目录执行：

```bash
git init
git add .
git commit -m "initial proxy node collector"
git branch -M main
git remote add origin https://github.com/<your-user>/proxy-node-collector.git
git push -u origin main
```

推送完成后，进入仓库 `Settings -> Actions -> General`，确认 `Workflow permissions` 设置为 `Read and write permissions`，否则自动提交更新会失败。

## 配置采集源

编辑 `config/sources.yaml`：

```yaml
sources:
  - name: "example-http"
    url: "https://example.com/http.txt"
    scheme: "http"
    enabled: true
```

字段说明：

- `name`: 来源名称，会写入 `data/proxies.json`。
- `url`: 公开代理列表 URL。
- `scheme`: `http`、`https`、`socks4`、`socks4a`、`socks5`、`socks5h` 或 `auto`。没有协议前缀的节点会使用这里的协议。
- `enabled`: 是否启用该来源。

常用测试参数也在 `settings` 下配置：

- `test_urls`: 可用性检测 URL。
- `concurrency`: 并发测试数量。
- `test_timeout_seconds`: 单个代理测试超时时间。
- `max_alive`: 最多发布多少个可用代理。

## 注意事项

- 公开代理质量波动很大，每天可用数量可能为 0。
- GitHub Actions 的网络环境和你本机不同，本机可用不代表 Actions 可用。
- 如果采集源失效，直接在 `config/sources.yaml` 中禁用或替换。
- 本项目不会采集或发布带用户名密码的代理。
