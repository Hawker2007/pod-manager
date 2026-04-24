# DevPod Control Center — Agent Implementation Spec
> Version 0.2 — auth clarified, URL scheme updated  
> Target: autonomous coding agent (Claude Code or similar)

---

## 0. Mission

Build a self-contained web-based control center that runs **inside** a Linux development pod on AKS. It must be reachable over HTTPS through a corporate proxy, require no external dependencies at runtime, and be launchable as a single supervisord-managed service. The UI must work on a weak Citrix VM client — all computation stays in the pod.

---

## 1. Technology Choices & Rationale

### Backend
| Choice | Rationale |
|---|---|
| **Python 3.11+** | Already present in micromamba base env; no extra runtime |
| **FastAPI** | Async-native, SSE support, auto OpenAPI docs, minimal overhead |
| **uvicorn** | ASGI server; single worker sufficient (all clients are one user) |
| **psutil** | Process table, per-process CPU/RAM — pure Python, no root needed |
| **httpx[asyncio]** | Async HTTP for connectivity checks; respects proxy env vars natively |
| **asyncssh** | GitHub SSH reachability check without shelling out |
| **xmlrpc.client** (stdlib) | Supervisord XML-RPC over unix socket — zero extra deps |
| **No database** | All state is ephemeral (cgroup files, supervisord, filesystem). History stored in a bounded in-memory ring buffer only. |

### Frontend
| Choice | Rationale |
|---|---|
| **React 18** | Component model fits panel-per-feature layout |
| **Vite** | Fast dev build; produces a single `/dist` static bundle for nginx |
| **Recharts** | Lightweight charting; SSE-fed sparklines |
| **No Redux** | Single user, single pod — React context + useReducer is enough |
| **Tailwind CSS** | Utility classes; no design system to maintain |
| **Native fetch + EventSource** | No extra HTTP lib; SSE reconnects automatically |

### Infrastructure (inside pod)
| Component | Role |
|---|---|
| **nginx** | TLS termination, static SPA serving, reverse proxy to FastAPI and to running dev services |
| **supervisord** | Manages nginx, FastAPI/uvicorn, and all user dev services |
| **micromamba** | Package management; the control center backend lives in its own env |

---

## 2. Repository Layout

```
devpod-control-center/
├── backend/
│   ├── main.py                  # FastAPI app factory, mounts all routers
│   ├── config.py                # Pydantic Settings — reads env vars
│   ├── sse.py                   # SSE broadcaster (asyncio.Queue fan-out)
│   ├── collector.py             # Background metric collection loop (2s)
│   ├── routers/
│   │   ├── metrics.py           # GET /events  (SSE)
│   │   ├── processes.py         # GET /api/processes
│   │   ├── disk.py              # GET /api/disk
│   │   ├── services.py          # GET/POST /api/services/*
│   │   ├── packages.py          # GET/POST /api/packages/*
│   │   ├── diagnostics.py       # GET /api/diag
│   │   ├── containers.py        # GET/POST /api/containers/*  (optional — podman only)
│   │   └── info.py              # GET /api/info  (pod identity)
│   ├── lib/
│   │   ├── cgroup.py            # cgroup v2 file readers
│   │   ├── supervisord.py       # XML-RPC client wrapper
│   │   ├── mamba.py             # micromamba subprocess wrapper
│   │   ├── podman.py            # podman subprocess wrapper (no-op when binary absent)
│   │   └── diag_checks.py       # Individual connectivity check coroutines
│   └── requirements.txt
│
├── frontend/
│   ├── index.html
│   ├── vite.config.ts
│   ├── tailwind.config.ts
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx              # Shell: sidebar nav + panel router
│   │   ├── store/
│   │   │   └── metrics.tsx      # Context + useReducer for SSE state
│   │   ├── hooks/
│   │   │   ├── useSSE.ts        # EventSource hook with auto-reconnect
│   │   │   ├── useApi.ts        # Thin fetch wrapper (REST calls)
│   │   │   └── usePolling.ts    # Interval-based refresh for slow endpoints
│   │   ├── panels/
│   │   │   ├── Resources.tsx    # CPU / RAM / swap gauges + sparklines
│   │   │   ├── Disk.tsx         # /home and /projects usage bars + top dirs
│   │   │   ├── Services.tsx     # Supervisord service list + catalog
│   │   │   ├── Packages.tsx     # Micromamba search + install + envs
│   │   │   ├── Diagnostics.tsx  # Connectivity check grid
│   │   │   ├── Containers.tsx   # Podman containers (hidden when podman unavailable)
│   │   │   └── Access.tsx       # SSH strings + HTTPS service URLs
│   │   └── components/
│   │       ├── Topbar.tsx       # Pod identity bar
│   │       ├── Sidebar.tsx      # Nav with active state
│   │       ├── MetricGauge.tsx  # Reusable bar + sparkline card
│   │       ├── StatusBadge.tsx  # running / stopped / starting / error
│   │       ├── ServiceRow.tsx   # Single supervisord service line
│   │       ├── DiagRow.tsx      # Single connectivity check line
│   │       └── InstallLog.tsx   # Streaming install output panel
│
├── nginx/
│   └── devpod-cc.conf           # nginx server block (template)
│
├── supervisord/
│   └── control-center.conf      # supervisord program block
│
├── scripts/
│   ├── install.sh               # Bootstrap: mamba env + pip + build frontend
│   └── uninstall.sh
│
└── README.md
```

