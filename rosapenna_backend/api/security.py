"""Shared API-key and per-client rate-limit dependencies."""

import hmac
import ipaddress
import threading
import time
from collections import defaultdict, deque
from typing import Dict, Optional

from fastapi import Depends, Header, HTTPException, Request

from rosapenna_backend.config import settings

_rate_buckets: Dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()


def verify_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    if not settings.api_key:
        raise HTTPException(status_code=503, detail="API authentication is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")

def rate_limit(request: Request):
    peer_ip = request.client.host if request.client else "unknown"
    client_ip = peer_ip
    try:
        peer = ipaddress.ip_address(peer_ip)
        trusted = any(peer in ipaddress.ip_network(cidr.strip(), strict=False)
                      for cidr in settings.trusted_proxy_cidrs.split(",") if cidr.strip())
        forwarded = request.headers.get("x-forwarded-for", "")
        if trusted and forwarded:
            chain = [part.strip() for part in forwarded.split(",")]
            chain.append(peer_ip)
            while len(chain) > 1:
                candidate = chain.pop()
                try:
                    candidate_ip = ipaddress.ip_address(candidate)
                except ValueError:
                    client_ip = peer_ip
                    break
                if not any(candidate_ip in ipaddress.ip_network(cidr.strip(), strict=False)
                           for cidr in settings.trusted_proxy_cidrs.split(",") if cidr.strip()):
                    client_ip = str(candidate_ip)
                    break
                client_ip = str(candidate_ip)
    except ValueError:
        client_ip = peer_ip
    now = time.monotonic()
    window = 60.0
    with _rate_lock:
        bucket = _rate_buckets[client_ip]
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= settings.rate_limit_per_minute:
            raise HTTPException(status_code=429, detail="Rate limit exceeded, slow down")
        bucket.append(now)

COMMON_DEPS = [Depends(rate_limit), Depends(verify_api_key)]
