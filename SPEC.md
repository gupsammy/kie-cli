# kie — CLI spec (v1)

Agent-first CLI wrapping the kie.ai generation API. Primary: video models (Seedance family);
secondary: image models. Designed per clig.dev + Agent Ergonomics rubric.

0. **Language & distribution**: Python ≥3.11 · `argparse` (stdlib) · dep: `httpx` only ·
   uv project (`pyproject.toml`, `src/kie_cli/`) · install: `uv tool install -e .` → `kie` on PATH
   (also runnable as `uv run kie`).
1. **Name**: `kie`
2. **One-liner**: Generate video/image with kie.ai models, with pre-flight credit cost estimates.

## 3. USAGE

```
kie [global flags] <subcommand> [args]

kie models   [--kind video|image] [--json]
kie schema   <model> [--json]
kie cost     <model> [generation flags] [--json]
kie balance  [--json]
kie generate <model> -p PROMPT [generation flags] [--wait|--no-wait] [--out DIR]
             [--no-download] [--dry-run] [--yes] [--max-credits N] [--json]
kie status   <taskId> [--wait] [--timeout SECS] [--json]
kie download <taskId> [--out DIR] [--json]
kie tasks    [--limit N] [--state STATE] [--refresh] [--json]
kie upload   <file|-> [--json]
kie pricing  [--refresh] [--model SUBSTR] [--json]
```

## 4. Subcommands

| Cmd | Semantics | Idempotent | State |
|---|---|---|---|
| `models` | List model registry (bundled). | yes | none |
| `schema` | Print a model's input params: name, type, required, default, enum. Source: bundled registry. | yes | none |
| `cost` | Estimate credits + USD for a generation BEFORE running it. Computed locally from pricing cache. Also reports current balance and `sufficient: bool`. | yes | none |
| `balance` | `GET /api/v1/chat/credit` → credits + USD. | yes | none |
| `generate` | Build input from flags → cost estimate → confirm gate → `POST /api/v1/jobs/createTask` → (default) poll until terminal → download results → ledger append. `--no-wait` returns taskId immediately. `--dry-run` prints exact request body + estimate, exits 0, never submits. | no (creates task; safe to retry only if no taskId was returned) | ledger append |
| `status` | Poll one task. `--wait` blocks until terminal/timeout (resumes an interrupted `generate --wait`). Updates ledger. | yes | ledger update |
| `download` | Re-download results of a (successful) task to `--out`. | yes | ledger update (local_paths) |
| `tasks` | Read local ledger (newest first). `--refresh` re-polls non-terminal entries. | yes (`--refresh`: API reads) | ledger update |
| `upload` | Push a local file (or stdin `-`) to kie file API → returns URL usable in inputs. Files expire ~24h server-side. | no (new URL each call) | none |
| `pricing` | Show cached pricing table; `--refresh` re-fetches from pricing endpoint. | yes | cache file |

## 5. Global flags

- `-h, --help` (all levels) · `--version`
- `--json` — structured output contract (see §6). NDJSON for `models`, `tasks`, `pricing`.
- `-v, --verbose` — HTTP request/response logging to stderr.
- `-q, --quiet` — suppress non-essential human output (no effect on `--json` data).

### Generation flags (shared by `cost` and `generate`; consistent everywhere)

- `-p, --prompt TEXT` — `-` reads stdin.
- `-i, --image PATH_OR_URL` — repeatable. Local paths are auto-uploaded (base64 ≤10MB, stream above) and substituted with the returned URL. Mapped per-model by the registry: single → `image_url`/`first_frame_url`/`image`, list → `image_urls`/`reference_image_urls`.
- `--last-frame PATH_OR_URL` — maps to `last_frame_url`/`end_image_url` where supported.
- `--duration N` · `--resolution 480p|720p|1080p|...` · `--aspect-ratio 16:9|...` — registry coerces type (some models want string durations, some int).
- `--audio / --no-audio` → `generate_audio` (only where supported).
- `--seed N` (where supported).
- `--param key=value` — repeatable raw passthrough into `input` (escape hatch; value parsed as JSON if it parses, else string). Unknown keys are NOT validated locally — server is source of truth.
- `--input-json JSON|-` — full `input` object verbatim (overrides everything except `model`); `-` reads stdin.

### `generate`-only flags

- `--wait` (default) / `--no-wait` · `--timeout SECS` (default 900) · `--out DIR` (default `.`) · `--no-download`
- `--yes` — skip cost confirmation. `--max-credits N` — hard cap: exit 7 before submitting if estimate > N.
- `--dry-run` — print request + estimate, never submit.
- `--callback URL` — pass-through `callBackUrl`.

### Cost confirmation gate (safety rail)

estimate ≤ threshold (default 50 credits, env `KIE_CONFIRM_THRESHOLD`) → proceed.
Above threshold: TTY → y/N prompt; non-TTY without `--yes` → exit 7,
`{"error":"confirmation_required","hint":"kie generate ... --yes"}`. `--max-credits` is an
independent hard cap that `--yes` does NOT bypass.