---

## 3. Configuration

All runtime config via environment variables, with sane defaults. Defined in `backend/config.py` using `pydantic-settings`.

```python
# backend/config.py
class Settings(BaseSettings):
    # Pod identity (injected by K8s downward API or set manually)
    POD_NAME: str = "devpod-unknown"
    POD_NAMESPACE: str = "devpods"
    NODE_NAME: str = ""

    # Paths
    CGROUP_ROOT: Path = Path("/sys/fs/cgroup")
    SUPERVISORD_SOCK: str = "unix:///tmp/supervisor.sock"
    MICROMAMBA_BIN: str = "micromamba"
    MICROMAMBA_ROOT: Path = Path("/home/user/micromamba")

    # Podman sidecar — optional feature, enabled automatically when binary is found
    # The backend calls `shutil.which(PODMAN_BIN)` at startup to set PODMAN_AVAILABLE.
    # If the binary is absent the /api/containers/* router is still registered but
    # every endpoint returns 503 {"error": "podman_unavailable"} immediately.
    # The frontend checks GET /api/info to decide whether to show the Containers panel.
    PODMAN_BIN: str = "podman"          # path or name on $PATH
    PODMAN_SOCKET: str = ""             # optional: "unix:///run/user/1000/podman/podman.sock"
                                        # if set, use REST API instead of subprocess

    # Metric collection
    METRICS_INTERVAL_SEC: float = 2.0
    METRICS_HISTORY_LEN: int = 60        # 60 samples = 2 min of sparkline

    # Disk — which paths to report
    DISK_MOUNT_PATHS: list[str] = ["/home", "/projects"]

    # Diagnostics — targets
    DIAG_GITHUB_SSH_HOST: str = "github.com"
    DIAG_GITHUB_API_URL: str = "https://api.github.com"
    DIAG_PROXY_TEST_URL: str = "https://www.google.com"
    DIAG_EXTRA_HOSTS: list[str] = []    # ["registry.internal:5000"]

    # Corp ingress URL pattern
    # Auth is handled entirely by the in-house ingress controller — the backend
    # needs no auth logic. A service listening on port P in pod POD_NAME is
    # automatically reachable (after ingress auth) at:
    #   https://<INGRESS_BASE_HOST>-<P>-<POD_NAME>.<INGRESS_DOMAIN>
    # Example: webpack-dev on :3000 in pod devpod-user42 →
    #   https://devpod-3000-devpod-user42.pods.corp
    INGRESS_BASE_HOST: str = "devpod"    # prefix segment before the port
    INGRESS_DOMAIN: str = "pods.corp"    # domain suffix after pod name
    CC_INGRESS_PORT: int = 8080          # port nginx listens on (control center itself)

    # Computed helper — NOT a setting, defined as a method on Settings:
    # def service_url(self, port: int) -> str:
    #     return f"https://{self.INGRESS_BASE_HOST}-{port}-{self.POD_NAME}.{self.INGRESS_DOMAIN}"
```

---

## 4. Backend Implementation Details

### 4.1 SSE Broadcaster (`sse.py`)

Implement a pub/sub fan-out so multiple browser tabs can all receive the same metric stream without running the collector multiple times.

```python
# Pattern to implement:
class SSEBroadcaster:
    def __init__(self): self._queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q = asyncio.Queue(maxsize=10)
        self._queues.append(q)
        return q

    def unsubscribe(self, q): self._queues.remove(q)

    async def publish(self, event: dict):
        data = json.dumps(event)
        for q in list(self._queues):
            try: q.put_nowait(data)
            except asyncio.QueueFull: pass  # drop oldest, slow client
```

The SSE endpoint in `routers/metrics.py` streams with:
```
data: {json}\n\n
```
Keep-alive comment (`": ping\n\n"`) every 15s to prevent proxy timeouts through Citrix.

### 4.2 Metric Collector (`collector.py`)

Background task started in FastAPI `lifespan`. Runs every `METRICS_INTERVAL_SEC`.

**CPU calculation from cgroup v2:**
```python
# Read /sys/fs/cgroup/cpu.stat twice, 1 second apart
# usage_usec delta / (1_000_000 * elapsed) / num_cpus * 100 = CPU%
# Also read /sys/fs/cgroup/cpu.max for limit (e.g. "800000 100000" = 8 cores)
```

**Memory:**
```python
# memory.current  = current bytes used
# memory.max      = limit bytes (or "max" if unlimited)
# memory.swap.current, memory.swap.max for swap
```

**Per-process top list** (psutil):
```python
# psutil.process_iter(['pid','name','cmdline','cpu_percent','memory_info','status'])
# Sort by cpu_percent desc, take top 8
# cpu_percent requires two calls — maintain process objects between ticks
```

**Ring buffer:** Maintain `deque(maxlen=METRICS_HISTORY_LEN)` for cpu_pct and mem_pct. These are returned in the SSE payload so the frontend can draw sparklines without requesting history separately.

