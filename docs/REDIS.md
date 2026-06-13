# Redis

Redis runs as a lightweight sidecar container (`redis:7-alpine`) on the backend Docker network. It is used by **two separate components**: GTAIF and Connexions.

---

## Docker Compose

```yaml
redis:
  image: redis:7-alpine
  container_name: getraind-redis
  hostname: redis
  restart: unless-stopped
  command: >
    redis-server
      --maxmemory 512mb
      --maxmemory-policy allkeys-lru
      --save ""
      --appendonly no
  networks:
    - backend
  healthcheck:
    test: ["CMD", "redis-cli", "ping"]
    interval: 10s
    timeout: 3s
    retries: 5
```

**Key configuration decisions:**

| Setting | Value | Reason |
|---|---|---|
| `maxmemory` | 512MB | Hard cap — prevents Redis from starving other services |
| `maxmemory-policy` | `allkeys-lru` | When full, evict the least-recently-used key. No data stored here is irreplaceable. |
| `--save ""` | (persistence off) | All data is ephemeral by design (tokens, rate counters, session registry). Persistence would waste I/O with no benefit. |
| `--appendonly no` | (AOF off) | Same reason — no need to survive restarts. |

The container is **not exposed to the host** (`ports:` is intentionally absent). Only services on the `backend` Docker network can reach it.

---

## GTAIF — `utils/redis_client.py`

`GTAIF/utils/redis_client.py` wraps `redis.asyncio` (the async driver). One `RedisClient` instance is created at startup and stored on `app.state.redis`. If Redis is unavailable at startup, `app.state.redis = None` and the warning `gtaif.redis_unavailable` is logged. **GTAIF continues to start and function without Redis** — all three uses degrade gracefully.

### 1. WebSocket Token Single-Use Enforcement

**Where**: `utils/auth.py` → `validate_ws_token()`  
**Key namespace**: `gtaif:ws_token:used:<hmac_sig>`

WebSocket tokens are short-lived HMAC-signed strings (`username:expiry`) issued by the Portal. Without Redis, a valid token could be extracted and replayed to open a second WebSocket connection impersonating the same user.

`consume_ws_token()` uses atomic `SET NX EX` (set-if-not-exists with TTL) to mark the HMAC signature as spent:

```python
key    = f"gtaif:ws_token:used:{sig}"
result = await client.set(key, "1", nx=True, ex=remaining_ttl)
# result = True  → first time seen (OK)
# result = None  → already in Redis (replay attack)
```

The TTL equals the token's remaining validity window, so the key self-cleans when the token would have expired anyway.

**Without Redis**: Tokens are validated by HMAC and expiry only. A captured token could be replayed until it expires.

### 2. Per-User Chat Rate Limiting

**Where**: `routers/ws.py` → WebSocket `chat` message handler  
**Key namespace**: `gtaif:rate:chat:<username>`  
**Defaults**: 30 messages per 60-second window

```python
allowed, count = await redis.check_rate_limit(username)
if not allowed:
    await websocket.send_json({
        "type":    "error",
        "message": "Rate limit exceeded. Please wait a moment.",
        "code":    "rate_limited",
    })
    continue
```

The implementation uses a pipeline to `INCR` the counter and read the TTL atomically, then sets a 60-second TTL on the first increment. The window starts from the user's first message in that window, not a fixed clock boundary.

```python
pipe.incr(key)
pipe.ttl(key)
count, ttl = await pipe.execute()
if ttl < 0:
    await client.expire(key, window)   # set TTL on first hit
```

**Without Redis**: Rate limiting is disabled — all messages are allowed through.

### 3. WebSocket Session Registry

**Where**: `routers/ws.py` — on connect and disconnect  
**Key namespace**: `gtaif:ws:session:<session_id>`  
**TTL**: 24 hours (self-cleaning for stale entries)

```python
# On connection:
await redis.register_ws_session(session_id, username)

# On disconnect (finally block — always runs):
await redis.unregister_ws_session(session_id)
```

Each key stores the `username` that owns the session. The purpose is **cross-worker routing**: when multiple GTAIF instances run behind nginx, an event targeting `session_id` needs to find which worker holds that WebSocket connection. This routing is **not yet implemented** (GTAIF currently runs as a single worker), but the session registry is the foundation.

`get_ws_session_username(session_id)` performs the reverse lookup for use by future pub/sub routing code.

**Without Redis**: Session registry is disabled — no impact until multi-worker pub/sub is implemented.

---

## Key Namespace Summary

| Key | Value | TTL | Purpose |
|---|---|---|---|
| `gtaif:ws_token:used:<sig>` | `"1"` | Token remaining lifetime | Spent WS auth token — replay prevention |
| `gtaif:rate:chat:<username>` | Request count (int) | 60s window | Per-user chat message rate counter |
| `gtaif:ws:session:<session_id>` | `username` (str) | 86400s | Live session → owner mapping |

---

## Connexions — `utils/red.py`

The **Connexions** service (`connexions/`) uses a separate, older Redis integration via the synchronous `redis` driver with RedisJSON. It stores connection documents (connector metadata) as JSON blobs keyed by connection ID.

```python
# Key pattern: id:<connection_id>
client.json().set('id:{}'.format(id), Path.root_path(), document)
client.json().get('id:{}'.format(id))
client.scan_iter('id:*')   # list all connections
```

This is independent of GTAIF's key namespace (`gtaif:*`). Both components share the same Redis instance but do not interfere because of the different key prefixes.

> **Note**: The Connexions `RED` class uses the synchronous driver on the same `redis:6379` endpoint. If Connexions grows in scale, it should be migrated to `redis.asyncio` to avoid blocking the event loop.

---

## Graceful Degradation Summary

| Redis unavailable | Impact |
|---|---|
| WS token replay protection | Disabled — valid tokens accepted without replay check |
| Rate limiting | Disabled — all chat messages allowed |
| Session registry | Disabled — no effect until multi-worker routing lands |
| Connexions JSON store | Connection lookup fails; errors logged |

GTAIF never crashes on Redis failure. The startup log emits `gtaif.redis_unavailable` with the error detail so it's visible without breaking the service.

---

## Configuration

| Env var | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://redis:6379` | Full Redis URL including auth if needed |

Set in `compose/docker-compose.yml` under GTAIF's environment block. Change to add a password: `redis://:password@redis:6379`.