## 6. I/O contract

- stdout: primary data only. Human mode: brief text (state changes say what changed: taskId, credits consumed, file paths). JSON mode: single object per command; NDJSON (one obj/line) for list commands.
- stderr: progress (polling ticks, human mode + TTY only), warnings (stale pricing cache), errors.
- `--json` errors → stderr: `{"error":"<snake_code>","message":"...","hint":"<exact command or null>"}`.
- Cosmetics: no spinners/color when not a TTY or `--json` or `NO_COLOR`.
- `generate`/`status` success JSON includes (compound output — no follow-up call needed):
  `{taskId, model, state, est_credits, credits_consumed, usd, result_urls, files, created_at, elapsed_s}`.
- `tasks --json` NDJSON rows = ledger records; `--limit` default 20; newest first.

## 7. Exit codes (typed, identical across subcommands)

| Code | Meaning | Triggers | JSON `error` examples |
|---|---|---|---|
| 0 | success | | |
| 1 | generic runtime | unexpected exception | `internal` |
| 2 | usage | bad flag, unknown model, missing required param, bad enum (hard-validated only for required fields) | `unknown_model` (hint: `kie models --json`), `invalid_params` |
| 3 | not found | unknown taskId (API 404 / 422 "recordInfo is null") | `task_not_found` |
| 4 | auth | missing `KIE_API_KEY`, API 401 | `auth_missing`, `auth_invalid` |
| 5 | upstream | network error, 429, 455, 5xx, task ended `state=fail` (`failMsg` in message), wait timeout (`wait_timeout`, hint: `kie status <id> --wait --json`) | `rate_limited`, `generation_failed`, `wait_timeout` |
| 7 | precondition | 402 insufficient credits (hint: `kie balance --json`), confirmation refused, `--max-credits` exceeded, mutually-exclusive inputs (seedance-2: first/last-frame vs reference images) | `insufficient_credits`, `confirmation_required`, `max_credits_exceeded` |

## 8. Env/config

- `KIE_API_KEY` (required; never a flag) · `KIE_API_BASE` (default `https://api.kie.ai`) ·
  `KIE_UPLOAD_BASE` (default `https://kieai.redpandaai.co`) · `KIE_HOME` (default `~/.kie`) ·
  `KIE_CONFIRM_THRESHOLD` (default 50) · `NO_COLOR`.
- No config file (flags > env only). State in `$KIE_HOME`: `tasks.jsonl` (ledger), `pricing.json` (cache).

## 9. Data Layer Decision

**Stateless wrapper + local task ledger + pricing cache.** Live polling must be fresh (Signal 5 NO
for status). But the API has no list-my-tasks endpoint (Signal 3) → append-only JSONL ledger written
on create/terminal-state is the only way an agent answers "what did I generate?". Pricing is
staleness-tolerant (Signal 5 YES) and one bulk endpoint → cached JSON with 7-day staleness warning +
bundled snapshot fallback (`data/pricing-snapshot.json`).

## 10. API provenance

Source: https://docs.kie.ai/llms.txt (documented) + JS-bundle discovery (undocumented pricing
endpoint). Full extraction: `research/kie-research.json`.

| Subcommand | ← Endpoint |
|---|---|
| `generate` | `POST /api/v1/jobs/createTask` `{model, callBackUrl?, input}` → `{data:{taskId}}` |
| `status`/`wait` | `GET /api/v1/jobs/recordInfo?taskId=` → states `waiting→queuing→generating→success|fail`; `resultJson` is a JSON-string with `resultUrls`. Some market docs reference `GET /api/v1/jobs/getTaskDetail` — implement recordInfo with one-time fallback; live test decides. |
| `balance` | `GET /api/v1/chat/credit` → `{data: 10051.0}` (float; **live-verified**) |
| `upload` | `POST {UPLOAD_BASE}/api/file-base64-upload` (≤10MB) / `file-stream-upload` (multipart) → `data.fileUrl` |
| `download` | result URLs valid 14 days; `POST /api/v1/common/download-url` → 20-min direct link (kie URLs only) |
| `pricing --refresh` | `POST /client/v1/model-pricing/page` `{pageNum,pageSize≤100,interfaceType?}` → paginated `{records:[{modelDescription,creditPrice,creditUnit,usdPrice,...}]}` (**UNDOCUMENTED, live-verified**, works with Bearer API key) |

Response envelope everywhere: `{code, msg, data}`; `code` 200 ok; map 401→4, 402→7, 404/422→3,
429/455/5xx→5. Auth: `Authorization: Bearer $KIE_API_KEY`.

**Fragility**: `/client/v1/model-pricing/*` is site-internal — isolate in `pricing.py`; on failure
fall back to bundled snapshot with a stderr warning (never hard-fail cost estimates). Rate limit:
20 createTask/10s. 1 credit = $0.005.

