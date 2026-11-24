"""The ERP boundary.

One protocol, two implementations. `ERPNextClient` talks to a real ERPNext
over its REST API; `FakeERPClient` (in `fake.py`) keeps the same documents in
a dictionary. Everything above this module -- the graph, the rules, the
three-way match, the report -- is written against the protocol and has never
been told which one it is holding.

That is not architectural decoration. It buys three things this project
needs:

1. The test suite and CI run with no ERP, no network and no credentials, in
   under thirty seconds.
2. `--dry-run` is not a flag threaded through a hundred call sites. It is a
   different object.
3. The agent can be developed, and its failures reproduced, while the ERP is
   down -- which, during an implementation, it frequently is.

Every write method returns the document as the server reports it *after* the
write, never the payload that was sent. The distinction is the entire point
of `verify=True`: a 200 response means ERPNext accepted the request, not that
the document says what you believe it says. Server-side defaults, naming
series, currency conversion, tax templates and validation hooks all run
between the two, and a procure-to-pay agent that trusts its own payload will
eventually book a number nobody typed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx

# Doctypes this application is permitted to touch, and how. Anything outside
# this table is refused before a request is built, so a prompt injection in a
# supplier's PDF cannot reach, say, `User` or `Payment Entry` even if it
# persuades the model to ask. See `tools.py` for where this is enforced.
ALLOWED_READ = frozenset({
    "Supplier", "Item", "Warehouse", "Company", "UOM", "Item Price",
    "Material Request", "Purchase Order", "Purchase Receipt", "Purchase Invoice",
})
ALLOWED_WRITE = frozenset({
    "Material Request", "Purchase Order", "Purchase Receipt", "Purchase Invoice",
})

# Server methods this application may call. `run_method` takes a dotted path,
# and Frappe exposes hundreds of whitelisted methods behind `/api/method/` --
# some of which write (`frappe.client.set_value`, `frappe.client.insert`) and
# would walk straight round the doctype allowlist above. Only ERPNext's own
# document mappers are needed, so only they are allowed.
ALLOWED_METHODS = frozenset({
    "erpnext.stock.doctype.material_request.material_request.make_purchase_order",
    "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_receipt",
    "erpnext.buying.doctype.purchase_order.purchase_order.make_purchase_invoice",
})


#: The custom field every document this application creates carries. Created
#: by `seed/seed_erpnext.py` as a unique, read-only, no-copy Data field, so the
#: database itself refuses a second document with the same key.
IDEMPOTENCY_FIELD = "procureflow_key"


def guard_method(dotted_path: str) -> None:
    """Refuse any server method that is not one of the three mappers."""
    if dotted_path not in ALLOWED_METHODS:
        raise ERPError(f"server method {dotted_path!r} may not be called by this application",
                       status=403)


class ERPError(RuntimeError):
    """An ERP operation failed in a way the caller is expected to handle.

    Carries the parsed server message rather than a wall of HTML, because
    this string ends up in a trace row and, eventually, in front of a buyer
    who needs to know what to do about it.
    """

    def __init__(self, message: str, *, status: int | None = None,
                 doctype: str | None = None, payload: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.doctype = doctype
        self.payload = payload


class VerificationError(ERPError):
    """The write succeeded and the document that came back is not what we asked for.

    This is the failure mode the whole project exists to catch, so it gets its
    own type: it must never be swallowed by a generic `except ERPError`.
    """


@dataclass
class Doc:
    """A document as the ERP reports it, not as we hoped it would be."""

    doctype: str
    name: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def docstatus(self) -> int:
        """0 draft, 1 submitted, 2 cancelled. ERPNext's whole lifecycle."""
        return int(self.data.get("docstatus", 0) or 0)

    @property
    def items(self) -> list[dict[str, Any]]:
        return list(self.data.get("items") or [])

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __repr__(self) -> str:                                # pragma: no cover
        return f"<{self.doctype} {self.name} docstatus={self.docstatus}>"


