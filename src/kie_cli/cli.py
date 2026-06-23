"""kie CLI — argparse tree, renderers, central error handler.

Module contracts (§13):
  registry.resolve(name) -> Model
  Model.build_input(common: dict, raw_params: dict) -> dict
  pricing.estimate(model, input) -> {credits, usd, unit, formula, source}
  pricing.load_table(refresh=False) -> records
  pricing.CREDIT_USD = 0.005
  ledger.append(rec); ledger.update(taskId, **kv); ledger.rows(limit?, state?) -> list
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from kie_cli import __version__
from kie_cli.api import Client, KieError, download_file
import kie_cli.ledger as ledger


# ── I/O helpers ──────────────────────────────────────────────────────────────

def _is_tty() -> bool:
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


def _is_stderr_tty() -> bool:
    return sys.stderr.isatty() and "NO_COLOR" not in os.environ


def _json_out(obj: Any) -> None:
    """Print a single JSON object to stdout."""
    print(json.dumps(obj, ensure_ascii=False))


def _ndjson_out(rows: list[dict]) -> None:
    """Print one JSON object per line to stdout (NDJSON)."""
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


def _human(msg: str, quiet: bool = False) -> None:
    """Print human-readable message to stdout unless quiet."""
    if not quiet:
        print(msg)


def _progress(msg: str) -> None:
    """Write progress line to stderr (human mode + TTY only)."""
    if _is_stderr_tty():
        print(msg, file=sys.stderr)


def _warn(msg: str) -> None:
    """Write a warning to stderr."""
    print(f"warning: {msg}", file=sys.stderr)


def _err_human(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


def _err_json(code: str, message: str, hint: str | None) -> None:
    obj: dict = {"error": code, "message": message, "hint": hint}
    print(json.dumps(obj, ensure_ascii=False), file=sys.stderr)


def _probe_video_seconds(urls: list) -> float | None:
    """Sum the durations (s) of reference video URLs via ffprobe, so a with-video-input
    estimate can bill (input_s + output_s) correctly. Returns None if ffprobe is missing
    or any probe fails — callers then fall back to the output-only estimate."""
    import shutil
    import subprocess

    if not urls or not shutil.which("ffprobe"):
        return None
    total = 0.0
    for u in urls:
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", u],
                capture_output=True, text=True, timeout=30,
            )
            total += float(out.stdout.strip())
        except (ValueError, OSError, subprocess.SubprocessError):
            return None
    return round(total, 1) or None


# ── Central error handler ────────────────────────────────────────────────────

def _handle_error(exc: KieError, use_json: bool) -> int:
    if use_json:
        _err_json(exc.code, exc.message, exc.hint)
    else:
        msg = exc.message
        if exc.hint:
            msg += f"\n  hint: {exc.hint}"
        _err_human(msg)
    return exc.exit_code


# ── Env / config ─────────────────────────────────────────────────────────────

def _confirm_threshold() -> float:
    try:
        return float(os.environ.get("KIE_CONFIRM_THRESHOLD", "50"))
    except ValueError:
        return 50.0


# ── Generation-flag helpers ──────────────────────────────────────────────────

def _parse_params(param_list: list[str]) -> dict:
    """Parse --param key=value list; value is JSON-decoded if possible, else string."""
    result: dict = {}
    for item in param_list:
        if "=" not in item:
            raise KieError("invalid_params", f"--param must be key=value, got: {item!r}", exit_code=2)
        key, _, raw = item.partition("=")
        try:
            result[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            result[key.strip()] = raw
    return result


def _build_common(args: Any) -> dict:
    """Build the common-flag dict passed to Model.build_input."""
    common: dict = {}
    if getattr(args, "prompt", None):
        p = args.prompt
        if p == "-":
            p = sys.stdin.read()
        common["prompt"] = p
    if getattr(args, "image", None):
        common["image"] = args.image  # list; upload handled later
    if getattr(args, "last_frame", None):
        common["last_frame"] = args.last_frame
    if getattr(args, "duration", None) is not None:
        common["duration"] = args.duration
    if getattr(args, "resolution", None):
        common["resolution"] = args.resolution
    if getattr(args, "aspect_ratio", None):
        common["aspect_ratio"] = args.aspect_ratio
    # audio: only add if explicitly set; use CLI key "audio" (build_input maps it via fm.audio)
    audio = getattr(args, "audio", None)
    if audio is not None:
        common["audio"] = audio
    if getattr(args, "seed", None) is not None:
        common["seed"] = args.seed
    return common


def _apply_input_json(args: Any) -> dict | None:
    """If --input-json given, return the parsed dict; None otherwise."""
    raw = getattr(args, "input_json", None)
    if raw is None:
        return None
    if raw == "-":
        raw = sys.stdin.read()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KieError("invalid_params", f"--input-json is not valid JSON: {exc}", exit_code=2) from exc


def _upload_local_images(client: Client, common: dict) -> None:
    """Auto-upload any local file paths in common['image'] and common['last_frame'] in place."""
    images = common.get("image", [])
    uploaded = []
    for path_or_url in images:
        if not path_or_url.startswith(("http://", "https://", "asset://")):
            p = Path(path_or_url)
            if not p.exists():
                raise KieError("invalid_params", f"Image file not found: {path_or_url}", exit_code=2)
            url = client.upload(p)
            uploaded.append(url)
        else:
            uploaded.append(path_or_url)
    if uploaded:
        common["image"] = uploaded

    last = common.get("last_frame")
    if last and not last.startswith(("http://", "https://", "asset://")):
        p = Path(last)
        if not p.exists():
            raise KieError("invalid_params", f"Last-frame file not found: {last}", exit_code=2)
        common["last_frame"] = client.upload(p)


def _cost_gate(
    estimate: dict,
    yes: bool,
    max_credits: float | None,
    use_json: bool,
    model_id: str,
    args: Any,
) -> None:
    """Apply cost confirmation gate (§5). Raises KieError or exits."""
    credits = estimate["credits"]

    # credits=None means pricing unknown; skip gate entirely (server will validate)
    if credits is None:
        return

    # Hard cap — not bypassed by --yes
    if max_credits is not None and credits > max_credits:
        hint = f"kie generate {model_id} ... --max-credits {int(credits) + 1}"
        raise KieError(
            "max_credits_exceeded",
            f"Estimated {credits} credits exceeds --max-credits {max_credits}",
            hint=hint,
            exit_code=7,
        )

    threshold = _confirm_threshold()
    if credits <= threshold:
        return  # under threshold — proceed

    if yes:
        return  # --yes skips prompt

    if sys.stdin.isatty() and sys.stdout.isatty():
        # Interactive prompt
        try:
            ans = input(f"Estimated cost: {credits} credits (${estimate['usd']:.4f}). Proceed? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            raise KieError("confirmation_required", "Confirmation declined", exit_code=7)
        if ans.strip().lower() != "y":
            raise KieError("confirmation_required", "Confirmation declined", exit_code=7)
    else:
        # Non-TTY without --yes
        # Build hint reproducing the command
        hint = f"kie generate {model_id} ... --yes"
        raise KieError(
            "confirmation_required",
            f"Estimated {credits} credits exceeds threshold {threshold}; re-run with --yes",
            hint=hint,
            exit_code=7,
        )


# ── Compound result builder ──────────────────────────────────────────────────

def _normalize_ts(value: Any) -> str | None:
    """Coerce a timestamp to ISO-8601 UTC. Accepts epoch millis/seconds (int/float/
    numeric str) or an already-ISO string. Returns None for falsy/unparseable input.
    The live status API returns `createTime` as epoch millis; the ledger stores ISO —
    this keeps the `created_at` field one consistent type across all commands."""
    if not value:
        return None
    if isinstance(value, str) and not value.replace(".", "", 1).isdigit():
        return value  # already ISO (or other non-numeric string) — pass through
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return str(value)
    if epoch > 1e12:  # millis
        epoch /= 1000.0
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _compound_result(
    task_id: str,
    model_id: str,
    state: str,
    est_credits: float,
    record: dict | None,
    files: list[str],
    created_at: str | None,
    elapsed_s: float | None,
) -> dict:
    credits_consumed = None
    usd = None
    result_urls: list[str] = []
    if record:
        credits_consumed = record.get("creditsConsumed")
        usd_raw = record.get("usd") or (
            credits_consumed * 0.005 if credits_consumed is not None else None
        )
        usd = usd_raw
        result_urls = record.get("resultUrls", [])

    return {
        "taskId": task_id,
        "model": model_id,
        "state": state,
        "est_credits": est_credits,
        "credits_consumed": credits_consumed,
        "usd": usd,
        "result_urls": result_urls,
        "files": files,
        "created_at": created_at,
        "elapsed_s": round(elapsed_s, 1) if elapsed_s is not None else None,
    }


# ── Subcommand: models ────────────────────────────────────────────────────────

def cmd_models(args: Any, client: Client) -> int:
    from kie_cli import registry  # parallel agent builds this
    all_models = list(registry.MODELS.values())
    if args.kind:
        all_models = [m for m in all_models if m.kind == args.kind]
    rows_out = [
        {"id": m.id, "aliases": m.aliases, "kind": m.kind, "modes": getattr(m, "modes", [])}
        for m in all_models
    ]
    if args.json:
        _ndjson_out(rows_out)
    else:
        for r in rows_out:
            aliases = ", ".join(r["aliases"]) if r["aliases"] else ""
            alias_str = f"  ({aliases})" if aliases else ""
            print(f"{r['id']}{alias_str}  [{r['kind']}]")
    return 0


# ── Subcommand: schema ────────────────────────────────────────────────────────

def _param_to_dict(p: Any) -> dict:
    """Convert a Param dataclass to a plain dict for JSON serialization."""
    d: dict = {"name": p.name, "type": p.type, "required": p.required}
    if p.default is not None:
        d["default"] = p.default
    if p.enum is not None:
        d["enum"] = p.enum
    if p.desc:
        d["desc"] = p.desc
    return d


def cmd_schema(args: Any, client: Client) -> int:
    from kie_cli import registry
    model = registry.resolve(args.model)
    params_raw = getattr(model, "params", [])
    params = [_param_to_dict(p) for p in params_raw]
    if args.json:
        _json_out({"model": model.id, "params": params})
    else:
        print(f"Schema for {model.id}:")
        for p in params:
            req = "required" if p.get("required") else "optional"
            default = f"  default={p['default']}" if "default" in p else ""
            enum = f"  enum={p['enum']}" if "enum" in p else ""
            print(f"  {p['name']} ({p['type']}, {req}){default}{enum}")
    return 0


# ── Subcommand: balance ───────────────────────────────────────────────────────

def cmd_balance(args: Any, client: Client) -> int:
    credits = client.credit()
    usd = credits * 0.005
    if args.json:
        _json_out({"credits": credits, "usd": round(usd, 4)})
    else:
        _human(f"Balance: {credits} credits (${usd:.4f} USD)", quiet=args.quiet)
    return 0


# ── Subcommand: cost ──────────────────────────────────────────────────────────

def cmd_cost(args: Any, client: Client) -> int:
    from kie_cli import registry
    from kie_cli import pricing
    from kie_cli import dummy_ref

    model = registry.resolve(args.model)

    # Build input for estimate — skip required-field validation (pricing doesn't need full input)
    if _apply_input_json(args) is not None:
        inp = _apply_input_json(args)
    else:
        common = _build_common(args)
        raw_params = _parse_params(getattr(args, "param", []) or [])
        inp = model.build_input(common, raw_params, validate_required=False)

    # Mirror generate: preview the cheaper SKU when the dummy ref would be attached.
    dummy_secs = 0.0
    if dummy_ref.wants_dummy(model.id, inp, enabled=args.dummy_ref):
        inp["reference_video_urls"] = [dummy_ref.DUMMY_REF_URL]
        dummy_secs = dummy_ref.DUMMY_REF_SECONDS
    elif inp.get("reference_video_urls"):
        # Explicit user video ref(s): probe their duration so the estimate bills
        # (input_s + output_s), not output only. Falls back silently if unprobeable.
        probed = _probe_video_seconds(inp["reference_video_urls"])
        if probed:
            dummy_secs = probed

    est = pricing.estimate(model, inp, extra_input_seconds=dummy_secs)
    credits = est["credits"]

    try:
        balance = client.credit()
        sufficient = (balance >= credits) if credits is not None else None
    except KieError:
        balance = None
        sufficient = None

    result = {
        "model": model.id,
        "credits": credits,
        "usd": est["usd"],
        "unit": est.get("unit"),
        "formula": est.get("formula"),
        "source": est.get("source"),
        "balance": balance,
        "sufficient": sufficient,
    }

    if args.json:
        _json_out(result)
    else:
        # credits/usd are None when no pricing record matched — render gracefully
        # instead of crashing on the float format spec.
        if credits is None:
            estimate_line = "Estimate: unknown (no pricing record matched)"
        else:
            estimate_line = f"Estimate: {credits} credits (${est['usd']:.4f} USD)"
        note = est.get("note")
        _human(
            f"Model: {model.id}\n"
            f"{estimate_line}\n"
            f"Formula: {est.get('formula', 'n/a')}\n"
            + (f"Note: {note}\n" if note else "")
            + f"Balance: {balance} credits  sufficient={sufficient}",
            quiet=args.quiet,
        )
    return 0


# ── Subcommand: generate ──────────────────────────────────────────────────────

def cmd_generate(args: Any, client: Client) -> int:
    from kie_cli import registry
    from kie_cli import pricing
    from kie_cli import dummy_ref

    model = registry.resolve(args.model)

    # Build input
    input_override = _apply_input_json(args)
    if input_override is not None:
        inp = input_override
    else:
        common = _build_common(args)
        raw_params = _parse_params(getattr(args, "param", []) or [])
        # Upload local images before build_input
        _upload_local_images(client, common)
        inp = model.build_input(common, raw_params)

    # Auto-attach a blank 2s video ref for seedance-2/-2-fast/-2-mini when the
    # caller gave no video ref — flips onto the cheaper "with video input" SKU.
    dummy_secs = 0.0
    if dummy_ref.wants_dummy(model.id, inp, enabled=args.dummy_ref):
        inp["reference_video_urls"] = [dummy_ref.DUMMY_REF_URL]
        dummy_secs = dummy_ref.DUMMY_REF_SECONDS
    elif inp.get("reference_video_urls"):
        # Explicit user video ref(s): probe their duration so the estimate bills
        # (input_s + output_s), not output only. Falls back silently if unprobeable.
        probed = _probe_video_seconds(inp["reference_video_urls"])
        if probed:
            dummy_secs = probed

    # Pricing estimate
    est = pricing.estimate(model, inp, extra_input_seconds=dummy_secs)

    # Dry-run: print and exit — skip cost gate (no submission happens)
    if args.dry_run:
        request_body: dict = {"model": model.id, "input": inp}
        if args.callback:
            request_body["callBackUrl"] = args.callback
        result = {"model": model.id, "request": request_body, "estimate": est}
        if args.json:
            _json_out(result)
        else:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    # Cost gate — checked AFTER dry-run (dry-run skips gate; gate only guards actual submission)
    _cost_gate(est, args.yes, args.max_credits, args.json, model.id, args)

    # Submit task
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    t_start = time.monotonic()
    task_id = client.create_task(model.id, inp, callback=getattr(args, "callback", None))

    # Ledger: append pending row
    ledger.append({
        "taskId": task_id,
        "model": model.id,
        "state": "waiting",
        "est_credits": est["credits"],
        "created_at": created_at,
        "result_urls": [],
        "files": [],
    })

    if args.json:
        # Emit taskId immediately so agent can track it
        pass  # we'll emit compound result after wait
    else:
        _human(f"Task created: {task_id}", quiet=args.quiet)

    if args.no_wait:
        result = _compound_result(
            task_id, model.id, "waiting", est["credits"],
            None, [], created_at, None
        )
        if args.json:
            _json_out(result)
        else:
            _human(f"taskId: {task_id}  (--no-wait; use: kie status {task_id} --wait)", quiet=args.quiet)
        return 0

    # Poll with progress ticks
    timeout = getattr(args, "timeout", 900) or 900

    def _tick(rec: dict) -> None:
        elapsed = round(time.monotonic() - t_start, 0)
        _progress(f"  [{elapsed:.0f}s] state={rec.get('state')} taskId={task_id}")

    record = client.wait(task_id, timeout=timeout, on_tick=_tick)
    elapsed_s = time.monotonic() - t_start

    state = record.get("state", "unknown")
    result_urls: list[str] = record.get("resultUrls", [])
    credits_consumed = record.get("creditsConsumed")

    # Update ledger with terminal state
    ledger.update(task_id,
        state=state,
        result_urls=result_urls,
        credits_consumed=credits_consumed,
    )

    if state == "fail":
        fail_msg = record.get("failMsg") or record.get("resultJson", {}).get("failMsg", "")
        raise KieError(
            "generation_failed",
            f"Task {task_id} failed: {fail_msg}",
            hint=f"kie status {task_id} --json",
            exit_code=5,
        )

    # Download results
    files: list[str] = []
    if not args.no_download and result_urls:
        out_dir = Path(args.out or ".")
        for n, url in enumerate(result_urls, 1):
            dest = download_file(url, out_dir, f"{task_id}-{n}")
            files.append(str(dest))
        ledger.update(task_id, files=files)
        if not args.json:
            for f in files:
                _human(f, quiet=args.quiet)

    compound = _compound_result(
        task_id, model.id, state, est["credits"],
        record, files, created_at, elapsed_s
    )
    if args.json:
        _json_out(compound)
    else:
        _human(
            f"Done: {task_id}  state={state}  credits_consumed={credits_consumed}  "
            f"elapsed={elapsed_s:.1f}s",
            quiet=args.quiet,
        )
    return 0


# ── Subcommand: status ────────────────────────────────────────────────────────

def cmd_status(args: Any, client: Client) -> int:
    task_id = args.task_id
    t_start = time.monotonic()

    if args.wait:
        timeout = getattr(args, "timeout", 900) or 900

        def _tick(rec: dict) -> None:
            elapsed = round(time.monotonic() - t_start, 0)
            _progress(f"  [{elapsed:.0f}s] state={rec.get('state')} taskId={task_id}")

        record = client.wait(task_id, timeout=timeout, on_tick=_tick)
    else:
        record = client.get_task(task_id)

    elapsed_s = time.monotonic() - t_start
    state = record.get("state", "unknown")
    result_urls = record.get("resultUrls", [])
    credits_consumed = record.get("creditsConsumed")

    # Update ledger if row exists
    ledger.update(task_id, state=state, result_urls=result_urls, credits_consumed=credits_consumed)

    compound = _compound_result(
        task_id,
        record.get("model", ""),
        state,
        0,  # est_credits unknown from status
        record,
        [],
        _normalize_ts(record.get("createTime") or record.get("createdAt")),
        elapsed_s if args.wait else None,
    )

    if state == "fail":
        fail_msg = record.get("failMsg", "")
        if args.json:
            _json_out(compound)
        else:
            _err_human(f"Task {task_id} failed: {fail_msg}")
        return 5

    if args.json:
        _json_out(compound)
    else:
        _human(
            f"taskId={task_id}  state={state}  credits_consumed={credits_consumed}",
            quiet=args.quiet,
        )
    return 0


# ── Subcommand: logs ──────────────────────────────────────────────────────────

def cmd_logs(args: Any, client: Client) -> int:
    """Surface the exact input a task was submitted with — the prompt and params
    that actually reached the model.

    The task record stores the original request under `param`, whose `input` is a
    JSON string of the submitted payload. This is the same data the web dashboard's
    logs page shows; exposing it lets you verify a prompt was transmitted intact
    (e.g. confirm no truncation) rather than guessing from the result.
    """
    record = client.get_task(args.task_id)

    # _normalize_record parses `param` into a dict {"input": "<json string>", "model": …};
    # the nested `input` is still a JSON string — parse it to the submitted payload.
    submitted: dict = {}
    param = record.get("param")
    if isinstance(param, dict):
        inp = param.get("input")
        if isinstance(inp, str) and inp:
            try:
                submitted = json.loads(inp)
            except json.JSONDecodeError:
                submitted = {}
        elif isinstance(inp, dict):
            submitted = inp

    out = {
        "taskId": args.task_id,
        "model": record.get("model"),
        "state": record.get("state"),
        "creditsConsumed": record.get("creditsConsumed"),
        "failMsg": record.get("failMsg") or None,
        "input": submitted,
    }

    if args.json:
        _json_out(out)
    else:
        prompt = submitted.get("prompt", "")
        meta = {k: v for k, v in submitted.items() if k != "prompt"}
        _human(f"taskId={args.task_id}  state={out['state']}  model={out['model']}", quiet=args.quiet)
        if out["failMsg"]:
            _human(f"failMsg: {out['failMsg']}", quiet=args.quiet)
        if meta:
            _human(f"input params: {json.dumps(meta, ensure_ascii=False)}", quiet=args.quiet)
        if prompt:
            _human(f"prompt ({len(prompt)} chars):\n{prompt}", quiet=args.quiet)
    return 0


# ── Subcommand: download ──────────────────────────────────────────────────────

def cmd_download(args: Any, client: Client) -> int:
    task_id = args.task_id
    out_dir = Path(args.out or ".")

    # Look up in ledger first
    existing = ledger.rows()
    record: dict | None = None
    for row in existing:
        if row.get("taskId") == task_id:
            record = row
            break

    if record is None or not record.get("result_urls"):
        # Fall back to live API
        record = client.get_task(task_id)
        ledger.update(task_id, state=record.get("state"), result_urls=record.get("resultUrls", []))

    result_urls = record.get("result_urls") or record.get("resultUrls", [])
    if not result_urls:
        raise KieError("task_not_found", f"No result URLs found for task {task_id}", exit_code=3)

    files: list[str] = []
    for n, url in enumerate(result_urls, 1):
        dest = download_file(url, out_dir, f"{task_id}-{n}")
        files.append(str(dest))

    ledger.update(task_id, files=files)

    if args.json:
        _json_out({"taskId": task_id, "files": files})
    else:
        for f in files:
            _human(f, quiet=args.quiet)
    return 0


# ── Subcommand: tasks ─────────────────────────────────────────────────────────

def cmd_tasks(args: Any, client: Client) -> int:
    limit = getattr(args, "limit", 20) or 20
    state_filter = getattr(args, "state", None)

    task_rows = ledger.rows(limit=None, state=state_filter)

    if args.refresh:
        terminal = {"success", "fail"}
        refreshed: list[dict] = []
        for row in task_rows:
            if row.get("state") not in terminal:
                try:
                    rec = client.get_task(row["taskId"])
                    row["state"] = rec.get("state", row.get("state"))
                    row["result_urls"] = rec.get("resultUrls", row.get("result_urls", []))
                    row["credits_consumed"] = rec.get("creditsConsumed", row.get("credits_consumed"))
                    ledger.update(row["taskId"], state=row["state"],
                                  result_urls=row["result_urls"],
                                  credits_consumed=row["credits_consumed"])
                except KieError:
                    pass  # stale row — skip silently
            refreshed.append(row)
        task_rows = refreshed

    task_rows = task_rows[:limit]

    if args.json:
        _ndjson_out(task_rows)
    else:
        if not task_rows:
            _human("No tasks found.", quiet=args.quiet)
        for row in task_rows:
            print(
                f"{row.get('taskId','')}  {row.get('state','')}  "
                f"{row.get('model','')}  {row.get('created_at','')}"
            )
    return 0


# ── Subcommand: upload ────────────────────────────────────────────────────────

def cmd_upload(args: Any, client: Client) -> int:
    source = args.file
    if source == "-":
        data = sys.stdin.buffer.read()
        url = client.upload(data, filename="stdin.bin")
    else:
        p = Path(source)
        if not p.exists():
            raise KieError("invalid_params", f"File not found: {source}", exit_code=2)
        url = client.upload(p)

    if args.json:
        _json_out({"url": url})
    else:
        _human(url, quiet=args.quiet)
    return 0


# ── Subcommand: pricing ───────────────────────────────────────────────────────

def cmd_pricing(args: Any, client: Client) -> int:
    from kie_cli import pricing

    try:
        records = pricing.load_table(refresh=args.refresh)
    except KieError as exc:
        _warn(f"Pricing refresh failed ({exc.message}); using cached snapshot.")
        records = pricing.load_table(refresh=False)

    model_substr = getattr(args, "model", None)
    if model_substr:
        records = [r for r in records if model_substr.lower() in str(r).lower()]

    if args.json:
        _ndjson_out(records)
    else:
        for r in records:
            desc = r.get("modelDescription", r.get("model", ""))
            credit = r.get("creditPrice", "?")
            unit = r.get("creditUnit", "")
            print(f"{desc}  {credit} credits/{unit}")
    return 0


# ── Argument parser ───────────────────────────────────────────────────────────

def _add_global_flags(parser: Any) -> None:
    """Add flags shared across all subcommands."""
    parser.add_argument("--json", action="store_true", default=False,
                        help="Structured JSON/NDJSON output")
    parser.add_argument("-v", "--verbose", action="store_true", default=False,
                        help="HTTP request/response logging to stderr")
    parser.add_argument("-q", "--quiet", action="store_true", default=False,
                        help="Suppress non-essential human output")


def _add_generation_flags(parser: Any) -> None:
    """Add generation flags shared by `cost` and `generate`."""
    parser.add_argument("-p", "--prompt", metavar="TEXT",
                        help="Prompt text; '-' reads stdin")
    parser.add_argument("-i", "--image", metavar="PATH_OR_URL", action="append",
                        help="Image input (repeatable); local paths auto-uploaded")
    parser.add_argument("--last-frame", metavar="PATH_OR_URL",
                        help="Last-frame image URL or local path")
    parser.add_argument("--duration", type=int, metavar="N",
                        help="Duration in seconds")
    parser.add_argument("--resolution", metavar="RES",
                        help="e.g. 480p, 720p, 1080p")
    parser.add_argument("--aspect-ratio", metavar="RATIO",
                        help="e.g. 16:9, 9:16, 1:1")
    parser.add_argument("--audio", dest="audio", action="store_true", default=None,
                        help="Enable audio generation")
    parser.add_argument("--no-audio", dest="audio", action="store_false",
                        help="Disable audio generation")
    parser.add_argument("--seed", type=int, metavar="N",
                        help="Random seed (where supported)")
    parser.add_argument("--param", metavar="key=value", action="append",
                        help="Raw passthrough param (repeatable); value parsed as JSON if valid")
    parser.add_argument("--input-json", metavar="JSON|-",
                        help="Full input object verbatim; '-' reads stdin")
    parser.add_argument("--dummy-ref", dest="dummy_ref", action="store_true", default=True,
                        help="Attach the cost-saving blank 2s video ref on seedance-2 / -fast / -mini when no "
                             "video ref is present (cheaper with-video SKU). ON by default; auto-skipped "
                             "when a real video ref is present.")
    parser.add_argument("--no-dummy-ref", dest="dummy_ref", action="store_false",
                        help="Opt OUT of the dummy ref (manual escape hatch). The dummy does NOT harm "
                             "verbatim dialogue — adherence is governed by prompt length (keep <=~3000 "
                             "chars). Reach for this only if a dialogue regression reappears on an "
                             "already-lean prompt.")


def build_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(
        prog="kie",
        description="Generate video/image with kie.ai models, with pre-flight credit cost estimates.",
    )
    parser.add_argument("--version", action="version", version=f"kie {__version__}")
    _add_global_flags(parser)

    sub = parser.add_subparsers(dest="subcommand", metavar="<subcommand>")
    sub.required = True

    # models
    p_models = sub.add_parser("models", help="List available models")
    _add_global_flags(p_models)
    p_models.add_argument("--kind", choices=["video", "image"],
                          help="Filter by kind")

    # schema
    p_schema = sub.add_parser("schema", help="Print model input schema")
    _add_global_flags(p_schema)
    p_schema.add_argument("model", metavar="MODEL")

    # balance
    p_balance = sub.add_parser("balance", help="Show current credit balance")
    _add_global_flags(p_balance)

    # cost
    p_cost = sub.add_parser("cost", help="Estimate generation cost")
    _add_global_flags(p_cost)
    p_cost.add_argument("model", metavar="MODEL")
    _add_generation_flags(p_cost)

    # generate
    p_gen = sub.add_parser("generate", help="Generate video/image")
    _add_global_flags(p_gen)
    p_gen.add_argument("model", metavar="MODEL")
    _add_generation_flags(p_gen)
    p_gen.add_argument("--wait", dest="no_wait", action="store_false", default=False,
                       help="Wait for task completion (default)")
    p_gen.add_argument("--no-wait", dest="no_wait", action="store_true",
                       help="Return taskId immediately without waiting")
    p_gen.add_argument("--timeout", type=int, default=900, metavar="SECS",
                       help="Wait timeout in seconds (default 900)")
    p_gen.add_argument("--out", metavar="DIR", default=".",
                       help="Output directory for downloaded files (default: .)")
    p_gen.add_argument("--no-download", action="store_true", default=False,
                       help="Skip downloading result files")
    p_gen.add_argument("--yes", action="store_true", default=False,
                       help="Skip cost confirmation prompt")
    p_gen.add_argument("--max-credits", type=float, metavar="N",
                       help="Hard credit cap; exit 7 if estimate exceeds this")
    p_gen.add_argument("--dry-run", action="store_true", default=False,
                       help="Print request + estimate, never submit")
    p_gen.add_argument("--callback", metavar="URL",
                       help="Webhook URL for completion notification")

    # status
    p_status = sub.add_parser("status", help="Get task status")
    _add_global_flags(p_status)
    p_status.add_argument("task_id", metavar="TASK_ID")
    p_status.add_argument("--wait", action="store_true", default=False,
                          help="Block until terminal state")
    p_status.add_argument("--timeout", type=int, default=900, metavar="SECS")

    # logs
    p_logs = sub.add_parser("logs", help="Show a task's submitted input (prompt + params)")
    _add_global_flags(p_logs)
    p_logs.add_argument("task_id", metavar="TASK_ID")

    # download
    p_dl = sub.add_parser("download", help="Download results of a task")
    _add_global_flags(p_dl)
    p_dl.add_argument("task_id", metavar="TASK_ID")
    p_dl.add_argument("--out", metavar="DIR", default=".",
                      help="Output directory (default: .)")

    # tasks
    p_tasks = sub.add_parser("tasks", help="List tasks from local ledger")
    _add_global_flags(p_tasks)
    p_tasks.add_argument("--limit", type=int, default=20, metavar="N")
    p_tasks.add_argument("--state", metavar="STATE",
                         help="Filter by state (e.g. success, fail, waiting)")
    p_tasks.add_argument("--refresh", action="store_true", default=False,
                         help="Re-poll non-terminal tasks from API")

    # upload
    p_upload = sub.add_parser("upload", help="Upload a file to kie storage")
    _add_global_flags(p_upload)
    p_upload.add_argument("file", metavar="FILE|-")

    # pricing
    p_pricing = sub.add_parser("pricing", help="Show pricing table")
    _add_global_flags(p_pricing)
    p_pricing.add_argument("--refresh", action="store_true", default=False,
                           help="Re-fetch pricing from API")
    p_pricing.add_argument("--model", metavar="SUBSTR",
                           help="Filter by model name substring")

    return parser


# ── Entry point ───────────────────────────────────────────────────────────────

SUBCOMMAND_MAP = {
    "models": cmd_models,
    "schema": cmd_schema,
    "balance": cmd_balance,
    "cost": cmd_cost,
    "generate": cmd_generate,
    "status": cmd_status,
    "logs": cmd_logs,
    "download": cmd_download,
    "tasks": cmd_tasks,
    "upload": cmd_upload,
    "pricing": cmd_pricing,
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # Verbose: enable httpx logging
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.DEBUG)

    # Resolve JSON flag: subcommand flag takes precedence (already on args namespace)
    use_json = args.json

    # Build client (auth checked lazily at first API call, not here)
    client = Client()

    handler = SUBCOMMAND_MAP.get(args.subcommand)
    if handler is None:
        parser.print_help(sys.stderr)
        sys.exit(2)

    try:
        exit_code = handler(args, client)
    except KieError as exc:
        sys.exit(_handle_error(exc, use_json))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001
        if use_json:
            _err_json("internal", str(exc), None)
        else:
            _err_human(f"Internal error: {exc}")
        if args.verbose:
            import traceback
            traceback.print_exc(file=sys.stderr)
        sys.exit(1)

    sys.exit(exit_code)
