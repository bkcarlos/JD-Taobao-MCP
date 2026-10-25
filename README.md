# JD-Taobao Browser MCP

一个本地运行、人工参与的 MCP Server。它通过 Playwright 打开真实、可见的 Chromium 浏览器，让 Codex 或其他 MCP 客户端在京东、淘宝、天猫页面中进行低频浏览、商品搜索、详情提取。

> 本项目仅暴露商品和京东购物车读取工具。用户必须自己完成扫码、密码、验证码、滑块和安全验证；工具不会绕过平台风控，也不会自动购买、加购、结算或支付。

## 功能

- 打开京东、淘宝登录页，并复用独立浏览器资料目录中的登录状态。
- 搜索京东或淘宝商品，返回标题、价格、店铺、评论文本、商品链接和图片。
- `get_cart(platform="jd", max_items=50)` 复用京东登录，只读购物车商品、规格、数量、单价和勾选状态。`selected_total` 是已勾选商品的页面估价；`partial=true` 表示分页、数量上限或无法识别的条目导致结果不完整。目前仅支持京东，不自动翻页。
- 打开商品详情页，提取标题、价格、店铺、规格、图片、产品参数、好评和差评。只返回选定的商品字段，不导出页面全文或原始 JSON-LD。
- 商品详情输出带硬约束：必须包含 `product_url`、`product_parameters`、`good_reviews`、`bad_reviews` 及对应 `status` 字段；`good_reviews` 最多 5 条，`bad_reviews` 最多 2 条。
- 链接工具只返回允许的公开链接；导航不执行网页点击事件。保留公开页面滚动。
- 登录、订单、地址、支付和其他账户页面不允许向 MCP 导出内容。购物车仅通过专用工具返回选定字段，属于私人数据。
- 导航按明确的主机与路径白名单校验，资源请求按平台域名白名单校验；阻止未知第三方请求。详见 [安全边界](SECURITY.md)。

## 项目结构

```text
JD-Taobao-MCP/
├─ server.py                       MCP 工具入口
├─ jd_taobao_mcp/
│  ├─ browser.py                   Playwright 持久化浏览器与受限导航
│  ├─ service.py                   搜索和详情页流程
│  ├─ safety.py                    页面、资源域名和输出链接校验
│  └─ extractors/                  京东/淘宝页面提取器
├─ config_examples/
│  ├─ codex-config.toml            Codex 配置示例
│  └─ mcp-client.json              Cursor、Claude Desktop 等配置示例
├─ scripts/
│  ├─ install_windows.ps1          Windows 安装脚本
│  └─ start_windows.bat            Windows 启动脚本
├─ .env.example                    环境变量示例
├─ pyproject.toml                  Python 包配置
└─ README.md                       项目主页文档
```

## Windows 安装

要求：

- Windows 10/11
- Python 3.11+，推荐 Python 3.12
- Codex CLI 或其他支持 MCP 的客户端

在 PowerShell 中执行：

```powershell
git clone https://github.com/bkcarlos/JD-Taobao-MCP.git
cd JD-Taobao-MCP
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install_windows.ps1
```

脚本会完成：

- 创建 `.venv`
- 安装本项目和 MCP SDK
- 安装 Playwright Chromium
- 首次复制 `.env.example` 为 `.env`
- 设置本地 Playwright 浏览器目录 `.ms-playwright`

手动安装：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e "."
.\.venv\Scripts\python.exe -m playwright install chromium
copy .env.example .env
```

本地启动测试：

```powershell
.\.venv\Scripts\python.exe server.py
```

如果服务进入等待状态，没有立即退出，说明 MCP stdio 服务可以被客户端拉起。按 `Ctrl+C` 退出。

## Codex 配置

Codex 的 MCP 配置文件通常位于：

```text
%USERPROFILE%\.codex\config.toml
```

把下面配置追加到 `config.toml`，并把 `<PROJECT_DIR>` 替换为本仓库的绝对路径，例如：

```text
C:\Users\你的用户名\Desktop\JD-Taobao-MCP
```

```toml
[mcp_servers.jd_taobao_browser]
command = "<PROJECT_DIR>/.venv/Scripts/python.exe"
args = ["<PROJECT_DIR>/server.py"]
cwd = "<PROJECT_DIR>"
enabled = true
required = false
startup_timeout_sec = 30
tool_timeout_sec = 120
default_tools_approval_mode = "prompt"

