---
name: model-audit
description: Operate an already running model observatory candidate through its versioned HTTP API or CLI to inspect route baseline coverage, explicitly start sampling, poll or cancel jobs, and explain statistical evidence separately from per-request declarations.
---

Use the candidate's `/api/audit/v1` backend; do not compute a second score. Default execution is zero-cost synthetic loopback. HTTPS `/v1` configuration is supported, with remote dispatch disabled unless the operator explicitly launches `serve` using stdin `allow_remote: true` after separate credential/cost authorization. Inspect capabilities.remote_execution_enabled; never enable it implicitly. This delivery was tested only on literal `http://127.0.0.1:<isolated-port>/v1` targets. Never use ports 18082–18085 or found login tokens. Real provider keys, live traffic and remote calibration require separate authorization; do not provision a key for the current zero-cost workflow.

Obtain the running instance URL and ephemeral local bearer authorization from the operator through a secure in-memory channel. No local repository context is required for direct HTTP. Do not guess URLs, read other sessions or persist credentials. All endpoints require exact loopback Host and matching Origin when present. A 401/403 is a stop, not a reason to bypass authorization.

1. `GET /api/audit/v1/capabilities`. Require `schema_version: 1`, record engine commit and protocols; use `data.conditions[protocol]` for explicit `chat`, `responses` or `anthropic` selection. A 503 means unavailable; previous green history does not prove current health.
2. `GET /api/audit/v1/coverage`. Match exact model ID, protocol and complete conditions. Check source, date, sample_count and hash. A relay's own claim is not trusted provenance. Unknown model, mismatched settings, fewer than 40 valid samples, or fewer than two compatible reference models means evidence insufficient. Explain this before proposing probes.
3. Only on an explicit sampling action, `POST /api/audit/v1/jobs` with `{"spec":{"endpoint":"http://127.0.0.1:<isolated-port>/v1","model":"A","protocol":"chat","samples":40,"timeout":1,"token_budget":8192,"conditions":<exact capabilities conditions>},"api_key":<in-memory synthetic endpoint key>}`. Do not include credentials in a URL, process argument, saved file, screenshot or report. No automatic retries or model summaries. The backend permits one active job, at most 200 samples plus one canary, individual wall-clock timeout .05–5s, and a 120s job deadline covering transport and engine workers. Reservation and reported token limits are both enforced; supplied usage is not provider identity proof.
4. Poll `GET /api/audit/v1/jobs/{id}` every five seconds; polling must never submit another job. `POST /api/audit/v1/jobs/{id}/cancel` with `{}` cancels future dispatch; the backend terminates an in-flight worker. Terminal states: completed, failed, cancelled. `GET /api/audit/v1/jobs` returns history. Do not join these reports to conversation records by timestamps.
5. Interpret `declaration` separately from `verdict`. 声明一致/冲突/缺失 refers only to response model fields. 未发现异常/发现偏离/证据不足 describes route samples under the bound endpoint + model + protocol + conditions + baseline hashes + timestamps. Preserve `engine_result.forgery.status` and the backend limitations. Report valid_samples, sample_attempts, requests, errors, skipped, failures and context checks. AIG's score, response model, signature shape or token counts alone do not authenticate weights, GPT-6, or an individual request.

For baseline import, `POST /api/audit/v1/baselines` accepts exactly `schema_version, source, model, protocol, date, conditions, samples` (40..10000 integers 1..355 from the defined prompt). Establish provenance independently before importing; the stored hash proves content integrity, not trust. Calibration is the same jobs API with `spec.mode="calibrate"` and explicit `spec.source="synthetic:<description>"`; default synthetic calls produce synthetic reference statistics only. Independently authorized remote calibration uses an explicit independently established source description, never relay self-report as official truth.

When a local checkout is available, CLI calls the same HTTP operations:

```text
python3 -m model_monitor.relay_audit capabilities --url http://127.0.0.1:<candidate-port>
python3 -m model_monitor.relay_audit coverage --url http://127.0.0.1:<candidate-port>
python3 -m model_monitor.relay_audit submit --url http://127.0.0.1:<candidate-port>
python3 -m model_monitor.relay_audit status --url http://127.0.0.1:<candidate-port> --id <id>
python3 -m model_monitor.relay_audit cancel --url http://127.0.0.1:<candidate-port> --id <id>
python3 -m model_monitor.relay_audit history --url http://127.0.0.1:<candidate-port>
```

Pass stdin JSON `{"auth_token":<in-memory local auth>}`; submit/import-baseline additionally require `"body":<API JSON body>`. Use a subprocess input stream or a secure pipe, never shell-expanded credentials in command arguments. CLI errors exit nonzero with redacted JSON. The maintained schema and startup/rollback instructions are at `model_monitor/relay_audit/schema.json` and `model_monitor/relay_audit/README.md` in the candidate checkout. Direct HTTP clients may discover fields through capabilities without those files.
