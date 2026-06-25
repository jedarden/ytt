# Spike: FastMCP path-inserted + path-bearing OAuth metadata (ytt-7dk)

## Question

Can FastMCP emit `resource`/`issuer` = `.../ytt` at path-inserted `/.well-known/oauth-*/ytt` endpoints, or are custom Starlette routes required?

## Answer

**FastMCP 3.4.2 DOES emit path-bearing identifiers natively**, but a **custom `get_routes()` override is required** to mount the path-inserted AS metadata routes.

## Findings

### 1. Native FastMCP Support (Path-Bearing Identifiers)

FastMCP 3.4.2 correctly handles path-bearing URLs when configured:

```python
provider = OAuthProvider(
    base_url='https://mcp.ardenone.com/ytt',  # path-bearing
    resource_base_url='https://mcp.ardenone.com',  # origin only
)
```

**Native behavior confirmed:**
- `_get_resource_url('/ytt')` → `https://mcp.ardenone.com/ytt` ✓
- `get_well_known_routes()` computes path-inserted routes ✓
- Metadata documents contain correct path-bearing `issuer`/`resource` ✓

### 2. The Gap: Path-Inserted Routes Not Mounted

**Problem**: `create_streamable_http_app` calls `get_routes()`, NOT `get_well_known_routes()`.

FastMCP's native `get_routes(mcp_path='/ytt')` returns:
```python
[
  '/.well-known/oauth-authorization-server',  # standard route only
  '/authorize',
  '/token',
  '/.well-known/oauth-protected-resource/ytt',  # PRM is path-inserted
]
```

The path-inserted AS metadata route (`/.well-known/oauth-authorization-server/ytt`) is computed by `get_well_known_routes()` but NOT included in `get_routes()`, so it would NOT be mounted in the ASGI app.

### 3. Solution: Custom `get_routes()` Override

The `YttOAuthProvider.get_routes()` override merges the path-inserted well-known routes:

```python
def get_routes(self, mcp_path: str | None = None) -> list:
    base_routes = super().get_routes(mcp_path)
    # Replicate OAuthProvider.get_well_known_routes path-insertion logic
    # ... add /.well-known/oauth-authorization-server/ytt
    # ... add /.well-known/openid-configuration/ytt
    return base_routes
```

This ensures all path-inserted routes are actually mounted.

### 4. Verified Endpoints

**AS Metadata (RFC 8414)**:
- `GET /.well-known/oauth-authorization-server/ytt` → 200
- `issuer`: `https://mcp.ardenone.com/ytt` ✓
- `authorization_endpoint`: `https://mcp.ardenone.com/ytt/authorize` ✓
- `token_endpoint`: `https://mcp.ardenone.com/ytt/token` ✓
- `code_challenge_methods_supported`: `["S256"]` ✓

**Protected Resource Metadata (RFC 9728)**:
- `GET /.well-known/oauth-protected-resource/ytt` → 200
- `resource`: `https://mcp.ardenone.com/ytt` ✓
- `authorization_servers`: `["https://mcp.ardenone.com/ytt"]` ✓

**HTTP 401 Challenge**:
- `POST /ytt` (no token) → 401
- `WWW-Authenticate: Bearer ...` header present ✓

## Conclusion

✅ **FastMCP 3.4.2 DOES support path-bearing OAuth metadata natively**
✅ **Path-inserted well-known routes work correctly**
✅ **Custom `get_routes()` override is required** (gap in FastMCP's mounting logic)
✅ **Implementation in `auth.py` is correct and complete**
✅ **All Phase 5 auth tests pass**

**No additional work needed** — the current implementation handles path-inserted metadata correctly via the `YttOAuthProvider.get_routes()` override.

## Tested Against

- FastMCP version: 3.4.2
- Test suite: `tests/unit/test_auth.py` (all 16 tests pass)
- Endpoints verified: AS metadata, PRM, 401 WWW-Authenticate header

## Related

- Phase 5 spike item (a): Static client with two redirect URIs ✓
- Phase 5 spike item (b): DCR disabled ✓
- ADR-001: FastMCP self-issued tokens, audience-bound to path-bearing URL
- RFC 8414 §3.1: Path-inserted AS metadata
- RFC 9728 §3.1: Path-inserted PRM