**SSE payload schema (emitted every 2s):**
```json
{
  "ts": 1719000000.123,
  "cpu": {
    "pct": 40.2,
    "cores_used": 3.2,
    "cores_limit": 8,
    "history": [22, 35, 40, ...]
  },
  "mem": {
    "pct": 57.4,
    "used_gb": 18.4,
    "limit_gb": 32.0,
    "history": [50, 52, 54, ...]
  },
  "swap": {
    "pct": 10.0,
    "used_gb": 0.4,
    "limit_gb": 4.0,
    "history": [8, 9, 8, ...]
  },
  "processes": [
    {"pid": 1842, "name": "java", "cmdline_short": "IntelliJ IDEA", "cpu_pct": 18.2, "mem_pct": 22.4, "rss_gb": 7.2},
    ...
  ]
}
```

### 4.3 cgroup Reader (`lib/cgroup.py`)

```python
# Functions to implement:
def read_cpu_stat(cgroup_root) -> dict:          # returns usage_usec, nr_throttled, etc.
def read_cpu_max(cgroup_root) -> tuple[int,int]: # (quota_us, period_us) → effective cores
def read_memory_current(cgroup_root) -> int:     # bytes
def read_memory_max(cgroup_root) -> int | None:  # bytes or None if "max"
def read_swap_current(cgroup_root) -> int:
def read_swap_max(cgroup_root) -> int | None:
def read_io_stat(cgroup_root) -> dict:           # per-device read/write bytes (future)
```

Always handle `FileNotFoundError` gracefully — return `None` and log a warning. The cgroup path may differ if the pod uses a non-root cgroup slice; make the root configurable.

### 4.4 Supervisord Client (`lib/supervisord.py`)

Use Python's `xmlrpc.client.ServerProxy` with a custom `UnixStreamTransport`:

```python
import xmlrpc.client, socket, http.client

class UnixStreamHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.unix_socket_path)

# Wrap with ServerProxy pointing at http://localhost/RPC2
# Methods to expose:
#   supervisor.getAllProcessInfo() → list of process dicts
#   supervisor.startProcess(name)
#   supervisor.stopProcess(name)
#   supervisor.restartProcess(name)  # = stopProcess + startProcess
#   supervisor.getProcessInfo(name)
#   supervisor.readProcessStdoutLog(name, offset, length) → for log tail
```

**Service catalog** — maintain a static `catalog.json` (or hardcoded dict) of installable services with their supervisord `.conf` snippet and any pre-start commands. When user adds a service from the catalog, write the `.conf` to supervisord's include dir and call `supervisor.reloadConfig()` then `supervisor.addProcessGroup(name)`.

### 4.5 Micromamba Wrapper (`lib/mamba.py`)

All mamba operations run as async subprocesses. Long-running installs stream stdout back over SSE using a **job** pattern:

```python
# Job lifecycle:
# 1. POST /api/packages/install  → returns {"job_id": "pkg-abc123", "status": "started"}
# 2. Client subscribes to SSE — install log lines arrive as:
#    {"type": "job_output", "job_id": "pkg-abc123", "line": "...", "done": false}
# 3. On completion: {"type": "job_output", "job_id": "pkg-abc123", "done": true, "exit_code": 0}
```

```python
# Functions to implement:
async def search(query: str, channel: str) -> list[dict]:
    # micromamba search --json <query> -c <channel>
    # Returns list of {name, version, channel, build}

async def install(package: str, env: str, channel: str, job_id: str):
    # micromamba install -n <env> <package> -c <channel> -y
    # Stream stdout lines to SSEBroadcaster with job_id tag

async def list_envs() -> list[dict]:
    # micromamba env list --json
    # Returns list of {name, prefix, is_active}

async def list_installed(env: str) -> list[dict]:
    # micromamba list -n <env> --json
```

### 4.6 Diagnostics (`lib/diag_checks.py`)

Each check is an independent async coroutine returning a `DiagResult`:

```python
@dataclass
class DiagResult:
    name: str
    status: Literal["ok", "warn", "fail", "checking"]
    detail: str       # human-readable detail line
    latency_ms: int | None
    checked_at: float # unix timestamp
```

**Checks to implement:**

| Check name | Implementation |
|---|---|
| `proxy` | `httpx.get(DIAG_PROXY_TEST_URL, proxies=..., timeout=5)` — verify HTTP_PROXY env is set and works |
| `github_ssh` | `asyncssh.connect("github.com", port=22, known_hosts=None)` — parse banner for "Hi username!" |
| `github_https` | `httpx.get("https://api.github.com", timeout=5)` — check 200 + x-github-request-id header |
| `dns` | `asyncio.get_event_loop().getaddrinfo("github.com", None)` — measure resolution time |
| `npm_registry` | `httpx.head("https://registry.npmjs.org/lodash", timeout=8)` — warn if >2s |
| `docker_registry` | `httpx.get(f"http://{DOCKER_REGISTRY}/v2/", timeout=3)` — configurable host |
| `aks_metadata` | `httpx.get("http://169.254.169.254/metadata/instance?api-version=2021-02-01", headers={"Metadata":"true"}, timeout=2)` |

`GET /api/diag` runs all checks **concurrently** with `asyncio.gather`. Cache results for 30s — the client can force refresh with `?force=true`.

### 4.7 Disk (`routers/disk.py`)