[mcp_servers.jd_taobao_browser.env]
PLAYWRIGHT_HEADLESS = "false"
PLAYWRIGHT_BROWSERS_PATH = ".ms-playwright"
BROWSER_PROFILE_DIR = ".tmp/browser-profile"
TAOBAO_SEARCH_MODE = "mobile"
```

也可以用命令添加：

```powershell
codex mcp add jd-taobao-browser --env PLAYWRIGHT_HEADLESS=false --env PLAYWRIGHT_BROWSERS_PATH=.ms-playwright --env BROWSER_PROFILE_DIR=.tmp/browser-profile --env TAOBAO_SEARCH_MODE=mobile -- C:\Users\你的用户名\Desktop\JD-Taobao-MCP\.venv\Scripts\python.exe C:\Users\你的用户名\Desktop\JD-Taobao-MCP\server.py
codex mcp list
```

配置后重启 Codex。看到 `jd-taobao-browser` 或 `jd_taobao_browser` 已启用后即可调用工具。

## Cursor、Claude Desktop 等 JSON 客户端配置

参考 `config_examples/mcp-client.json`：

```json
{
  "mcpServers": {
    "jd-taobao-browser": {
      "command": "<PROJECT_DIR>\\.venv\\Scripts\\python.exe",
      "args": ["<PROJECT_DIR>\\server.py"],
      "env": {
        "PLAYWRIGHT_HEADLESS": "false",
        "PLAYWRIGHT_BROWSERS_PATH": ".ms-playwright",
        "BROWSER_PROFILE_DIR": ".tmp/browser-profile",
        "TAOBAO_SEARCH_MODE": "mobile"
      }
    }
  }
}
```

## 首次使用流程

1. 调用 `browser_start()` 启动可见浏览器。
2. 调用 `open_login(platform="jd")` 或 `open_login(platform="taobao")`。
3. 在浏览器里手动扫码、输入密码、完成验证码或安全验证。
4. 调用 `check_login(platform="jd")` 或 `check_login(platform="taobao")`。
5. 调用 `search_products(...)` 或 `get_product_detail(url)` 提取商品数据。

示例提示：

```text
用 jd-taobao-browser MCP 搜索京东的“RTX 5070 笔记本”，最多返回 10 个，价格 7000 到 12000 元，并按价格从低到高排序。不要点击购买或购物车。
```

```text
打开这个淘宝商品链接，提取标题、当前页面价格、店铺、产品参数、最多 5 条好评和最多 2 条差评；页面要求验证时停止并让我手动处理。
```

## 工具清单

| 工具 | 作用 |
|---|---|
| `browser_start` | 启动可见 Chromium 浏览器 |
| `browser_status` | 查看京东、淘宝浏览器状态 |
| `begin_login` | 生成京东短时私有扫码链接（需配置 `LOGIN_PUBLIC_URL`） |
| `open_login` | 打开京东或淘宝登录页 |
| `check_login` | 检查是否可能已登录，不返回 Cookie 值 |
| `open_url` | 打开白名单内的公开商品、搜索或首页；登录使用 `open_login` |
| `search_products` | 搜索商品并结构化提取 |
| `get_product_detail` | 提取商品详情，包含硬约束字段 |
| `page_snapshot` | 仅返回当前公开页面的允许链接；`max_chars` 保留兼容但不再使用 |
| `extract_current_page` | 对当前商品页面提取链接和选定商品字段 |
| `list_page_elements` | 列出允许的公开链接并分配临时 ref |
| `click_page_element` | 导航到 ref 对应的公开链接，不执行点击事件或提交表单 |
| `scroll_page` | 页面滚动 |
| `browser_close` | 关闭浏览器 |

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PLAYWRIGHT_HEADLESS` | `false` | 是否无头运行；建议保持 `false`，便于人工登录和验证 |
| `BROWSER_CHANNEL` | 空 | 可填 `msedge` 或 `chrome`，留空使用 Playwright Chromium |
| `BROWSER_EXECUTABLE_PATH` | 空 | 指定通用浏览器可执行文件路径 |
| `JD_BROWSER_EXECUTABLE_PATH` | 空 | 单独指定京东浏览器路径 |
| `TAOBAO_BROWSER_EXECUTABLE_PATH` | 空 | 单独指定淘宝/天猫浏览器路径 |
| `TAOBAO_SEARCH_MODE` | `mobile` | 淘宝搜索模式，可选 `mobile` 或 `pc` |
| `BROWSER_PROFILE_DIR` | `~/.jd-taobao-browser-mcp/browser-profile` | 独立浏览器资料目录 |
| `PLAYWRIGHT_BROWSERS_PATH` | `.ms-playwright` | Playwright 浏览器下载目录 |
| `NAVIGATION_TIMEOUT_MS` | `45000` | 页面导航超时 |
| `ACTION_TIMEOUT_MS` | `15000` | 单步操作超时 |
| `ACTION_DELAY_MS` | `700` | 操作间隔 |
| `SLOW_MO_MS` | `80` | Playwright 慢动作延迟 |
| `MAX_SEARCH_RESULTS` | `30` | 单次搜索最多返回数量 |
| `MCP_HTTP_TOKEN` | 空 | HTTP 模式必填，至少 32 个可见 ASCII 字符的独立随机令牌 |
| `MCP_HTTP_HOST` | `127.0.0.1` | HTTP 监听地址；Docker 内使用 `0.0.0.0`，宿主机映射仍为回环地址 |
| `MCP_HTTP_EXTRA_HOSTS` | 空 | 额外允许的精确 Host，逗号分隔，例如 `shopping-mcp:8000`；不允许通配符，不扩大 Origin 白名单 |
| `MCP_HTTP_PORT` | `8000` | HTTP 监听端口，仅绑定 `127.0.0.1` |
| `PROXY` | 空 | 可选代理，例如 `http://127.0.0.1:7897` |

