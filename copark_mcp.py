"""Unofficial MCP server and CLI for CoPark guest parking permits.

CoPark (copark.dk) runs on Giant Leap's "Parko" platform. This talks to the same
JSON API as the CoPark web portal (https://copark-permit.giantleap.no), so anything
you can do there for guest permits, you can do from an MCP client or a cron job.

Configuration lives outside the code: run `copark-mcp setup` once on the machine
that will run it. It asks for your login and writes ~/.config/copark-mcp/env (mode 600),
which every command reads on start. Plain environment variables work too and take precedence.

Settings:
  COPARK_USERNAME          your CoPark login (the mobile number you use on the web portal)
  COPARK_PASSWORD          your CoPark password
  COPARK_GUEST_PRODUCT_ID  optional; which product to order. If unset, the guest permit
                           product on your account is found automatically
  COPARK_GUEST_MATCH       optional; regex for recognising guest products by name
                           (default: "g(æ|ae)st", case-insensitive)
  COPARK_MONTHLY_LIMIT     optional; guest permits allowed per month by your association,
                           so usage can show how many are left
  COPARK_ALLOW_PAID        optional; set to 1 to allow ordering products that cost money
  COPARK_TOKEN_FILE        optional; session token cache (default ~/.config/copark-mcp/token)
  COPARK_MCP_HOST          optional; bind address for --http (default 127.0.0.1)
  COPARK_MCP_PATH          optional; URL path for --http (default /mcp)
  COPARK_MCP_ALLOWED_HOSTS optional; public hostname(s) the --http server is reached by, comma-
                           separated. Requests for other hosts are rejected. Unset: no host check
  COPARK_CONFIG_DIR        optional; where env + token live (default ~/.config/copark-mcp)

Usage:
  copark-mcp setup              first-time setup: credentials, limit, test login
  copark-mcp                    MCP over stdio
  copark-mcp --http [port]      MCP over streamable HTTP (default port 8766)
  copark-mcp order <plate>      order a guest permit now
  copark-mcp list               active permits
  copark-mcp history [n]        last n guest permits (default 20)
  copark-mcp usage [months]     guest permits per month
  copark-mcp products           permit products on your account
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import string
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = "https://copark-permit.giantleap.no/api/"
LOCALE = "da_DK_copark"
TZ = ZoneInfo("Europe/Copenhagen")
PAGE_SIZE = 40
CONFIG_DIR = Path(os.environ.get("COPARK_CONFIG_DIR") or Path.home() / ".config" / "copark-mcp")
ENV_FILE = CONFIG_DIR / "env"


def load_env_file(path: Path = ENV_FILE) -> None:
    """Read KEY=VALUE lines from the local config file. Real env vars take precedence."""
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)

logging.getLogger("httpx").setLevel(logging.WARNING)


class CoParkError(RuntimeError):
    pass


def _guest_re() -> re.Pattern:
    return re.compile(os.environ.get("COPARK_GUEST_MATCH", r"g(æ|ae)st"), re.IGNORECASE)


def is_guest(name: str | None) -> bool:
    return bool(_guest_re().search(name or ""))


def normalize_plate(plate: str) -> str:
    p = "".join(ch for ch in plate.upper() if ch.isalnum())
    if not 2 <= len(p) <= 8:
        raise CoParkError(f"'{plate}' doesn't look like a number plate.")
    return p


def _agreement(a: dict) -> dict:
    name = a.get("productName") or ""
    return {
        "id": a.get("id"),
        "product": name,
        "area": a.get("parkingRightName"),
        "plates": a.get("plateNumbers"),
        "valid_from": a.get("validFromTime"),
        "valid_to": a.get("validToTime"),
        "note": a.get("noteFromUser") or a.get("note"),
        "is_guest_permit": is_guest(name),
    }


class CoParkClient:
    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        token_file: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.username = username or os.environ.get("COPARK_USERNAME")
        self.password = password or os.environ.get("COPARK_PASSWORD")
        self.token_file = Path(
            token_file
            or os.environ.get("COPARK_TOKEN_FILE")
            or CONFIG_DIR / "token"
        )
        self.http = httpx.Client(
            base_url=BASE_URL,
            timeout=20,
            transport=transport,
            headers={
                "X-GLTLocale": LOCALE,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        self._token: str | None = None
        self._guest_product_id: str | None = os.environ.get("COPARK_GUEST_PRODUCT_ID") or None

    # ---------- auth ----------

    def _load_token(self) -> str | None:
        if not self._token:
            try:
                self._token = self.token_file.read_text().strip() or None
            except FileNotFoundError:
                pass
        return self._token

    def _save_token(self, token: str) -> None:
        self._token = token
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(token)
        os.chmod(self.token_file, 0o600)

    def login(self) -> None:
        if not (self.username and self.password):
            raise CoParkError("Set COPARK_USERNAME and COPARK_PASSWORD to log in.")
        r = self.http.post(
            "public/user/authenticate.json",
            json={"userName": self.username, "password": self.password},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("requiresSmsAuthentication") or data.get("requiresSmsAuthenticationSetup"):
            raise CoParkError("CoPark requires SMS verification for this account; password login isn't enough.")
        token = data.get("token")
        if data.get("resultCode") != "SUCCESS" or not token:
            raise CoParkError(f"Login failed: {data.get('errorCode')} {data.get('errorMessage')}")
        self._save_token(token)

    @staticmethod
    def _is_auth_failure(r: httpx.Response) -> bool:
        if r.status_code in (401, 403):
            return True
        try:
            code = str(r.json().get("errorCode") or "")
        except Exception:
            return False
        return any(k in code.upper() for k in ("TOKEN", "AUTH", "SESSION", "LOGIN"))

    def _request(self, method: str, path: str, *, params=None, json_body=None, headers=None, retry=True) -> dict:
        if not self._load_token():
            self.login()
        h = {"X-Token": self._token, **(headers or {})}
        r = self.http.request(method, path, params=params, json=json_body, headers=h)
        if self._is_auth_failure(r) and retry:
            self.login()
            return self._request(method, path, params=params, json_body=json_body, headers=headers, retry=False)
        r.raise_for_status()
        data = r.json() if r.content else {}
        if data.get("resultCode") not in (None, "SUCCESS"):
            raise CoParkError(f"{path}: {data.get('errorCode')} {data.get('errorMessage')}")
        return data

    # ---------- products ----------

    def products(self) -> list[dict]:
        """Permit products available to this account, across all its parking areas."""
        out = []
        facilities = self._request("GET", "public/user/product/permission/facilities.json").get("facilities") or []
        for f in facilities:
            prods = self._request("GET", f"public/user/product/permission/facility/{f['id']}.json").get("products") or []
            for p in prods:
                out.append({
                    "id": p["id"],
                    "name": p.get("name"),
                    "area": p.get("parkingRightName"),
                    "facility": f.get("name"),
                    "price_dkk": (p.get("priceCents") or 0) / 100,
                    "description": p.get("description"),
                    "is_guest_permit": is_guest(p.get("name")),
                })
        return out

    def guest_product_id(self) -> str:
        if self._guest_product_id:
            return self._guest_product_id
        guests = [p for p in self.products() if p["is_guest_permit"]]
        if len(guests) == 1:
            self._guest_product_id = guests[0]["id"]
            return self._guest_product_id
        if not guests:
            raise CoParkError("No guest permit product found on this account. Run `copark-mcp products` "
                              "and set COPARK_GUEST_PRODUCT_ID.")
        listing = "; ".join(f"{p['id']} ({p['name']})" for p in guests)
        raise CoParkError(f"Several guest products found, set COPARK_GUEST_PRODUCT_ID to one of: {listing}")

    # ---------- permits ----------

    def lookup_plate(self, plate: str) -> str | None:
        return self._request("GET", f"public/user/platenumber/lookup/{normalize_plate(plate)}.json").get("message")

    def order_guest_permit(self, plate: str, product_id: str | None = None) -> dict:
        product_id = product_id or self.guest_product_id()
        plate = normalize_plate(plate)
        start = datetime.now(TZ).strftime("%Y-%m-%d 00:00")

        quote = self._request(
            "POST",
            "public/user/product/permission/request-order.json",
            json_body={"productId": product_id, "startTime": start},
        )
        price = quote.get("priceCents") or 0
        if price and os.environ.get("COPARK_ALLOW_PAID") != "1":
            raise CoParkError(f"Not ordering: this product costs {price / 100:.2f} kr. Set COPARK_ALLOW_PAID=1 to allow it.")

        idempotency_key = "ID-" + "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(12))
        order = self._request(
            "POST",
            "user/product/permission/place_order.action",
            headers={"X-GLT-IDEMPOTENCY-KEY": idempotency_key},
            json_body={
                "acceptedTermsAndConditionsId": None,
                "firstTermPriceCents": quote.get("firstTermPriceCents") or 0,
                "plateNumbers": [plate],
                "priceCents": price,
                "productId": product_id,
                "qualificationForm": [],
                "startTime": f"{start}:00",
            },
        )
        return {
            "order_id": order.get("id"),
            "status": order.get("status"),
            "product": order.get("productName"),
            "plates": order.get("plateNumbers"),
            "valid_from": order.get("fromTime"),
            "valid_to": order.get("toTimeInclusive"),
            "price_dkk": (order.get("priceCents") or 0) / 100,
        }

    def active_permits(self, guest_only: bool = False) -> list[dict]:
        data = self._request("GET", "user/purchase/activehistory.json")
        items = [_agreement(a) for a in data.get("activeParkingAgreements") or []]
        return [i for i in items if i["is_guest_permit"]] if guest_only else items

    def history(self, guest_only: bool = True, max_items: int = 1000) -> list[dict]:
        """Past and current agreements, newest first."""
        out: list[dict] = []
        page = 0
        while len(out) < max_items:
            data = self._request("GET", "user/agreement/report.json",
                                 params={"pageIndex": page, "pageSize": PAGE_SIZE})
            batch = data.get("parkingAgreements") or []
            for a in batch:
                item = _agreement(a)
                if not guest_only or item["is_guest_permit"]:
                    out.append(item)
            page += 1
            if len(batch) < PAGE_SIZE or page * PAGE_SIZE >= (data.get("totalParkingAgreementCount") or 0):
                break
        return out[:max_items]

    def monthly_usage(self, months: int = 12) -> dict:
        """Guest permits per calendar month (by start time), newest month first."""
        now = datetime.now(TZ)
        keys, y, m = [], now.year, now.month
        for _ in range(max(1, months)):
            keys.append(f"{y:04d}-{m:02d}")
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        counts = {k: 0 for k in keys}
        plates: dict[str, int] = {}
        for p in self.history(guest_only=True):
            k = (p.get("valid_from") or "")[:7]
            if k in counts:
                counts[k] += 1
                for pl in p.get("plates") or []:
                    plates[pl] = plates.get(pl, 0) + 1
        result = {
            "this_month": counts[keys[0]],
            "per_month": counts,
            "plates": dict(sorted(plates.items(), key=lambda kv: -kv[1])),
        }
        limit = os.environ.get("COPARK_MONTHLY_LIMIT", "")
        if limit.isdigit():
            result["monthly_limit"] = int(limit)
            result["remaining_this_month"] = max(0, int(limit) - result["this_month"])
        return result


# ---------- MCP ----------

mcp = FastMCP("copark")
_client: CoParkClient | None = None


def client() -> CoParkClient:
    global _client
    if _client is None:
        _client = CoParkClient()
    return _client


@mcp.tool()
def order_guest_permit(plate: str) -> dict:
    """Order a CoPark guest permit for a number plate, starting now.

    CoPark only sells guest permits from the current moment (no future start dates),
    so call this on the day the guest arrives. The duration is set by the parking area.
    Refuses paid products unless COPARK_ALLOW_PAID=1.
    """
    return client().order_guest_permit(plate)


@mcp.tool()
def list_permits(guest_only: bool = True) -> list[dict]:
    """List currently active CoPark permits (guest permits only by default)."""
    return client().active_permits(guest_only=guest_only)


@mcp.tool()
def permit_history(limit: int = 20) -> list[dict]:
    """Past guest permits, newest first, with plate and validity period."""
    return client().history(guest_only=True, max_items=limit)


@mcp.tool()
def guest_permit_usage(months: int = 12) -> dict:
    """Guest permits used per month and per plate.

    If COPARK_MONTHLY_LIMIT is set, also shows how many are left this month.
    """
    return client().monthly_usage(months)


@mcp.tool()
def lookup_plate(plate: str) -> str:
    """Look up a Danish number plate; returns the car's description, e.g. colour, make and model."""
    return client().lookup_plate(plate) or "No match"