```python
# GET /api/disk
# Response schema:
{
  "mounts": [
    {
      "path": "/home",
      "total_gb": 256.0,
      "used_gb": 122.0,
      "free_gb": 134.0,
      "pct": 47.7,
      "top_dirs": [
        {"path": "/home/.m2/repository", "size_gb": 72.1},
        {"path": "/home/.cache",          "size_gb": 30.2},
        {"path": "/home/.local",          "size_gb": 9.8}
      ]
    },
    { "path": "/projects", ... }
  ]
}
```

`top_dirs` is computed by running `du -x --max-depth=3 <mount> --block-size=1` as a subprocess and sorting. This can be slow — run it in a `ThreadPoolExecutor` and cache for 60s. Return stale data immediately with `"stale": true` flag if cache is warm, trigger refresh in background.

### 4.8 Pod Info (`routers/info.py`)

```python
# GET /api/info  — called once on app load, cached for the session
# All URLs are constructed using settings.service_url(port)
{
  "pod_name": "devpod-user42",
  "namespace": "devpods",
  "node_name": "aks-nodepool-01-vmss000003",
  "start_time": "2024-06-20T10:00:00Z",
  "cpu_limit_cores": 8,
  "mem_limit_gb": 32.0,
  "uptime_sec": 212400,
  # Control center's own ingress URL (for display in the topbar / sharing)
  "control_center_url": "https://devpod-8080-devpod-user42.pods.corp",
  # SSH — pod name is the hostname, resolved by corp DNS
  "ssh_host": "devpod-user42",
  "ssh_port": 22,
  # URL pattern — frontend uses this to construct service links
  # Template: replace {port} with the service port number
  "service_url_template": "https://devpod-{port}-devpod-user42.pods.corp",
  # Feature flags — frontend uses these to show/hide optional panels
  "features": {
    "podman": true    // false when podman binary not found at startup
  }
}
```

The `service_url_template` field lets the frontend construct URLs for any port without knowing the ingress pattern at build time. The backend fills in the pod-specific parts; the frontend just does `template.replace("{port}", String(port))`.

### 4.9 Services API (`routers/services.py`)

```
GET  /api/services              → list all supervisord processes + status
POST /api/services/{name}/start → supervisor.startProcess(name)
POST /api/services/{name}/stop  → supervisor.stopProcess(name)
POST /api/services/{name}/restart → stop + start
GET  /api/services/{name}/logs  → last 200 lines via readProcessStdoutLog
GET  /api/services/catalog      → list of installable service templates
POST /api/services/catalog/{name}/add  → write .conf, reload supervisord
DELETE /api/services/{name}     → stop + remove .conf + reload
```

**Service response schema:**
```json
{
  "name": "postgresql",
  "description": "PostgreSQL 15",
  "status": "running",
  "pid": 1842,
  "uptime_sec": 86400,
  "exitcode": null,
  "stdout_logfile": "/var/log/supervisor/postgresql.log",
  "https_port": null,
  "https_url": null
}
```

`https_port` is an optional integer parsed from a comment in the supervisord `.conf` file:
```ini
[program:webpack-dev]
command=npm run dev
; cc_https_port=3000
; cc_description=Webpack dev server
```
The backend parses lines beginning with `; cc_https_port=` from the `.conf` file at service registration time and stores them alongside the supervisord process info. `https_url` is derived at response time using `settings.service_url(https_port)` and is `null` if `https_port` is not set.

The `; cc_` prefix convention is used for all control-center metadata in supervisord configs — this way it's human-readable, won't conflict with supervisord's own directives, and the backend has a clear parsing target. Additional supported annotations:
```ini
; cc_https_port=3000          # port the ingress should route to
; cc_description=short text   # shown in the Services panel
; cc_category=database        # optional grouping tag in the UI
```

---

## 5. Frontend Implementation Details

### 5.1 SSE Hook (`hooks/useSSE.ts`)

```typescript
// useSSE(url: string): MetricsState
// - Creates EventSource on mount
// - Dispatches to metricsReducer on each message
// - Handles onerror with exponential backoff reconnect (1s, 2s, 4s, max 30s)
// - Shows "reconnecting" banner in Topbar when disconnected
// - Cleans up EventSource on unmount
```

### 5.2 Metrics Store (`store/metrics.tsx`)

```typescript
interface MetricsState {
  connected: boolean
  lastTs: number | null
  cpu: { pct: number; coresUsed: number; coresLimit: number; history: number[] } | null
  mem: { pct: number; usedGb: number; limitGb: number; history: number[] } | null
  swap: { pct: number; usedGb: number; limitGb: number; history: number[] } | null
  processes: ProcessInfo[]
}
// useReducer with action types: METRICS_UPDATE, CONNECTED, DISCONNECTED
// Exposed via MetricsContext — consumed by Resources panel and Topbar
```

### 5.3 Panel Routing

Use a simple string state in `App.tsx` (`activePanel`) — no React Router needed. The sidebar sets it, the content area renders the matching panel component. All panels are always mounted (use `display: none` to hide inactive ones) so they don't lose local state when switching tabs.

### 5.4 Resources Panel

- Three `MetricGauge` cards in a row: CPU, Memory, Swap
- Each gauge shows: current value + limit, percentage bar (colored by severity: green <70%, amber <85%, red ≥85%), sparkline from history array
- Process table below: sortable by CPU% or MEM%, shows top 8
- cgroup limits displayed as a mono code row at the bottom
- Data comes entirely from SSE — no REST call needed