@runtime_checkable
class ERPClient(Protocol):
    """What the agent is allowed to do to an ERP.

    Deliberately small. There is no `request()` escape hatch and no method
    that takes a URL, so neither the model nor a future contributor in a
    hurry can reach a doctype that is not in the allowlist above.
    """

    def get_doc(self, doctype: str, name: str) -> Doc: ...

    def list_docs(self, doctype: str, filters: dict[str, Any] | None = None,
                  fields: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]: ...

    def exists(self, doctype: str, name: str) -> bool: ...

    def insert(self, doctype: str, payload: dict[str, Any], *,
               verify: dict[str, Any] | None = None) -> Doc: ...

    def submit(self, doctype: str, name: str) -> Doc: ...

    def run_method(self, dotted_path: str, **kwargs: Any) -> dict[str, Any]: ...


def _compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Field-by-field read-back comparison, tolerant only where it must be.

    Floats are compared to two decimals because ERPNext stores currency at
    the precision configured for the company, and a quantity of 10 may come
    back as 10.0. Anything else is compared as a string, which is strict on
    purpose: a supplier code that comes back different is exactly the event
    this function exists to notice.
    """
    drift: list[str] = []
    for key, want in expected.items():
        got = actual.get(key)
        if isinstance(want, float) or isinstance(got, float):
            try:
                if round(float(want), 2) != round(float(got), 2):
                    drift.append(f"{key}: asked {want}, stored {got}")
                continue
            except (TypeError, ValueError):
                pass
        if str(want) != str(got):
            drift.append(f"{key}: asked {want!r}, stored {got!r}")
    return drift


class ERPNextClient:
    """ERPNext over REST, with retries, parsed errors and read-back verification.

    Auth is the token scheme: `Authorization: token <api_key>:<api_secret>`.
    The secret is held in memory only and is never logged -- `tracing.py`
    redacts anything that looks like one, and this class never puts
    credentials in a message or a URL.
    """

    def __init__(self, base_url: str, api_key: str, api_secret: str, *,
                 timeout: float = 30.0, max_retries: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = f"token {api_key}:{api_secret}"
        self.max_retries = max_retries
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": self._auth,
                     "Accept": "application/json",
                     "Content-Type": "application/json"},
        )

    # -- plumbing ---------------------------------------------------------

    def _parse_error(self, resp: httpx.Response) -> str:
        """Turn an ERPNext error page into one sentence a human can act on.

        Frappe reports validation failures as a 417 whose body carries a
        JSON-encoded list of HTML strings inside `_server_messages`. Left
        raw, a missing-mandatory-field error reaches the operator as four
        hundred characters of markup, so it is unpacked here instead.
        """
        try:
            body = resp.json()
        except Exception:                                      # noqa: BLE001
            text = (resp.text or "").strip()
            return text[:300] or f"HTTP {resp.status_code} with an empty body"

        msgs = body.get("_server_messages")
        if msgs:
            import json
            import re
            try:
                parts = json.loads(msgs)
            except (TypeError, ValueError):
                parts = [msgs]
            out = []
            for p in parts:
                if isinstance(p, str):
                    try:
                        p = json.loads(p).get("message", p)
                    except (TypeError, ValueError):
                        pass
                # Tags become a space so words either side do not run
                # together, then runs of whitespace collapse -- otherwise
                # "<b>Schedule Date</b> cannot" reads as "Schedule Date
                # cannot" with a doubled gap in front of a human.
                out.append(" ".join(re.sub(r"<[^>]+>", " ", str(p)).split()))
            joined = " | ".join(x for x in out if x)
            if joined:
                return joined[:400]

        for key in ("exception", "message", "exc_type"):
            if body.get(key):
                return str(body[key])[:400]
        return f"HTTP {resp.status_code}"

    def _request(self, method: str, path: str, *, retry: bool = True,
                 **kw: Any) -> dict[str, Any]:
        """One HTTP call, with backoff on the failures that are worth retrying.

        Retried: timeouts, connection errors and 5xx -- a restarting worker
        or a slow background job. Not retried: 4xx. A 417 validation error
        means the document is wrong, and sending it twice more only puts
        three identical complaints in the log.

        **A create is never retried here** (`retry=False`). A timeout on a
        POST does not mean the document was not created: the server may
        have committed it and lost the response. Re-sending it blind is how
        an integration books the same Purchase Order twice. The retry for a
        create lives in `tools.insert_exactly_once`, which first looks the
        document up by its idempotency key and only re-sends if it is not
        there.
        """
        last: Exception | None = None
        attempts = self.max_retries if retry else 1
        for attempt in range(attempts):
            try:
                resp = self._client.request(method, path, **kw)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last = e
                if attempt == attempts - 1:
                    break
                time.sleep(2 ** attempt * 0.5)
                continue

            if resp.status_code < 400:
                try:
                    return resp.json()
                except Exception:                              # noqa: BLE001
                    return {}

            if resp.status_code >= 500 and attempt < attempts - 1:
                time.sleep(2 ** attempt * 0.5)
                continue

            raise ERPError(self._parse_error(resp), status=resp.status_code)

        raise ERPError(f"{type(last).__name__} after {attempts} attempt(s): {last}")

    @staticmethod
    def _guard(doctype: str, write: bool) -> None:
        pool = ALLOWED_WRITE if write else ALLOWED_READ
        if doctype not in pool:
            verb = "written" if write else "read"
            raise ERPError(f"{doctype!r} may not be {verb} by this application")

    # -- reads ------------------------------------------------------------

    def get_doc(self, doctype: str, name: str) -> Doc:
        self._guard(doctype, write=False)
        body = self._request("GET", f"/api/resource/{doctype}/{name}")
        return Doc(doctype, name, body.get("data") or {})

    def list_docs(self, doctype: str, filters: dict[str, Any] | None = None,
                  fields: list[str] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        self._guard(doctype, write=False)
        import json
        params: dict[str, Any] = {"limit_page_length": limit}
        if filters:
            params["filters"] = json.dumps([[k, "=", v] for k, v in filters.items()])
        if fields:
            params["fields"] = json.dumps(fields)
        body = self._request("GET", f"/api/resource/{doctype}", params=params)
        return list(body.get("data") or [])

    def exists(self, doctype: str, name: str) -> bool:
        try:
            self.get_doc(doctype, name)
            return True
        except ERPError as e:
            if e.status == 404:
                return False
            raise

    # -- writes -----------------------------------------------------------

    def insert(self, doctype: str, payload: dict[str, Any], *,
               verify: dict[str, Any] | None = None) -> Doc:
        """Create a document, then read it back and prove it says what we asked.

        `verify` names the fields that must survive the round trip. They are
        re-read from the server rather than taken from the insert response,
        because the insert response is generated in the same request and can
        reflect the submitted payload more faithfully than the stored row.
        """
        self._guard(doctype, write=True)
        body = self._request("POST", f"/api/resource/{doctype}", json=payload, retry=False)
        data = body.get("data") or {}
        name = data.get("name")
        if not name:
            raise ERPError(f"{doctype} was created but the server returned no name",
                           doctype=doctype, payload=payload)

        stored = self.get_doc(doctype, name)
        if verify:
            drift = _compare(verify, stored.data)
            if drift:
                raise VerificationError(
                    f"{doctype} {name} was stored differently from what was sent: "
                    + "; ".join(drift), doctype=doctype, payload=payload)
        return stored

    def submit(self, doctype: str, name: str) -> Doc:
        """Submit, then confirm docstatus is actually 1.

        A submit that returns 200 and leaves the document in draft is rare
        and extremely expensive to discover later, because every downstream
        document silently refuses to link to it.
        """
        self._guard(doctype, write=True)
        self._request("PUT", f"/api/resource/{doctype}/{name}", json={"docstatus": 1})
        stored = self.get_doc(doctype, name)
        if stored.docstatus != 1:
            raise VerificationError(
                f"{doctype} {name} did not submit: docstatus is {stored.docstatus}",
                doctype=doctype)
        return stored

    def run_method(self, dotted_path: str, **kwargs: Any) -> dict[str, Any]:
        """Call a whitelisted server method, e.g. the MR-to-PO mapper.

        These dotted paths move between ERPNext versions, so every caller is
        expected to verify the path exists on the installed version and fall
        back to building the document itself. See `tools.py::make_mapped_doc`.
        The path is checked against `ALLOWED_METHODS` before a request is built.
        """
        guard_method(dotted_path)
        body = self._request("POST", f"/api/method/{dotted_path}", json=kwargs)
        return body.get("message") if isinstance(body.get("message"), dict) else body

    def close(self) -> None:
        self._client.close()