@mcp.tool()
def list_products() -> list[dict]:
    """List the permit products on this account, including which ones are guest permits."""
    return client().products()


# ---------- CLI ----------

def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _transport_security():
    """Host-header checking for --http.

    The MCP SDK only accepts Host: localhost by default when bound to 127.0.0.1, which breaks
    reverse proxies and tunnels. COPARK_MCP_ALLOWED_HOSTS (comma-separated public hostnames)
    keeps the check on and allows those; if unset, the check is off.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [h.strip() for h in os.environ.get("COPARK_MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*hosts, *(f"{h}:*" for h in hosts), "127.0.0.1:*", "localhost:*"],
        allowed_origins=[*(f"https://{h}" for h in hosts), "http://127.0.0.1:*", "http://localhost:*"],
    )


def setup() -> None:
    """Interactive first-time setup: asks for credentials and writes them to ENV_FILE (mode 600)."""
    import getpass

    print(f"CoPark MCP setup. Settings are saved to {ENV_FILE} on this machine only.\n")
    current = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                current[k.strip()] = v.strip()

    def ask(key: str, prompt: str, secret: bool = False, default: str = "") -> str:
        default = current.get(key) or default
        shown = "keep current" if (secret and default) else default
        suffix = f" [{shown}]" if shown else ""
        value = (getpass.getpass if secret else input)(f"{prompt}{suffix}: ").strip()
        return value or default

    values = {
        "COPARK_USERNAME": ask("COPARK_USERNAME", "CoPark login (mobile number; 8 digits for Danish numbers)"),
        "COPARK_PASSWORD": ask("COPARK_PASSWORD", "CoPark password", secret=True),
        "COPARK_MONTHLY_LIMIT": ask("COPARK_MONTHLY_LIMIT", "Guest permits allowed per month (blank if none)"),
        "COPARK_MCP_ALLOWED_HOSTS": ask("COPARK_MCP_ALLOWED_HOSTS",
                                        "Public hostname for --http, e.g. myhost.example.ts.net (blank if none)"),
        "COPARK_GUEST_PRODUCT_ID": current.get("COPARK_GUEST_PRODUCT_ID", ""),
        "COPARK_MCP_PATH": current.get("COPARK_MCP_PATH") or f"/mcp-{secrets.token_hex(16)}",
    }

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    ENV_FILE.touch(mode=0o600, exist_ok=True)
    os.chmod(ENV_FILE, 0o600)
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    for k, v in values.items():
        if v:
            os.environ[k] = v

    print("\nTesting login...")
    cl = CoParkClient()
    cl.login()
    try:
        product = cl.guest_product_id()
        print(f"Logged in. Guest permit product: {product}")
    except CoParkError as e:
        print(f"Logged in, but: {e}")
    print(f"HTTP endpoint path (keep it secret if you expose the server): {values['COPARK_MCP_PATH']}")


def main(argv: list[str]) -> None:
    load_env_file()
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if cmd == "setup":
            setup()
        elif cmd == "order" and len(args) == 1:
            _print(client().order_guest_permit(args[0]))
        elif cmd == "list":
            _print(client().active_permits())
        elif cmd == "history":
            _print(client().history(max_items=int(args[0]) if args else 20))
        elif cmd == "usage":
            _print(client().monthly_usage(int(args[0]) if args else 12))
        elif cmd == "products":
            _print(client().products())
        elif cmd == "--http":
            mcp.settings.host = os.environ.get("COPARK_MCP_HOST", "127.0.0.1")
            mcp.settings.port = int(args[0]) if args else 8766
            mcp.settings.streamable_http_path = os.environ.get("COPARK_MCP_PATH", "/mcp")
            mcp.settings.transport_security = _transport_security()
            mcp.run(transport="streamable-http")
        elif cmd in ("-h", "--help", "help"):
            print(__doc__)
        elif cmd == "":
            mcp.run()
        else:
            print(__doc__, file=sys.stderr)
            sys.exit(2)
    except CoParkError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


def _entry() -> None:
    main(sys.argv[1:])


if __name__ == "__main__":
    _entry()