### 5.5 Services Panel

- On mount: `GET /api/services` → render list
- On mount: `GET /api/services/catalog` → render catalog chips
- Each `ServiceRow`: name, PID (if running), status badge, action buttons
  - Running: Restart, Stop, View logs
  - Stopped: Start, Remove
  - Starting/stopping: spinner, buttons disabled
- "View logs" opens an inline `<pre>` below the row (slide-down), fetches `GET /api/services/{name}/logs`, auto-refreshes every 5s while open
- Catalog chips: clicking one calls `POST /api/services/catalog/{name}/add`, then refreshes the list
- Optimistic UI: immediately set status to "starting"/"stopping" on button click, reconcile on next poll

### 5.6 Packages Panel

- Environment selector at top (from `GET /api/packages/envs`)
- Search input + channel selector → `GET /api/packages/search?q=&channel=`
- Results list with Install button per package
- Install triggers `POST /api/packages/install` → opens `InstallLog` panel
- `InstallLog`: subscribes to SSE, filters messages by `job_id`, displays streaming log lines in a `<pre>`, shows success/failure banner on completion
- Installed packages tab: `GET /api/packages/list?env=` — searchable, with uninstall button

### 5.7 Diagnostics Panel

- On mount + every 60s: `GET /api/diag` (uses cached results)
- "Run all checks" button: `GET /api/diag?force=true`
- Individual re-check: `GET /api/diag/{check_name}?force=true`
- While checking: spinner in status dot, latency shows "…"
- Fail rows show a "Troubleshoot ↗" link that calls `sendPrompt` / opens a help modal
- Last-checked timestamp shown per row
- Proxy environment variable display: shows `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` values (masked if they contain credentials)

### 5.8 Access Panel

- Pod info from `GET /api/info` (fetched once on mount, stored in React context)
- SSH connection string with copy button:
  ```
  ssh <pod_name>
  ```
- IDE deep-link buttons:
  - IntelliJ Gateway: `jetbrains-gateway://connect#host=<ssh_host>&port=22&projectPath=/projects`
  - VS Code Remote: `vscode://vscode-remote/ssh-remote+<ssh_host>/projects`
- **Running HTTPS services** — derived from `GET /api/services`, filtered to services where `https_port` is set and status is `running`. URL constructed as:
  ```typescript
  const url = info.service_url_template.replace("{port}", String(svc.https_port))
  // e.g. "https://devpod-3000-devpod-user42.pods.corp"
  ```
  Display: service name, port badge, full URL as a clickable link, copy button.
- **Stopped services with a port** — show greyed out with a "Start service" shortcut button
- Quick commands section: static copy-able shell snippets (proxy export, mamba activate, git config workarounds)

---

## 6. nginx Configuration Template

nginx's **only jobs** inside the pod are: serve the static SPA bundle and reverse-proxy `/api/` and `/events` to FastAPI. TLS termination and authentication are handled upstream by the corp ingress controller — nginx listens on plain HTTP.

There is **no** `/app/:port/` proxy pattern. Each dev service that needs HTTPS access is registered in supervisord with a port annotation, and the corp ingress controller automatically exposes it at the standard `https://{INGRESS_BASE_HOST}-{port}-{POD_NAME}.{INGRESS_DOMAIN}` URL. The control center backend constructs these URLs for display — it does not proxy the traffic itself.

```nginx
# nginx/devpod-cc.conf
# Listens on plain HTTP — TLS and auth are handled by the corp ingress controller.
# The ingress routes https://devpod-<CC_INGRESS_PORT>-<POD_NAME>.pods.corp
# to this pod on port <CC_INGRESS_PORT>.

server {
    listen ${CC_INGRESS_PORT};
    server_name _;

    # Serve React SPA static bundle
    root /opt/devpod-control-center/frontend/dist;
    index index.html;
    try_files $uri $uri/ /index.html;

    # REST API → FastAPI (bound to 127.0.0.1:8000)
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;
    }

    # SSE stream — must disable all buffering or events are held until buffer fills
    location /events {
        proxy_pass http://127.0.0.1:8000/events;
        proxy_http_version 1.1;
        proxy_set_header Connection "";   # force keep-alive to upstream
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding on;
        # X-Accel-Buffering header from FastAPI response also disables buffering
        # as a belt-and-suspenders measure for any intermediate proxy in the corp network
    }
}
```

**Important note for the agent**: Do not add TLS `listen 443 ssl` directives or `ssl_certificate` paths. The ingress handles TLS. Adding SSL config here would break the setup.

---

## 7. Supervisord Configuration

```ini
# supervisord/control-center.conf
[program:control-center]
command=/home/user/micromamba/envs/cc-env/bin/uvicorn main:app --host 127.0.0.1 --port 8000 --log-level info
directory=/opt/devpod-control-center/backend
autostart=true
autorestart=true
startretries=5
startsecs=3
stdout_logfile=/var/log/supervisor/control-center.stdout.log
stderr_logfile=/var/log/supervisor/control-center.stderr.log
environment=POD_NAME="%(ENV_POD_NAME)s",POD_NAMESPACE="%(ENV_POD_NAMESPACE)s"
```

