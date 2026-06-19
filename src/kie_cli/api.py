"""KIE API client — envelope unwrap, error mapping, polling, upload, pricing."""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

# ── Constants ────────────────────────────────────────────────────────────────

API_BASE = "https://api.kie.ai"
UPLOAD_BASE = "https://kieai.redpandaai.co"
UPLOAD_THRESHOLD = 10 * 1024 * 1024  # 10 MB: base64 below, multipart above
TIMEOUT = 60.0  # seconds


# ── Exception ────────────────────────────────────────────────────────────────

class KieError(Exception):
    """Typed error that maps directly to a CLI exit code."""

    def __init__(
        self,
        code: str,           # snake_case, matches JSON `error` field
        message: str,
        hint: str | None = None,
        exit_code: int = 1,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.http_status = http_status


# ── Error mapping helper ─────────────────────────────────────────────────────

def _map_envelope_error(code: int, msg: str, context: str = "") -> KieError:
    """Map API response code to KieError per SPEC §7 / §10."""
    if code == 401:
        return KieError("auth_invalid", msg or "Invalid API key", exit_code=4, http_status=code)
    if code == 402:
        return KieError(
            "insufficient_credits", msg or "Insufficient credits",
            hint="kie balance --json", exit_code=7, http_status=code,
        )
    if code == 404 or (code == 422 and "recordInfo" in (msg or "")):
        return KieError("task_not_found", msg or "Task not found", exit_code=3, http_status=code)
    if code == 422:
        return KieError("invalid_params", msg or "Invalid parameters", exit_code=2, http_status=code)
    if code == 429:
        return KieError("rate_limited", msg or "Rate limited", exit_code=5, http_status=code)
    if code in (455, 500, 501, 505) or code >= 500:
        return KieError("upstream_error", msg or "Upstream error", exit_code=5, http_status=code)
    return KieError("upstream_error", msg or f"API error {code}", exit_code=5, http_status=code)


# ── Record normalisation ─────────────────────────────────────────────────────

def _normalize_record(rec: dict) -> dict:
    """Parse JSON-string fields in a recordInfo record in place, return it."""
    # param: JSON string → dict
    if isinstance(rec.get("param"), str) and rec["param"]:
        try:
            rec["param"] = json.loads(rec["param"])
        except json.JSONDecodeError:
            pass

    # resultJson: JSON string → dict; extract resultUrls as list
    result_json = rec.get("resultJson")
    if isinstance(result_json, str) and result_json:
        try:
            parsed = json.loads(result_json)
            rec["resultJson"] = parsed
            rec["resultUrls"] = parsed.get("resultUrls", [])
        except json.JSONDecodeError:
            rec["resultUrls"] = []
    else:
        rec["resultUrls"] = []

    return rec


# ── Client ───────────────────────────────────────────────────────────────────

class Client:
    def __init__(self, api_key: str | None = None, base: str | None = None) -> None:
        # Key stored but NOT required here — checked at request time (exit 4 only on API calls).
        self._key = api_key or os.environ.get("KIE_API_KEY", "")
        self._base = (base or os.environ.get("KIE_API_BASE", API_BASE)).rstrip("/")
        self._upload_base = os.environ.get("KIE_UPLOAD_BASE", UPLOAD_BASE).rstrip("/")
        # If recordInfo 404s, we fall back once and remember the working path.
        self._task_path = "/api/v1/jobs/recordInfo"

    # ── Internal ──────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        if not self._key:
            raise KieError(
                "auth_missing", "KIE_API_KEY is not set",
                hint="export KIE_API_KEY=...", exit_code=4,
            )
        return {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict | None = None,
        base: str | None = None,
    ) -> Any:
        """Make a request and return unwrapped `data`. Raises KieError on non-200."""
        url = (base or self._base).rstrip("/") + path
        try:
            resp = httpx.request(
                method,
                url,
                headers=self._headers(),
                json=json,
                params=params,
                timeout=TIMEOUT,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise KieError("network_error", str(exc), exit_code=5) from exc

        try:
            body = resp.json()
        except Exception as exc:
            raise KieError(
                "upstream_error",
                f"Non-JSON response (HTTP {resp.status_code})",
                exit_code=5,
                http_status=resp.status_code,
            ) from exc

        api_code = body.get("code", resp.status_code)
        if api_code == 200:
            return body.get("data")

        raise _map_envelope_error(api_code, body.get("msg", ""), context=str(body))

    # ── Public API ────────────────────────────────────────────────────────────

    def create_task(self, model: str, input: dict, callback: str | None = None) -> str:
        """POST /api/v1/jobs/createTask → taskId string."""
        payload: dict = {"model": model, "input": input}
        if callback:
            payload["callBackUrl"] = callback
        data = self.request("POST", "/api/v1/jobs/createTask", json=payload)
        return data["taskId"]

    def get_task(self, task_id: str) -> dict:
        """GET recordInfo; fall back once to getTaskDetail on 404/422 + remember path."""
        try:
            data = self.request(
                "GET", self._task_path, params={"taskId": task_id}
            )
        except KieError as err:
            if err.exit_code == 3 and self._task_path != "/api/v1/jobs/getTaskDetail":
                # One-time fallback: try the alternate path
                data = self.request(
                    "GET", "/api/v1/jobs/getTaskDetail", params={"taskId": task_id}
                )
                self._task_path = "/api/v1/jobs/getTaskDetail"  # remember for future calls
            else:
                raise
        return _normalize_record(data)

    def wait(
        self,
        task_id: str,
        timeout: int = 900,
        on_tick: Callable[[dict], None] | None = None,
    ) -> dict:
        """Poll until terminal state (success|fail) or timeout. 5s for first 30s, then 10s."""
        terminal = {"success", "fail"}
        deadline = time.monotonic() + timeout
        elapsed = 0.0
        while True:
            rec = self.get_task(task_id)
            if rec.get("state") in terminal:
                return rec
            if on_tick:
                on_tick(rec)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise KieError(
                    "wait_timeout",
                    f"Task {task_id!r} did not finish within {timeout}s",
                    hint=f"kie status {task_id} --wait --json",
                    exit_code=5,
                )
            interval = 5.0 if elapsed < 30.0 else 10.0
            sleep_secs = min(interval, remaining)
            time.sleep(sleep_secs)
            elapsed += sleep_secs

    def credit(self) -> float:
        """GET /api/v1/chat/credit → float."""
        return float(self.request("GET", "/api/v1/chat/credit"))

    def upload(self, source: Path | bytes, filename: str | None = None) -> str:
        """Upload file; ≤10MB → base64 endpoint, else multipart stream. Returns public downloadUrl."""
        if isinstance(source, Path):
            data = source.read_bytes()
            fname = filename or source.name
        else:
            data = source
            fname = filename or "upload.bin"

        auth_header = self._headers()["Authorization"]
        headers = {"Authorization": auth_header}

        if len(data) <= UPLOAD_THRESHOLD:
            # Detect mime type from filename extension (best-effort)
            ext = Path(fname).suffix.lower().lstrip(".")
            mime_map = {
                "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp", "mp4": "video/mp4",
                "mov": "video/quicktime", "webm": "video/webm",
            }
            mime = mime_map.get(ext, "application/octet-stream")
            b64 = base64.b64encode(data).decode()
            payload = {
                "base64Data": f"data:{mime};base64,{b64}",
                "fileName": fname,
                "uploadPath": "images",   # required by upload server despite being documented optional
            }
            resp = httpx.post(
                f"{self._upload_base}/api/file-base64-upload",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
                timeout=TIMEOUT,
            )
        else:
            # Multipart stream upload
            resp = httpx.post(
                f"{self._upload_base}/api/file-stream-upload",
                headers=headers,
                files={"file": (fname, data)},
                data={"uploadPath": "images"},   # required by upload server
                timeout=TIMEOUT,
            )

        try:
            body = resp.json()
        except Exception as exc:
            raise KieError(
                "upstream_error",
                f"Upload: non-JSON response (HTTP {resp.status_code})",
                exit_code=5,
            ) from exc

        api_code = body.get("code", resp.status_code)
        if api_code != 200:
            raise _map_envelope_error(api_code, body.get("msg", ""), context="upload")

        # Upload server returns the public URL under `downloadUrl` (live-verified);
        # accept `fileUrl`/`url` as fallbacks in case the endpoint shape shifts.
        data_obj = body.get("data") or {}
        url = data_obj.get("downloadUrl") or data_obj.get("fileUrl") or data_obj.get("url")
        if not url:
            raise KieError(
                "upstream_error",
                f"Upload: response missing download URL (keys: {list(data_obj)})",
                exit_code=5,
            )
        return url

    def fetch_pricing(self) -> list[dict]:
        """Paginate POST /client/v1/model-pricing/page for video + image. Returns flat records."""
        records: list[dict] = []
        for interface_type in ("video", "image"):
            page = 1
            total_seen = 0
            while True:
                data = self.request(
                    "POST",
                    "/client/v1/model-pricing/page",
                    json={"pageNum": page, "pageSize": 100, "interfaceType": interface_type},
                )
                page_records = data.get("records", [])
                records.extend(page_records)
                total_seen += len(page_records)
                total = data.get("total", 0)
                if total_seen >= total or not page_records:
                    break
                page += 1
        return records


# ── Module-level download helper ─────────────────────────────────────────────

def download_file(url: str, dest_dir: Path, basename: str) -> Path:
    """Stream-download url into dest_dir/basename+ext. Extension taken from URL path."""
    parsed = urlparse(url)
    url_ext = Path(parsed.path).suffix  # e.g. ".mp4"
    filename = basename + (url_ext or "")
    dest = dest_dir / filename
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        with httpx.stream("GET", url, timeout=TIMEOUT, follow_redirects=True) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=65536):
                    fh.write(chunk)
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise KieError("network_error", str(exc), exit_code=5) from exc

    return dest