## 11. Cost model (pricing.py)

- Per-second models (seedance-2, seedance-2-fast, kling-3.0, grok video): `credits = unit(resolution, video_input?, audio?) × duration`; seedance with reference_video_urls: `unit × (input_s + output_s)`.
- Fixed-SKU models (kling-2.6, wan-2.6, hailuo-2.3, seedance-1.5-pro, v1-*): lookup by (duration, resolution, audio).
- Per-image models: flat per image (× n where `num_images` supported).
- Estimates are labeled `"estimate"`; actual `creditsConsumed` comes back in recordInfo and is written to ledger.

## 12. Model registry (v1 — market jobs API only; veo3/runway dedicated APIs out of scope v1)

Video: `bytedance/seedance-2` (+`-fast`), `bytedance/seedance-1.5-pro`, `bytedance/v1-pro-{text,image}-to-video`, `bytedance/v1-pro-fast-image-to-video`, `bytedance/v1-lite-{text,image}-to-video`, kling 2.6 t2v/i2v, kling 3.0, wan 2.6 t2v/i2v, hailuo 2.3 i2v pro, grok-imagine t2v/i2v, topaz/video-upscale.
Image: google/nano-banana (+edit), nano-banana-2, nano-banana-pro, seedream/4.5-{text-to-image,edit}, seedream/5-lite-text-to-image, z-image, flux-2/pro-text-to-image, qwen/image-edit, topaz/image-upscale, recraft/remove-background.

Exact model ids, full param tables, and enums: `research/kie-research.json` (`.seedance.models`,
`.otherVideo.models`, `.images.models`). Registry stores per model: id, aliases (short names:
`seedance-2`, `nano-banana`, `z-image`, ...), kind, modes (t2v/i2v/edit), param specs (for `schema`
+ type coercion + required check), common-flag mapping, mutual-exclusion groups, pricing rule key.
Validation philosophy: hard-validate required fields + flag-mapped enums; pass everything else
through (server is source of truth — never block a param the server might accept).

## 13. Module contracts (build to these exactly)

```
src/kie_cli/api.py      KieError(code, message, hint, exit_code); Client(api_key?, base?):
                        .request(method, path, json?, base?) -> data (envelope unwrap + error map)
                        .create_task(model, input, callback?) -> taskId
                        .get_task(taskId) -> normalized dict (parses param/resultJson strings)
                        .wait(taskId, timeout, on_tick?) -> record   # poll 5s→10s backoff
                        .credit() -> float
                        .upload(path|bytes, filename?) -> url
                        .fetch_pricing() -> list[record]             # paginates all types
                        download_file(url, dest_dir, basename) -> Path
src/kie_cli/registry.py MODELS: dict[id, Model]; resolve(name) -> Model (alias/exact, else KieError
                        unknown_model); Model.build_input(common: dict, raw_params: dict) -> dict
src/kie_cli/pricing.py  estimate(model, input) -> {credits, usd, unit, formula, source}
                        load_table(refresh=False) -> records; CREDIT_USD = 0.005
src/kie_cli/ledger.py   append(rec); update(taskId, **kv); rows(limit?, state?) -> list (newest first)
src/kie_cli/cli.py      argparse tree; human+json renderers; central error handler -> exit codes
```

## 14. Examples

```bash
# 1. Pre-flight cost check (agent loop staple)
kie cost seedance-2 --duration 8 --resolution 1080p --no-audio --json
# → {"model":"bytedance/seedance-2","credits":816,"usd":4.08,"sufficient":true,...}

# 2. Cheap image gen end-to-end, JSON out, file paths back
kie generate z-image -p "neon koi over wet asphalt" --out renders/ --json | jq -r '.files[]'

# 3. Seedance i2v: local image auto-uploads; cap spend; passthrough param
kie generate seedance-2-fast -p - -i ./ref.png --duration 5 --resolution 720p \
  --max-credits 200 --yes --param nsfw_checker=false --json < prompt.txt

# 4. Error recovery: wait timed out (exit 5) → resume the same task
kie status task_byte_123 --wait --timeout 600 --json

# 5. Ledger: what did I make today, and re-pull URLs
kie tasks --state success --limit 10 --json | jq -r '.result_urls[]'
```

## 15. Testing contract

- `tests/` pytest; httpx `MockTransport` — zero network, zero credits. Cover: envelope→exit-code
  map, cost math golden values (e.g. seedance-2 1080p no-input 8s = 816 credits), registry
  build_input (required/mutual-exclusion/type coercion), ledger append/update, CLI JSON shapes,
  dry-run never POSTs.
- Live smoke (`tests/live/`, opt-in `KIE_LIVE=1`): balance, pricing refresh, cost, dry-run,
  upload, ONE z-image generation (0.8 credits), status, download, tasks. HARD BUDGET ≤5 credits.
  NEVER any video model.