## HTTP 模式

默认使用 stdio。本机 HTTP 模式需要访问令牌：

```powershell
$env:MCP_HTTP_TOKEN = (.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))")
.\.venv\Scripts\python.exe server.py --http
```

客户端连接 `http://127.0.0.1:8000/mcp`，在每个请求中发送 `Authorization: Bearer <MCP_HTTP_TOKEN>`。令牌只能保存在客户端的私有配置中，不要放入 URL 或提交到 Git。缺少令牌时服务拒绝启动；请求缺少或使用错误令牌时返回 401。所有持有令牌的客户端共享同一浏览器会话，不支持多用户隔离。

跨机器访问需要自行配置可信的 TLS 反向代理或加密隧道，保留认证头，避免明文传输令牌。优先在同一台机器上使用 stdio。

## Docker 部署

```sh
cp .env.example .env
# 在私有 .env 中设置独立的随机 MCP_HTTP_TOKEN，然后启动：
docker compose up -d --build
```

宿主机通过 `http://127.0.0.1:18123/mcp` 访问，发送 `Authorization: Bearer <MCP_HTTP_TOKEN>`。端口不发布到公网。使用 `MCP_HOST_PORT` 调整宿主机端口，使用 `MCP_ENV_FILE` 指定已有私有环境文件；容器内部始终监听 8000。

容器以非 root 用户运行，启用 Chromium 沙箱，根文件系统只读，浏览器资料保存在 `browser-data` 命名卷。`docker compose down` 保留登录资料；`down -v` 会删除资料。不要在两个实例间同时挂载并使用同一浏览器资料。

```sh
docker compose ps
docker compose logs --tail=100
docker compose run --rm --no-deps -e MCP_BROWSER_TESTS=1 mcp python -m unittest discover -s tests -v
```

健康检查验证未授权请求返回 401；首次部署还需通过带认证的 MCP 客户端调用 `browser_start` 验证沙箱。服务器默认无头模式，登录需要另行安排人工浏览器会话。Docker 权限与 seccomp 配置见 [docker/README.md](docker/README.md)。

## 安全边界与兼容性变化（0.1.1）

