"""Build src/kie_cli/data/models-catalog.json from the kie.ai market docs.

Each market doc page (https://docs.kie.ai/market/<vendor>/<model>.md) embeds an
OpenAPI 3 spec for POST /api/v1/jobs/createTask whose `input` schema is the model's
parameter table. This script fetches every video/image page listed in
https://docs.kie.ai/llms.txt, parses that schema and writes a flat catalog that
registry.py loads at import time (hand-written Model entries win on id collision).

Run:  uv run --with pyyaml python research/build_catalog.py
Dev-only (pyyaml is not a runtime dependency).
"""
from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import yaml

LLMS = "https://docs.kie.ai/llms.txt"
OUT = Path(__file__).resolve().parents[1] / "src" / "kie_cli" / "data" / "models-catalog.json"
HEADERS = {"Accept": "text/markdown"}  # some pages otherwise serve the HTML app shell

# Doc pages whose `model` enum was copy-pasted from a sibling page; fix by URL suffix.
ID_FIX = {
    "qwen2/text-to-image.md": "qwen2/text-to-image",
    "kling/v25-turbo-image-to-video-pro.md": "kling/v2-5-turbo-image-to-video-pro",
}


def _index() -> list[tuple[str, str, str, str]]:
    txt = httpx.get(LLMS, timeout=30).text
    rows = []
    for line in txt.splitlines():
        m = re.match(r"^- (Video|Image) +Models > ([^\[]*)\[([^\]]*)\]\((https://docs\.kie\.ai/market/[^)]*)\)", line)
        if m:
            rows.append((m.group(1).lower(), m.group(2).strip(), m.group(3).strip(), m.group(4)))
    return rows


def _fetch(url: str) -> str:
    return httpx.get(url, headers=HEADERS, timeout=60, follow_redirects=True).text


def _deref(node, comps):
    if isinstance(node, dict) and "$ref" in node:
        return comps.get(node["$ref"].split("/")[-1], {})
    return node


def _parse(kind: str, family: str, title: str, url: str, txt: str) -> dict | None:
    blocks = [b for b in re.findall(r"```yaml\n(.*?)\n```", txt, re.S) if "openapi:" in b]
    if not blocks:
        return None
    spec = yaml.safe_load(blocks[0])
    comps = (spec.get("components") or {}).get("schemas") or {}
    post = (spec.get("paths") or {}).get("/api/v1/jobs/createTask", {}).get("post")
    if not post:
        return None  # not a market-jobs endpoint (e.g. gemini-omni audio/character)
    body = post["requestBody"]["content"]["application/json"]
    schema = _deref(body["schema"], comps)
    props = schema.get("properties") or {}
    model_prop = props.get("model") or {}
    mid = (model_prop.get("enum") or [model_prop.get("default")])[0]
    if not mid:
        m = re.search(r'"model"\s*:\s*"([^"]+)"', str(body.get("example", "")))
        mid = m.group(1) if m else None
    for suffix, fixed in ID_FIX.items():
        if url.endswith("/market/" + suffix):
            mid = fixed
    if not mid:
        return None

    inp = _deref(props.get("input") or {}, comps)
    # `input` may carry plain properties AND/OR oneOf/anyOf/allOf variants — merge all.
    iprops: dict = dict(inp.get("properties") or {})
    ireq: set = set(inp.get("required") or [])
    branches = [_deref(b, comps) for k in ("oneOf", "anyOf", "allOf") for b in (inp.get(k) or [])]
    req_sets = []
    for b in branches:
        for pk, pv in (b.get("properties") or {}).items():
            iprops.setdefault(pk, pv)
        req_sets.append(set(b.get("required") or []))
    if req_sets:
        ireq |= set.intersection(*req_sets)  # required only when every variant requires it
    for v in (inp.get("x-apidog-refs") or {}).values():  # e.g. shared nsfw_checker
        for pk, pv in (_deref(v, comps).get("properties") or {}).items():
            iprops.setdefault(pk, pv)

    params = []
    for name, p in iprops.items():
        name = name.strip()  # docs carry stray trailing spaces ('image_urls ')
        p = _deref(p, comps)
        t = p.get("type", "string")
        if isinstance(t, list):
            t = t[0]
        desc = (p.get("description") or "").strip().split("\n")[0][:160]
        entry = {"name": name, "type": t, "required": name in ireq}
        if p.get("default") is not None:
            entry["default"] = p["default"]
        if p.get("enum"):
            entry["enum"] = p["enum"]
        if desc:
            entry["desc"] = desc
        params.append(entry)
    return {"id": mid, "kind": kind, "family": family, "title": title, "doc": url,
            "summary": post.get("summary"), "params": params}


def main() -> int:
    rows = _index()
    if not rows:
        print("ERROR: llms.txt returned no market pages (transient fetch failure?) — "
              "not writing", file=sys.stderr)
        return 1
    with ThreadPoolExecutor(16) as ex:
        pages = list(ex.map(lambda r: _fetch(r[3]), rows))
    catalog, skipped = [], []
    for (kind, family, title, url), txt in zip(rows, pages):
        try:
            entry = _parse(kind, family, title, url, txt)
        except Exception as exc:  # noqa: BLE001 — report and move on
            skipped.append((url, f"{type(exc).__name__}: {exc}"))
            continue
        (catalog if entry else skipped).append(entry or (url, "no createTask spec"))
    catalog.sort(key=lambda c: (c["kind"], c["id"]))
    ids = [c["id"] for c in catalog]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    if dups:
        print("ERROR duplicate ids:", dups, file=sys.stderr)
        return 1
    # Refuse to shrink the shipped catalog: a flaky page fetch must not drop models.
    try:
        prev = len(json.loads(OUT.read_text()))
    except (OSError, ValueError):
        prev = 0
    if len(catalog) < prev:
        print(f"ERROR: parsed {len(catalog)} models but the shipped catalog has {prev}; "
              f"skipped pages: {[u for u, _ in skipped]} — re-run, not writing", file=sys.stderr)
        return 1
    OUT.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {OUT} — {len(catalog)} models "
          f"({sum(c['kind'] == 'video' for c in catalog)} video, "
          f"{sum(c['kind'] == 'image' for c in catalog)} image); skipped {len(skipped)}")
    for url, why in skipped:
        print("  skipped", url, "—", why)
    return 0


if __name__ == "__main__":
    sys.exit(main())