---

## 8. Install Script (`scripts/install.sh`)

The script must be idempotent and work without internet if packages are pre-cached.

```bash
#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR=/opt/devpod-control-center
ENV_NAME=cc-env

# 1. Create micromamba environment
micromamba create -n $ENV_NAME python=3.11 -y -c conda-forge
micromamba run -n $ENV_NAME pip install \
  fastapi uvicorn[standard] psutil httpx asyncssh pydantic-settings

# 2. Install Node (if not present) and build frontend
if ! command -v node &>/dev/null; then
  micromamba install -n $ENV_NAME nodejs -c conda-forge -y
fi
cd $INSTALL_DIR/frontend
micromamba run -n $ENV_NAME npm ci
micromamba run -n $ENV_NAME npm run build

# 3. Copy nginx config — substitute CC_INGRESS_PORT (default 8080)
# No TLS config — ingress handles that
export CC_INGRESS_PORT=${CC_INGRESS_PORT:-8080}
envsubst '${CC_INGRESS_PORT}' < $INSTALL_DIR/nginx/devpod-cc.conf \
  > /etc/nginx/conf.d/devpod-cc.conf
nginx -t && supervisorctl signal HUP nginx

# 4. Register control-center with supervisord
cp $INSTALL_DIR/supervisord/control-center.conf \
  /etc/supervisor/conf.d/control-center.conf
supervisorctl reread && supervisorctl update
supervisorctl start control-center

echo "Control center started."
echo "URL: https://${INGRESS_BASE_HOST:-devpod}-${CC_INGRESS_PORT}-${POD_NAME:-unknown}.${INGRESS_DOMAIN:-pods.corp}"
```


---

## 4.9 Podman Container Manager (`lib/podman.py` + `routers/containers.py`)

### Feature detection

At FastAPI startup (inside `lifespan`), call `shutil.which(settings.PODMAN_BIN)`. Store the result as `PODMAN_AVAILABLE: bool` on the app state. Expose it in `GET /api/info` under `features.podman`. The router is always mounted — individual endpoints check the flag and return early with 503 when unavailable. This means the frontend never needs to hard-code feature detection logic; it just reads `info.features.podman`.

If `PODMAN_SOCKET` is set in config, prefer the Podman REST API (HTTP over unix socket) over subprocess for all read operations (list, inspect, stats). Use subprocess for mutating operations (run, stop, rm, pull) regardless — the REST API for those is more complex and subprocess is simpler to stream.

### `lib/podman.py` — subprocess wrapper

All podman commands run as the pod user. The sidecar setup guarantees rootless podman is available when the binary is present — do not add `--privileged` or `sudo` anywhere.

```python
# Base invocation — always add --format json where supported
BASE_CMD = [settings.PODMAN_BIN]

async def is_available() -> bool:
    # shutil.which check — cached at startup, not re-checked per request
    ...

async def ps(all: bool = True) -> list[dict]:
    # podman ps --all --format json
    # Returns raw podman JSON — parsed and re-shaped by the router

async def inspect(container_id: str) -> dict:
    # podman inspect <id> --format json → first element

async def stats_once(container_id: str) -> dict:
    # podman stats <id> --no-stream --format json → cpu%, mem usage/limit

async def pull(image: str, job_id: str):
    # podman pull <image>
    # Streams output lines to SSEBroadcaster with job_id tag (same pattern as mamba install)

async def run(image: str, name: str, args: list[str], job_id: str):
    # podman run --name <name> -d <args> <image>
    # Validate: image matches ^[a-zA-Z0-9_\-\.\/:@]+$
    #           name matches ^[a-zA-Z0-9_\-]+$
    #           args is a pre-validated list from the router — never shell-expanded

async def stop(container_id: str): ...   # podman stop <id>
async def start(container_id: str): ...  # podman start <id>
async def remove(container_id: str, force: bool = False): ...  # podman rm [--force] <id>
async def logs(container_id: str, tail: int = 200) -> str:
    # podman logs --tail <n> <id>

async def images() -> list[dict]:
    # podman images --format json

async def rmi(image_id: str, force: bool = False): ...  # podman rmi [--force] <id>
```

### API endpoints (`routers/containers.py`)

```
GET  /api/containers                    → list all containers (podman ps --all)
GET  /api/containers/{id}               → inspect single container
GET  /api/containers/{id}/logs          → last 200 lines (query param: ?tail=N)
GET  /api/containers/{id}/stats         → one-shot cpu/mem stats (no-stream)
POST /api/containers/{id}/start         → podman start
POST /api/containers/{id}/stop          → podman stop
DELETE /api/containers/{id}             → podman rm (body: {"force": bool})
POST /api/containers/run                → podman run (see body schema below)
GET  /api/containers/images             → podman images list
POST /api/containers/images/pull        → podman pull (streaming via SSE job)
DELETE /api/containers/images/{id}      → podman rmi
```

**POST /api/containers/run — request body:**
```json
{
  "image": "postgres:16",
  "name": "my-postgres",
  "ports": ["5432:5432"],
  "env": {"POSTGRES_PASSWORD": "dev"},
  "volumes": ["/projects/data:/var/lib/postgresql/data"],
  "restart": "unless-stopped",
  "extra_args": []
}
```
The router translates this structured body into a validated args list — it never concatenates strings or uses `shell=True`. Volume paths are validated: source must start with `/home/` or `/projects/` (no escaping to host paths). Port mappings validated against `^\d{1,5}:\d{1,5}$`.