- 登录必须由用户直接在浏览器完成，不返回密码、验证码、输入框值、Cookie 名称或 Cookie 值。
- 移除 `type_into_element`、`take_screenshot`、`go_back`；不再使用按钮点击或回车提交表单。旧配置启用 `ALLOW_STATE_CHANGING_ACTIONS=true` 会拒绝启动。
- 快照只返回公开链接。详情不再返回全文或原始 JSON-LD，也不再从整页正文猜测参数与评论。需要点击展开才能读取的内容可能返回空数组及状态原因。
- 请求在发出前检查白名单。导航重定向转换为重新检查的 GET 导航；资源重定向及需要保留请求体的 307/308 跳转被拒绝。WebSocket、Service Worker 和下载被禁用。
- 严格策略可能阻止部分登录、验证码或 CDN 流程；不要为恢复功能而改成允许所有域名。本版本没有使用真实购物账号验证平台兼容性。
- 商品页面内容仍是不可信外部数据，不能当作工具指令。站点自身脚本仍会在允许的平台域名间发请求，因此这不是完整的浏览器网络沙箱，也不保证网站侧零状态变化。
- 浏览器资料目录包含登录状态，默认保存在本机；不要共享、上传或提交。详见 [SECURITY.md](SECURITY.md)。

## 常见问题

### 为什么不要使用日常 Chrome/Edge 用户目录？

Playwright 持久化上下文会保存 Cookie、本地存储和登录状态。不要把 `BROWSER_PROFILE_DIR` 指向日常浏览器默认用户目录；浏览器可能拒绝并发使用，也会增加误操作风险。

### 页面要求验证怎么办？

工具会返回 `requires_user_verification=true` 或在状态字段里说明原因。此时需要用户在可见浏览器中手动完成验证，然后再继续调用工具。

### GitHub 主页为什么会空？

GitHub 只会把仓库根目录的 `README.md` 显示在项目主页。详细文档必须放在仓库根目录，而不是只放在子目录或缺失的路径下。

## 开发命令

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

```powershell
.\scripts\start_windows.bat
```

启用离线 Chromium 回归测试（仅使用合成页面和本机测试服务器）：

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
$env:MCP_BROWSER_TESTS = "1"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

CI 会运行这两类测试；本地也可用 `MCP_BROWSER_TEST_EXECUTABLE` 指定测试浏览器。测试不会访问真实商城或使用登录资料。

## License

MIT


### Docker 中人工登录

配置 `LOGIN_PUBLIC_URL` 为用户可以打开的私有入口，例如
`https://shopping.example.test/login`。反向代理须保留 `/login` 路径与原始 Host，
仅转发 `/login` 及其子路径。不要暴露整个 MCP 服务，也不要在链接中放入 MCP 令牌。
也可使用 `http://127.0.0.1:18123/login` 配合 SSH 本地转发；HTTP 入口仅允许回环地址。

在 MCP 客户端给 bot 开放 `begin_login` 工具。用户要求登录时，bot 调用
`begin_login(platform="jd")` 并返回 `login_url`。用户本人打开链接，可点击“显示二维码”并用京东 App 确认，
或选择“账号密码登录”，由本人填写并提交。入口链接一次性使用，整个会话最多 10 分钟；
刷新已打开的页面会复用 HttpOnly 会话 Cookie。二维码不作为 MCP 工具结果返回。
目前私有登录入口仅支持京东。账号密码只能通过私有页面提交，不是 MCP 工具参数；
服务不记录或落盘保存账号密码，每个入口最多接收三次密码表单提交。
如京东要求额外验证，可在“京东官方登录页面”区域亲自点击或按住拖动，画面在拖动过程中更新。
鼠标和触屏均可操作；单次拖动最长 15 秒，切换页面或中断后会释放按下状态。
需要文字验证码时，先在该区域点选验证码输入框，再用页面提供的验证码栏填入；系统不会识别或自动解验证码。
所有交互仅限固定的京东登录文档，成功、关闭或超时后会清除临时登录页面。
如果手机扫码确认后一直等待，可打开“查看京东验证页面”检查是否需要进一步操作。
必须看到“登录状态已保存”才表示凭据已写入；提交密码或关闭入口都不代表登录成功。

检测到登录凭据后，页面先将 Cookie 原子保存到浏览器私有目录，再提示保存完成。
资料目录权限为 0700，`login-cookies.json` 权限为 0600，保存在现有 Docker 数据卷中。
服务重启会恢复尚未过期且浏览器中不存在的 Cookie（包括会话 Cookie）；正常关闭也会更新快照。
网站仍可撤销登录或要求重新验证，`check_login` 仅表示凭据存在，不保证服务端仍接受。
登录期间同一浏览器的商品导航暂时拒绝；完成、关闭入口或会话超时后恢复。
关闭入口不会清除登录。不要删除数据卷，不要将资料目录或 Cookie 快照提交到仓库。