**Container list response (per container):**
```json
{
  "id": "a3f2c1d4e5b6",
  "name": "my-postgres",
  "image": "postgres:16",
  "status": "running",         // running | exited | paused | created
  "state": "Up 2 hours",       // human string from podman
  "created": "2024-06-20T10:00:00Z",
  "ports": ["0.0.0.0:5432->5432/tcp"],
  "cpu_pct": 0.4,              // null if not running
  "mem_usage_mb": 124.0,       // null if not running
  "mem_limit_mb": null,        // null if no --memory set
  "https_url": null            // populated if a port matches a cc_https_port convention (see below)
}
```

### HTTPS URL auto-discovery for containers

Containers get the same ingress URL treatment as supervisord services. Convention: if a container is started with a label `cc.https_port=<port>`, the backend uses `settings.service_url(port)` to populate `https_url` in the response. Example:

```bash
podman run -d --label cc.https_port=8080 --name myapp myimage
```

The backend reads `podman inspect` labels for `cc.https_port` and `cc.description`, identical to the `; cc_` convention used for supervisord `.conf` files. The Access panel shows container HTTPS URLs alongside supervisord service URLs in the same list.

### Frontend — `Containers.tsx` panel

- Panel is **only shown in the sidebar** when `info.features.podman === true`. When podman is unavailable, the sidebar item is absent entirely — no disabled state, no tooltip. The panel does not exist from the user's perspective.
- On mount: `GET /api/containers` + `GET /api/containers/images`
- Auto-refreshes container list every 10s (containers change less frequently than metrics)
- Layout: two tabs — **Containers** and **Images**

**Containers tab:**
- One row per container: name, image (shortened), status badge, port mappings, cpu/mem mini-bars if running
- Actions: Start / Stop / Remove / Logs
- Logs: same inline slide-down `<pre>` pattern as supervisord service logs, refreshes every 5s while open
- "Run new container" button: opens a structured form (image, name, ports, env vars as key-value pairs, volumes) — maps to `POST /api/containers/run`; pull happens automatically if image not present locally (stream pull output via SSE job before run)

**Images tab:**
- List: repository, tag, size, created date
- Pull new image: text input + `POST /api/containers/images/pull` with streaming log
- Remove image: button per row with confirmation (force checkbox if container exists)

### SSE integration for container operations

Reuse the existing job pattern from the mamba installer. Pull and run operations that take time emit `{"type": "job_output", "job_id": "...", "line": "...", "done": false/true}` events. The frontend `InstallLog` component is reusable for this — it only cares about `job_id`, not the source of the job.

---

## 9. Security Considerations

- **Authentication is fully delegated to the corp ingress controller.** The FastAPI backend and nginx must not implement any auth logic. The ingress enforces identity before traffic ever reaches the pod — the backend can trust that any request that arrives is already authenticated. Do not add middleware, tokens, or session checks.
- **FastAPI binds to 127.0.0.1 only** (`--host 127.0.0.1`). Only nginx (also inside the pod) can reach it. Nginx listens on `0.0.0.0:<CC_INGRESS_PORT>` and is the only process reachable from outside the pod. This is the correct trust boundary.
- **CORS**: FastAPI CORS middleware allows only the computed ingress origin (`https://{INGRESS_BASE_HOST}-{CC_INGRESS_PORT}-{POD_NAME}.{INGRESS_DOMAIN}`) — no wildcard. The ingress controller enforces the actual auth; CORS is a defence-in-depth measure against browser-side cross-origin requests.
- **Subprocess sandboxing**: all subprocesses (`micromamba`, `du`) run as the pod user (non-root). Never `shell=True` with user-supplied input — always pass args as a list.
- **Supervisord actions are scoped**: only programs defined in supervisord config can be started/stopped. The API never accepts arbitrary command strings — only program names validated against `supervisor.getAllProcessInfo()` output.
- **Package install validation**: validate package name against `^[a-zA-Z0-9_\-\.]+$` before passing to micromamba. Reject channel names not in an allowlist (`conda-forge`, `defaults`, `bioconda`, `pytorch`).
- **Podman input validation**: container image names validated against `^[a-zA-Z0-9_\-\.\/\:@]+$`. Container names against `^[a-zA-Z0-9_\-]+$`. Volume source paths must begin with `/home/` or `/projects/` — reject anything else to prevent bind-mounting sensitive host paths. Port mappings validated against `^\d{1,5}:\d{1,5}$`. The `extra_args` field in the run body is rejected entirely if non-empty unless a config flag `PODMAN_ALLOW_EXTRA_ARGS=true` is set (off by default).
- **SSE keep-alive comments** do not contain any data — they are plain `: ping\n\n`.

---

## 10. Incremental Build Order (for agent)

Implement in this order so each step is independently testable:

1. **`backend/config.py` + `backend/main.py`** — FastAPI app skeleton, health check `GET /healthz`, uvicorn startup
2. **`lib/cgroup.py`** — cgroup v2 readers with unit tests using fixture files
3. **`collector.py` + `sse.py`** — metric loop + broadcaster; test with `curl -N http://localhost:8000/events`
4. **`routers/metrics.py`** — SSE endpoint wired to broadcaster
5. **`lib/supervisord.py`** — XML-RPC client; test against real supervisord socket
6. **`routers/services.py`** — service list, start/stop/restart, log tail
7. **`routers/disk.py`** — disk usage with background `du` + cache
8. **`lib/diag_checks.py` + `routers/diagnostics.py`** — all connectivity checks
9. **`lib/mamba.py` + `routers/packages.py`** — search, install with job streaming
10. **`routers/info.py`** — pod identity
11. **Frontend scaffold** — Vite + React + Tailwind, `useSSE` hook, MetricsContext, Topbar, Sidebar
12. **Resources panel** — wire to SSE; verify sparklines update live
13. **Services panel** — REST calls, optimistic UI, log viewer
14. **Packages panel** — search, install, streaming log
15. **Diagnostics panel** — concurrent checks, re-check per row
16. **Access panel** — static + dynamic service URLs
17. **`lib/podman.py`** — subprocess wrapper with `is_available()` detection; test each function with real podman if sidecar is present, or mock binary for unit tests
18. **`routers/containers.py`** — all endpoints; verify 503 behaviour when podman absent
19. **`Containers.tsx`** — panel hidden when `features.podman` false; containers tab + images tab; reuse `InstallLog` for pull/run streaming
20. **nginx config + supervisord config + install script**
21. **End-to-end test** — start everything via supervisord, open browser, verify all panels

---

## 11. Known Edge Cases to Handle

| Situation | Handling |
|---|---|
| `memory.max` contains literal `"max"` (no limit set) | Return `null` limit, show "no limit" in UI |
| supervisord socket not found | Return 503 with `{"error": "supervisord_unavailable"}` — Services panel shows warning banner |
| `du` takes >10s on large `/projects` | Return cached stale data immediately; run fresh `du` in background |
| Package install exits non-zero | Parse stderr from micromamba JSON output; surface error in InstallLog |
| SSE client disconnects mid-stream | `unsubscribe()` from broadcaster on generator cleanup; do not leave dangling queues |
| Pod has no cgroup v2 (fallback) | Detect by checking `/sys/fs/cgroup/cgroup.controllers`; fall back to `/proc/meminfo` and `/proc/stat` |
| Multiple browser tabs open | SSEBroadcaster fan-out handles this — each tab gets its own queue |
| GitHub SSH key not configured | `asyncssh` will get permission-denied; return `fail` status with "no SSH key configured" detail |
| Citrix / corp proxy strips `Connection: keep-alive` | nginx sets `proxy_http_version 1.1` and `Connection ""` on SSE route to force chunked transfer; FastAPI also sets `X-Accel-Buffering: no` response header |
| `INGRESS_BASE_HOST` / `INGRESS_DOMAIN` not set | Fall back to `devpod` / `pods.corp` defaults and log a warning at startup; show a config warning banner in the Topbar |
| Service `.conf` file has no `; cc_https_port=` annotation | `https_port` and `https_url` are `null`; service appears in the Services panel but not in the Access panel URLs list |
| `CC_INGRESS_PORT` already in use | Install script checks `ss -tlnp | grep :${CC_INGRESS_PORT}` before writing nginx config and exits with a clear error message |
| Podman binary not on `$PATH` | `shutil.which()` returns `None` at startup → `PODMAN_AVAILABLE=false` → `/api/info` returns `features.podman: false` → Containers panel absent from sidebar. No errors, no retries. |
| Podman sidecar present but socket not ready yet | `podman ps` subprocess returns non-zero; catch `subprocess.CalledProcessError`, return 503 with `{"error": "podman_not_ready"}` — client retries after 5s |
| Container name collision on `podman run` | podman exits non-zero with "name already in use"; parse stderr and return 409 with `{"error": "name_conflict", "detail": "..."}` |
| Volume path outside `/home/` or `/projects/` | Reject at router validation layer before calling podman, return 400 with `{"error": "invalid_volume_path"}` |
| Image pull needs registry auth | podman uses `~/.config/containers/auth.json` — the backend doesn't manage credentials. If pull fails with auth error, surface the raw podman error message in the SSE job stream so the user can run `podman login` manually in a terminal. |
| `podman stats` unavailable (cgroup issue) | Returns `cpu_pct: null, mem_usage_mb: null` — mini-bars in the UI show "—" instead of values |

---

## 12. Future Enhancements (out of scope for v1)

Note: Podman container management was promoted from this list to v1 scope (see section 4.9).


- **Terminal panel** — xterm.js + WebSocket to a ttyd or wetty process managed by supervisord
- **Pod restart / resize** — call Kubernetes API (needs ServiceAccount with limited RBAC) to delete/recreate pod with different resource requests
- **Persistent service catalog** — store user's added services in a JSON file in `/home/.config/devpod-cc/`
- **Environment variable editor** — view/edit pod env vars (restart required)
- **Git status widget** — `git status --porcelain` across `/projects/*/` directories
- **Network traffic monitor** — `/proc/net/dev` reader for pod-level rx/tx bytes
- **Log aggregation** — unified log view across all supervisord services with filtering
