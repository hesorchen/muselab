"""Runtime-editable settings: provider API keys, defaults, model params.
GET returns current values with keys masked. PUT atomically rewrites .env and
refreshes os.environ so the changes take effect without restarting the server.
"""
from __future__ import annotations
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from .auth import require_token
# _locate_executable used to live in this module but is now also needed
# by main.py for the CLI version probe at /api/meta. Both modules import
# from settings.locate_executable; we keep the underscored alias here so
# existing call sites in this file continue to work unchanged.
from .settings import locate_executable as _locate_executable

MCP_CONFIG_PATH = Path(__file__).resolve().parent.parent / "mcp.json"
MCP_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "mcp.json.example"

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Path to the .env file we read/write at runtime. Defaults to the repo
# root's `.env`. The MUSELAB_ENV_PATH override is critical for test
# isolation — without it, tests/test_regressions.py calling
# PUT /api/settings would clobber the developer's real .env (every CI
# run silently overwrote the DEEPSEEK_API_KEY with "sk-test-key-12345"
# until 2026-05-24 when this guard was added). Production setups
# never need to set the env var; it's a test-only escape hatch.
ENV_PATH = Path(os.environ.get(
    "MUSELAB_ENV_PATH",
    str(Path(__file__).resolve().parent.parent / ".env"),
))

# Providers exposed in the settings UI. Derived from the EFFECTIVE catalog
# (endpoints.catalog() = built-ins + user overrides + custom providers) so a
# user-added provider — or an edited built-in — surfaces in Settings without
# a restart. Anthropic is added explicitly (Claude isn't in the catalog — it
# routes through `claude login` OAuth, not a third-party adapter).
#
# MUST be a function, not a module constant: catalog() reads provider_overrides
# .json from disk, so a constant snapshot taken at import would freeze the
# provider list and new providers wouldn't appear until the process restarted.
def _provider_keys() -> list[tuple[str, str, str]]:
    # Anthropic first — recommended primary provider. Empty here is fine if
    # the user authenticates via `claude login` Pro/Max OAuth instead.
    out: list[tuple[str, str, str]] = [("ANTHROPIC_API_KEY", "Anthropic (Claude API)", "claude-sonnet-4-6")]
    from . import endpoints as _ep
    for p in _ep.catalog():
        probe_model = p.models[0][0] if p.models else ""
        out.append((p.env_key, p.display, probe_model))
    return out


# Env names a user-created provider's auto-minted key follows. The provider
# CRUD route mints `MUSELAB_PROVIDER_<SLUG>_API_KEY`; we whitelist this shape
# in put_settings so the generic provider_keys map can carry the api-key for a
# brand-new provider even on the same request that creates it (before catalog()
# has the entry). Still tight enough that PATH / MUSELAB_TOKEN can't slip through.
_CUSTOM_ENV_RE = re.compile(r"^MUSELAB_PROVIDER_[A-Z0-9_]+_API_KEY$")

DEFAULT_KEYS = [
    "MUSELAB_DEFAULT_MODEL",
    "MUSELAB_DEFAULT_PERMISSION",
]


def _mask(v: str) -> str:
    """Mask an API key for display. Show first 4 + last 4 chars only."""
    if not v:
        return ""
    if len(v) <= 10:
        return "•" * len(v)
    return v[:4] + "•" * (len(v) - 8) + v[-4:]


class SettingsIn(BaseModel):
    # Provider keys — kept as individual fields for the 4 original providers
    # (FE still sends them this way for backwards compat). Plus a generic
    # `provider_keys` map for any provider added later — FE sends
    # `{"DEEPSEEK_API_KEY": "sk-...", "DASHSCOPE_API_KEY": "sk-..."}` etc.
    # The generic map wins if both forms set the same env var. Whitelist of
    # accepted env names lives in PROVIDER_KEYS (derived from CATALOG), so
    # a typo / unrelated env var like PATH can't be smuggled through here.
    # Each value semantics:
    #   None / unset → don't touch
    #   "" (empty)   → don't touch (FE legacy form)
    #   "_delete_"   → remove the env entry
    #   anything else → write that value
    anthropic_api_key: str | None = None
    deepseek_api_key: str | None = None
    zhipuai_api_key: str | None = None
    minimax_api_key: str | None = None
    provider_keys: dict[str, str] | None = None
    # Defaults
    default_model: str | None = None
    default_permission: str | None = None
    # (Removed 2026-05-28) notify_scheduled / notify_normal —
    # The 4-toggle notification panel collapsed to a single client-side
    # "notify me" switch. Subscription state IS the on/off; no per-class
    # server-side env-var gate needed. Both chat.py and scheduler.py now
    # rely on presence.recently_active() for "don't double-notify while
    # the user is actively at a device".
    # Per-provider visibility toggle — dict of {probe_model: true/false}.
    # true = disable (hide from model picker). Sent as a partial diff: only
    # the toggled provider appears in the dict, not all providers.
    provider_disabled: dict[str, bool] | None = None


# Serializes the whole read-merge-replace cycle below. The os.replace alone
# is atomic, but two concurrent writers (e.g. two browser tabs saving
# different settings) would each read the same baseline and the second
# replace would silently drop the first writer's keys.
_ENV_WRITE_LOCK = threading.Lock()


def _write_env(updates: dict[str, str]) -> None:
    """Atomically merge updates into .env. Keys with empty-string value get
    written as `KEY=` (allowed); to actually remove a key, pass None and we
    drop the line.

    Security: values are stripped of CR/LF before writing. A newline in a
    value would otherwise split into extra `KEY=VALUE` lines on the next
    load_dotenv, letting a caller inject arbitrary env vars (e.g. PATH,
    MUSELAB_HOST) through a single whitelisted key — defeating the
    PROVIDER_KEYS name whitelist in put_settings. API keys never contain
    newlines, so stripping is safe and also fixes the benign case of a
    pasted key with a trailing newline silently corrupting .env."""
    updates = {
        k: (v if v is None else v.replace("\r", "").replace("\n", ""))
        for k, v in updates.items()
    }
    with _ENV_WRITE_LOCK:
        lines: list[str] = []
        if ENV_PATH.exists():
            lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

        out: list[str] = []
        written: set[str] = set()
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                out.append(line)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                new_v = updates[key]
                if new_v is None:
                    # drop line entirely
                    continue
                out.append(f"{key}={new_v}")
                written.add(key)
            else:
                out.append(line)

        # Append new keys not seen above.
        for k, v in updates.items():
            if v is None or k in written:
                continue
            out.append(f"{k}={v}")

        # Atomic write via temp + rename.
        fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(ENV_PATH.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(out).rstrip("\n") + "\n")
            os.replace(tmp, ENV_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        # Refresh in-process env so the change takes effect immediately.
        for k, v in updates.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# Single source of truth for setting defaults — keys = env var name, values
# = string default returned when env is unset. Both GET (echoes the value
# to the frontend) and PUT (compares incoming value against the "current"
# value to detect actual changes) must use the SAME default, otherwise
# saving a never-edited setting wrongly counts as "changed" — the env is
# unset, GET reported the default to the FE, FE sends the default back,
# PUT compares to None and sees a diff. (2026-05-23 — second pass at the
# "saved 7 settings" bug. First pass forgot to use defaults in compare.)
_SETTING_DEFAULTS: dict[str, str] = {
    "MUSELAB_MODEL":                "claude-sonnet-4-6",
    "MUSELAB_DEFAULT_MODEL":        "claude-sonnet-4-6",
    "MUSELAB_DEFAULT_PERMISSION":   "bypassPermissions",
}


def _current(env_key: str) -> str:
    """Read the current effective value of a setting env, falling back to the
    canonical default when unset. Used by put_settings's change-detection."""
    return os.environ.get(env_key, _SETTING_DEFAULTS.get(env_key, ""))


@router.get("", dependencies=[Depends(require_token)])
def get_settings() -> dict:
    """Return current settings with API keys masked."""
    from . import endpoints as _ep
    raw_disabled = os.environ.get("MUSELAB_DISABLED_PROVIDERS", "").strip()
    disabled_models = set(raw_disabled.split(",")) if raw_disabled else set()
    providers: list[dict] = []
    # Anthropic — special OAuth / API-key card. `editable: false` keeps the
    # simple key-only row (no endpoint/prefix/key editor), but its MODEL LIST
    # IS user-editable: `models_editable: true` tells the UI to render the
    # model-list sub-editor, and `is_overridden` lights up "restore default".
    av = os.environ.get("ANTHROPIC_API_KEY", "")
    claude_models = _ep.anthropic_models()
    providers.append({
        "kind": "anthropic",
        "id": "anthropic",
        "env_key": "ANTHROPIC_API_KEY",
        "display": "Anthropic (Claude API)",
        "configured": bool(av),
        "masked": _mask(av),
        "probe_model": claude_models[0] if claude_models else "claude-sonnet-4-6",
        "disabled": False,
        "base_url": "", "prefix": "",
        "models": claude_models,
        "is_builtin": True, "is_overridden": _ep.anthropic_models_overridden(),
        "editable": False, "models_editable": True,
    })
    # Third-party providers — full editor payload. endpoints owns the catalog
    # shape (id / base_url / prefix / models / builtin-vs-override); we layer on
    # the env-derived bits (configured / masked / disabled) it can't see.
    for m in _ep.provider_meta():
        v = os.environ.get(m["env_key"], "")
        # `disabled` honours BOTH the stable id (new form) and the legacy
        # first-model-id form earlier builds wrote, so the toggle survives the
        # disabled-key migration without resetting the user's hidden providers.
        disabled = (m["id"] in disabled_models) or (
            m["probe_model"] and m["probe_model"] in disabled_models)
        providers.append({
            "kind": "third_party",
            "id": m["id"],
            "env_key": m["env_key"],
            "display": m["display"],
            "configured": bool(v),
            "masked": _mask(v),
            "probe_model": m["probe_model"],
            "disabled": disabled,
            "base_url": m["base_url"],
            "prefix": m["prefix"],
            "models": m["models"],
            "supports_thinking": m.get("supports_thinking", True),
            "is_builtin": m["is_builtin"],
            "is_overridden": m["is_overridden"],
            "editable": True,
        })
    return {
        "providers": providers,
        "defaults": {
            # Model: prefer MUSELAB_DEFAULT_MODEL, fall back to MUSELAB_MODEL,
            # then to the canonical default. The two-key dance exists because
            # chat.py historically read MUSELAB_MODEL.
            "model": os.environ.get(
                "MUSELAB_DEFAULT_MODEL",
                os.environ.get("MUSELAB_MODEL", _SETTING_DEFAULTS["MUSELAB_MODEL"])),
            "permission": _current("MUSELAB_DEFAULT_PERMISSION"),
        },
        # `params` retained as an empty dict for FE backwards-compat — old
        # builds spread `d.params` into draftParams and would TypeError on
        # null. Will drop once all clients are >= 2026-05-28.
        "params": {},
    }


@router.put("", dependencies=[Depends(require_token)])
def put_settings(req: SettingsIn) -> dict:
    """Write any provided fields to .env and refresh os.environ in-process."""
    updates: dict[str, str] = {}

    # Provider keys: empty string means "keep current"; we ignore them.
    # Explicit non-empty string writes; "_delete_" sentinel removes.
    # Legacy form: individual snake-case fields for the original 4
    # providers. Kept for FE backwards-compat.
    legacy_key_map = {
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "deepseek_api_key": "DEEPSEEK_API_KEY",
        "zhipuai_api_key": "ZHIPUAI_API_KEY",
        "minimax_api_key": "MINIMAX_API_KEY",
    }
    for field, env_name in legacy_key_map.items():
        v = getattr(req, field)
        if v is None or v == "":   # skip unchanged
            continue
        if v == "_delete_":
            updates[env_name] = None  # type: ignore[assignment]  # signals removal
        else:
            updates[env_name] = v
    # Generic form: `provider_keys: {ENV_NAME: value}`. Reuses PROVIDER_KEYS
    # as a whitelist so the user can't shove arbitrary env names through
    # this endpoint (the endpoint runs with token auth, but defence-in-depth
    # keeps a credential-stealing route from existing on principle —
    # otherwise a future XSS could PUT `PATH=/tmp/evil` here).
    if req.provider_keys is not None:
        allowed_envs = {k for k, _, _ in _provider_keys()}
        for env_name, v in req.provider_keys.items():
            # Accept a catalog provider's env_key, OR the auto-minted shape a
            # brand-new custom provider uses (so its key can ride along on the
            # creating request before catalog() lists it). Everything else is
            # dropped — no PATH / MUSELAB_TOKEN smuggling.
            if env_name not in allowed_envs and not _CUSTOM_ENV_RE.match(env_name):
                continue   # silently drop typo'd / disallowed envs
            if not isinstance(v, str) or v == "":
                continue
            if v == "_delete_":
                updates[env_name] = None  # type: ignore[assignment]
                continue
            # Defence: refuse to save a string that looks like our own
            # mask (contains "•"). Without this, a bug in the FE that
            # accidentally piped p.masked into draftKeys[…] would
            # silently turn the user's real key into a string of
            # bullets — unrecoverable without a backup. The mask uses
            # U+2022 BULLET; legitimate API keys never contain it.
            if "•" in v:
                continue
            updates[env_name] = v

    # Per-field "did it actually change?" gate. The frontend sends every
    # draftDefaults / draftParams field on every save (it doesn't track
    # which ones the user edited), so without this gate `req.X is not None`
    # treats unchanged fields as updates and the response's `updated`
    # list inflates. Symptom: user only flipped the language toggle (which
    # is pure client-side localStorage, doesn't even hit this endpoint) and
    # the toast says "已修改 7 项设置" — model writes 2 env keys, plus
    # permission, all just re-asserting their current value (2026-05-23
    # user feedback). Comparing against _current() (which falls back to
    # the canonical default for unset envs) keeps the count honest AND
    # avoids pointless .env rewrites on no-op saves.
    #
    # CRITICAL: must use _current(), not os.environ.get(). When an env is
    # unset, os.environ.get returns None, GET endpoint returned the default
    # to the FE, FE sends the default back, naive `None != default` → diff
    # → "changed" → bogus count. _current() bakes in the default so the
    # round-trip is comparable.
    def _changed(env_key: str, new_val: str) -> bool:
        return _current(env_key) != new_val

    if req.default_model is not None:
        # Write both keys so `chat.py` (reads `settings.MODEL` → `MUSELAB_MODEL`)
        # and the settings GET endpoint (reads `MUSELAB_DEFAULT_MODEL`) agree.
        # Without this, the user changed "default model" in Settings and saw it
        # echoed back, but new sessions still used the .env's `MUSELAB_MODEL`.
        # Count as ONE updated field (both keys flip together — they're an
        # implementation detail, not two separate settings).
        if (_changed("MUSELAB_DEFAULT_MODEL", req.default_model)
                or _changed("MUSELAB_MODEL", req.default_model)):
            updates["MUSELAB_DEFAULT_MODEL"] = req.default_model
            updates["MUSELAB_MODEL"] = req.default_model
    if req.default_permission is not None and _changed(
            "MUSELAB_DEFAULT_PERMISSION", req.default_permission):
        updates["MUSELAB_DEFAULT_PERMISSION"] = req.default_permission
    if req.provider_disabled is not None:
        raw = os.environ.get("MUSELAB_DISABLED_PROVIDERS", "").strip()
        disabled_models = set(raw.split(",")) if raw else set()
        changed = False
        for model_id, disable in req.provider_disabled.items():
            if disable:
                if model_id not in disabled_models:
                    disabled_models.add(model_id)
                    changed = True
            else:
                if model_id in disabled_models:
                    disabled_models.discard(model_id)
                    changed = True
        if changed:
            if disabled_models:
                updates["MUSELAB_DISABLED_PROVIDERS"] = ",".join(sorted(disabled_models))
            else:
                updates["MUSELAB_DISABLED_PROVIDERS"] = None  # type: ignore[assignment]  # remove key

    _write_env(updates)
    # The `chat.py` module captured `MODEL` at import time; if we just touched
    # `MUSELAB_MODEL` here, existing imports won't see the new value. Push it
    # back so subsequent stream() calls pick up the new default.
    if "MUSELAB_MODEL" in updates:
        from . import settings as _settings
        _settings.MODEL = updates["MUSELAB_MODEL"]
        from . import chat as _chat
        _chat.MODEL = updates["MUSELAB_MODEL"]
    # `updated_count` is the user-facing tally for the "Saved N settings" toast.
    # Differs from len(updated) in one case: model changes write two env keys
    # (MUSELAB_MODEL + MUSELAB_DEFAULT_MODEL — they're an implementation detail
    # kept in sync) but represent ONE setting from the user's perspective. Without
    # this de-dup the toast says "Saved 2 settings" when the user only changed
    # the model dropdown. Old `updated` list is preserved unchanged for any
    # caller that wants the raw env-key list.
    updated_keys = list(updates.keys())
    paired_model_keys = {"MUSELAB_MODEL", "MUSELAB_DEFAULT_MODEL"}
    model_pair_count = sum(1 for k in updated_keys if k in paired_model_keys)
    updated_count = len(updated_keys) - max(0, model_pair_count - 1)
    return {
        "ok": True,
        "updated": updated_keys,
        "updated_count": updated_count,
    }


# ====== Provider catalog management ======
#
# The effective provider list = built-in defaults + user overrides, all owned
# by endpoints.py (persisted in provider_overrides.json). These routes are the
# write side of the Settings provider editor: create / edit / delete / restore.
# API keys are NEVER stored here — they go to .env via _write_env and are only
# ever returned masked. Provider metadata (endpoint / prefix / models) is not
# secret and lives in the JSON override store.


class ProviderIn(BaseModel):
    # id None → create a new custom provider (endpoints mints a stable id).
    # id set → edit that provider (built-in override or existing custom).
    id: str | None = None
    base_url: str = Field(..., min_length=1)
    prefix: str = Field(..., min_length=1)
    display: str | None = None
    env_key: str | None = None          # None → auto-mint for new providers
    models: list[str] = Field(default_factory=list)
    # Optional: write the api-key to .env in the SAME request that creates /
    # edits the provider. "" / None → leave .env untouched; "_delete_" → remove.
    api_key: str | None = None


class ProviderIdIn(BaseModel):
    # id carried in the body (not the URL path) so colons in built-in ids
    # like "b:deepseek-" never need URL-encoding gymnastics.
    id: str = Field(..., min_length=1)


@router.post("/providers", dependencies=[Depends(require_token)])
def upsert_provider(req: ProviderIn) -> dict:
    """Create or edit a provider override. Returns the saved provider's stable
    id + env_key so the FE can immediately PUT the api-key (or read it back).
    Validation errors (bad URL, dup prefix, model not prefixed) → 422."""
    from . import endpoints as _ep
    try:
        p = _ep.upsert_provider(
            pid=req.id,
            base_url=req.base_url,
            prefix=req.prefix,
            display=req.display or req.prefix,
            env_key=req.env_key or "",
            models=req.models,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    # Optional api-key write in the same call. Reuse the same mask-guard as
    # put_settings so a leaked mask string can't overwrite a real key.
    if req.api_key is not None:
        if req.api_key == "_delete_":
            _write_env({p.env_key: None})  # type: ignore[dict-item]
        elif req.api_key and "•" not in req.api_key:
            _write_env({p.env_key: req.api_key})
    return {"ok": True, "id": p.id, "env_key": p.env_key,
            "configured": bool(os.environ.get(p.env_key))}


@router.post("/providers/delete", dependencies=[Depends(require_token)])
def delete_provider(req: ProviderIdIn) -> dict:
    """Delete a provider. Built-ins are tombstoned (won't reappear) until
    restored; custom providers are dropped outright. The api-key stays in
    .env untouched — removing credentials is a separate, explicit action."""
    from . import endpoints as _ep
    changed = _ep.delete_provider(req.id)
    return {"ok": True, "changed": changed}


@router.post("/providers/restore", dependencies=[Depends(require_token)])
def restore_provider(req: ProviderIdIn) -> dict:
    """Restore a built-in provider to factory defaults (drop its override +
    tombstone). No-op for custom providers — they have no default to restore
    to (the FE should offer Delete instead). id="anthropic" reverts the
    Claude model list to the built-in default."""
    from . import endpoints as _ep
    changed = _ep.restore_provider(req.id)
    return {"ok": True, "changed": changed}


class AnthropicModelsIn(BaseModel):
    # Claude's model list (ids only — labels are derived). Anthropic keeps its
    # special key/OAuth auth row, so this is the one editable knob it exposes.
    models: list[str] = Field(default_factory=list)


@router.post("/providers/anthropic-models", dependencies=[Depends(require_token)])
def set_anthropic_models(req: AnthropicModelsIn) -> dict:
    """Override the Claude model list shown in the picker. Validation (empty,
    non-`claude-` id, dup) → 422. Restore the default via /providers/restore
    with id='anthropic'."""
    from . import endpoints as _ep
    try:
        _ep.set_anthropic_models(req.models)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    return {"ok": True, "models": _ep.anthropic_models()}


# ====== MCP server management ======
#
# Storage shape on disk (mcp.json) — two transports:
#   stdio (local subprocess):
#     {"name": {"command": "...", "args": [...], "env": {...},
#                "disabled": false}}
#   http/sse (remote connector, à la Claude app's account-level connectors):
#     {"name": {"type": "http", "url": "https://...",
#                "headers": {...}, "disabled": false}}
# `disabled` is a muselab-local field — when true, the server is omitted from
# the dict we hand to ClaudeAgentOptions so the SDK doesn't connect to it.
# The SDK passes `url`-shaped specs straight through to a remote MCP endpoint
# (see chat.py mcp_dict build, which keys on `command` OR `url`).


class MCPServerSpec(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    # Transport. Inferred when omitted: a `url` ⇒ "http", else "stdio".
    type: str | None = None
    # --- stdio (local subprocess) ---
    command: str | None = Field(default=None)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # --- http / sse (remote connector) ---
    url: str | None = Field(default=None)
    headers: dict[str, str] = Field(default_factory=dict)
    disabled: bool = False

    @model_validator(mode="after")
    def _check_transport(self) -> "MCPServerSpec":
        """Exactly one transport must be specified. A `url` means remote
        (http/sse); a `command` means local stdio. Normalise `type` so the
        on-disk entry and the SDK both see an explicit transport tag."""
        has_url = bool((self.url or "").strip())
        has_cmd = bool((self.command or "").strip())
        if has_url and has_cmd:
            raise ValueError(
                "specify either `command` (stdio) or `url` (remote), not both")
        if has_url:
            self.type = self.type or "http"
            if self.type not in ("http", "sse"):
                raise ValueError(
                    f"remote MCP `type` must be 'http' or 'sse', got {self.type!r}")
        elif has_cmd:
            self.type = self.type or "stdio"
            if self.type != "stdio":
                raise ValueError(
                    f"stdio MCP must have type 'stdio', got {self.type!r}")
        else:
            raise ValueError("either `command` (stdio) or `url` (remote) is required")
        return self


def _load_mcp() -> dict:
    if not MCP_CONFIG_PATH.exists():
        return {"mcpServers": {}}
    try:
        d = json.loads(MCP_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            return {"mcpServers": {}}
        d.setdefault("mcpServers", {})
        return d
    except (json.JSONDecodeError, OSError):
        return {"mcpServers": {}}


# ====== Claude Code MCP auto-detection (2026-05-23) ======
# muselab is positioned as a Claude Code replacement, so we want every MCP
# the user has already configured via `claude mcp add` / `~/.claude.json` /
# project `.mcp.json` to "just work" without re-entering. The four standard
# Claude Code MCP config locations are scanned at session-build time; any
# servers found get merged into the SDK options. muselab's own mcp.json
# always wins on name conflict so the user can override or disable any
# inherited entry from the muselab UI.

_CLAUDE_USER_JSON = Path.home() / ".claude.json"
_CLAUDE_USER_SETTINGS = Path.home() / ".claude" / "settings.json"


def _load_external_mcp_sources() -> dict[str, dict]:
    """Scan Claude Code's standard MCP config locations and return a flat
    {server_name: spec_dict_with_source} mapping. The `_source` field is
    added so the UI can show a "from Claude Code (user-global)" tag.

    Priority order (later sources override earlier on name conflict):
      1. ~/.claude.json top-level `mcpServers`        — user-global
      2. ~/.claude/settings.json `mcpServers`         — newer user-global schema
      3. ~/.claude.json projects.{archive}.mcpServers — per-project user-level
      4. <archive>/.mcp.json                          — per-project shared
                                                        (typically git-committed)

    Robust: any unreadable / malformed file is silently skipped so a broken
    Claude Code config doesn't break muselab boot. Each "skip" hits stderr
    once so the user can debug if needed.
    """
    from .settings import ROOT
    out: dict[str, dict] = {}

    def _add(name: str, spec: Any, source: str) -> None:
        if not isinstance(spec, dict) or not name:
            return
        out[name] = {**spec, "_source": source}

    def _ingest(payload: Any, mc_path: str, source: str) -> None:
        """Walk a dict-shaped JSON and slot every server under `mcpServers`
        into the output. `mc_path` is just for error messages."""
        if not isinstance(payload, dict):
            return
        servers = payload.get("mcpServers")
        if not isinstance(servers, dict):
            return
        for name, spec in servers.items():
            _add(name, spec, source)

    # 1. ~/.claude.json (top-level user-global)
    if _CLAUDE_USER_JSON.exists():
        try:
            cc = json.loads(_CLAUDE_USER_JSON.read_text(encoding="utf-8"))
            _ingest(cc, str(_CLAUDE_USER_JSON), "claude_user_global")
        except (json.JSONDecodeError, OSError) as e:
            sys.stderr.write(
                f"[mcp] could not read {_CLAUDE_USER_JSON}: "
                f"{type(e).__name__}: {e}\n")
        else:
            # 3. Per-project section inside the same file. Match by absolute
            # archive ROOT — Claude Code keys projects by exact path string.
            projects = cc.get("projects") if isinstance(cc, dict) else None
            if isinstance(projects, dict):
                proj_entry = projects.get(str(ROOT))
                if isinstance(proj_entry, dict):
                    _ingest(proj_entry, f"{_CLAUDE_USER_JSON}#projects.{ROOT}",
                             "claude_user_project")

    # 2. ~/.claude/settings.json — newer schema. Some installs only have this.
    if _CLAUDE_USER_SETTINGS.exists():
        try:
            cs = json.loads(_CLAUDE_USER_SETTINGS.read_text(encoding="utf-8"))
            _ingest(cs, str(_CLAUDE_USER_SETTINGS), "claude_user_settings")
        except (json.JSONDecodeError, OSError) as e:
            sys.stderr.write(
                f"[mcp] could not read {_CLAUDE_USER_SETTINGS}: "
                f"{type(e).__name__}: {e}\n")

    # 4. <archive>/.mcp.json — convention for project-shared, git-committed MCP.
    proj_mcp = ROOT / ".mcp.json"
    if proj_mcp.exists():
        try:
            pm = json.loads(proj_mcp.read_text(encoding="utf-8"))
            _ingest(pm, str(proj_mcp), "archive_project")
        except (json.JSONDecodeError, OSError) as e:
            sys.stderr.write(
                f"[mcp] could not read {proj_mcp}: "
                f"{type(e).__name__}: {e}\n")

    return out


def has_claude_ai_connectors() -> bool:
    """True if Claude Code has any claude.ai-managed remote connector
    (Gmail / Calendar / Drive / IBKR / …) registered.

    These connectors are a DIFFERENT class from `mcpServers` entries: the CLI
    brings them up over its own `claudeai-proxy` transport and they never
    appear under any `mcpServers` key — so `_load_external_mcp_sources()`
    (which only scans `mcpServers`) is blind to them. We detect them via the
    `claudeAiMcpEverConnected` marker Claude Code writes into ~/.claude.json.

    Why it matters: the wedge-readiness gate (`chat._await_mcp_ready`) is
    skipped entirely when `_has_enabled_external_mcp()` is False. On an install
    whose ONLY external MCP is a claude.ai connector, the old scanner returned
    False → the gate never ran → the connector connected mid-first-turn and
    wedged the thinking block. Treating "ever connected a claude.ai connector"
    as "external MCP present" keeps the gate armed for these users.
    """
    try:
        if not _CLAUDE_USER_JSON.exists():
            return False
        cc = json.loads(_CLAUDE_USER_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    ever = cc.get("claudeAiMcpEverConnected") if isinstance(cc, dict) else None
    return isinstance(ever, list) and len(ever) > 0


def _load_mcp_merged() -> dict[str, dict]:
    """Final {name: spec} mapping after merging muselab's own mcp.json with
    every Claude Code MCP source. muselab's entries override external ones
    on name conflict — that's the override channel for "I configured this
    in Claude Code but I want it disabled / customised in muselab".

    Special-case: a muselab entry that ONLY carries {disabled: bool} (no
    `command`) is treated as an override-flag on the external entry: the
    full external spec is kept, just with `disabled` flipped. Without this
    the user couldn't disable a Claude Code-only MCP without re-entering
    its full command + args in muselab's mcp.json.

    Every returned spec carries a `_source` field for UI display:
      "muselab" / "claude_user_global" / "claude_user_settings" /
      "claude_user_project" / "archive_project".
    """
    external = _load_external_mcp_sources()
    own = _load_mcp().get("mcpServers") or {}
    merged: dict[str, dict] = dict(external)
    for name, spec in own.items():
        if not isinstance(spec, dict):
            continue
        # Override-only entry: a pure {disabled: true|false} stub with NO
        # transport of its own (neither `command` for stdio nor `url` for
        # remote). Layer it on top of the existing external spec. A full
        # muselab entry — including a remote one, which also lacks `command`
        # — must NOT be mistaken for a stub, or it'd be discarded in favour
        # of the external entry (regression caught 2026-05-30).
        is_stub = ("command" not in spec and "url" not in spec
                   and "disabled" in spec)
        if is_stub and name in merged:
            merged[name] = {**merged[name], "disabled": bool(spec["disabled"])}
            # Note: keeps `_source` from the external entry so UI shows
            # original source + an "(overridden)" tag if it wants.
            merged[name]["_overridden_by_muselab"] = True
        else:
            # Full muselab entry — replaces external completely.
            merged[name] = {**spec, "_source": "muselab"}
    return merged


def _save_mcp(cfg: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix="mcp.", suffix=".json",
                                dir=str(MCP_CONFIG_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, MCP_CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_examples() -> list[dict]:
    """Return preset/example servers from mcp.json.example, if present."""
    if not MCP_EXAMPLE_PATH.exists():
        return []
    try:
        d = json.loads(MCP_EXAMPLE_PATH.read_text(encoding="utf-8"))
        items = []
        for name, spec in (d.get("mcpServers") or {}).items():
            url = spec.get("url", "")
            items.append({
                "name": name,
                "type": spec.get("type") or ("http" if url else "stdio"),
                "command": spec.get("command", ""),
                "args": spec.get("args", []),
                "env": spec.get("env", {}),
                "url": url,
                "headers": spec.get("headers", {}),
                "description": spec.get("description", ""),
            })
        return items
    except (json.JSONDecodeError, OSError):
        return []


@router.get("/mcp", dependencies=[Depends(require_token)])
def get_mcp_servers() -> dict:
    """List every MCP server muselab knows about — muselab's own mcp.json
    PLUS Claude Code's standard config locations (auto-detected). The
    `source` field tells the UI where each came from so users can see at
    a glance which MCPs are inherited from Claude Code vs. configured
    explicitly in muselab."""
    merged = _load_mcp_merged()
    servers = []
    for name, spec in merged.items():
        # Transport: explicit `type`, else inferred from shape (url ⇒ remote).
        url = spec.get("url", "")
        transport = spec.get("type") or ("http" if url else "stdio")
        servers.append({
            "name": name,
            "type": transport,
            "command": spec.get("command", ""),
            "args": spec.get("args", []),
            "env": {k: _mask(v) for k, v in (spec.get("env") or {}).items()},
            # Remote connector fields. URL is not a secret (shown plain); header
            # values are masked like env (often carry bearer tokens).
            "url": url,
            "headers": {k: _mask(v) for k, v in (spec.get("headers") or {}).items()},
            "disabled": bool(spec.get("disabled", False)),
            # Source tag for UI badge. "muselab" = explicit muselab entry;
            # everything else came from a Claude Code config file.
            "source": spec.get("_source", "muselab"),
            # When an external entry is disabled-overridden by a muselab
            # stub entry, the UI may want to show "disabled by you" instead
            # of "this MCP is broken".
            "overridden": bool(spec.get("_overridden_by_muselab", False)),
        })
    # Sort: muselab-source first (user-curated), then external by name.
    servers.sort(key=lambda s: (s["source"] != "muselab", s["name"].lower()))
    return {"servers": servers, "examples": _load_examples()}


@router.put("/mcp/{name}", dependencies=[Depends(require_token)])
def upsert_mcp_server(name: str, spec: MCPServerSpec) -> dict:
    """Create or replace one MCP server entry in muselab's own mcp.json.
    External (Claude Code) entries are NOT touched — they live in their
    own files; muselab only owns its mcp.json. If `name` happens to also
    exist in a Claude Code config, this muselab entry will win in the
    merged view (see _load_mcp_merged)."""
    # The URL path `name` is the authoritative key. MCPServerSpec also
    # carries a required `name` field; if a client sends a body whose
    # `name` disagrees with the path (e.g. a frontend rename typo), we
    # used to silently honour the path and discard the body name — a
    # rename could then write the wrong entry with no error. Reject the
    # mismatch loudly instead of guessing intent.
    if spec.name != name:
        raise HTTPException(
            422,
            f"body name {spec.name!r} does not match URL name {name!r}",
        )
    cfg = _load_mcp()

    # Defence (mirror of put_settings' "•" guard): GET /mcp masks secret values
    # (env values, header values) via _mask() (U+2022 BULLET). A FE that PUTs an
    # unchanged entry back would otherwise overwrite the real secret with bullets
    # — unrecoverable. For any value that still looks masked, recover the real
    # value from the current merged view (the same source the mask was derived
    # from); if none exists, drop the key rather than persist bullets.
    def _unmask(new_map: dict, field: str) -> dict:
        out = dict(new_map or {})
        if any(isinstance(v, str) and "•" in v for v in out.values()):
            existing = (_load_mcp_merged().get(name) or {}).get(field) or {}
            for k, v in list(out.items()):
                if isinstance(v, str) and "•" in v:
                    if k in existing:
                        out[k] = existing[k]
                    else:
                        out.pop(k)
        return out

    if spec.url:
        # Remote connector (http/sse). No command/args/env on disk.
        cfg["mcpServers"][name] = {
            "type": spec.type or "http",
            "url": spec.url.strip(),
            "headers": _unmask(spec.headers, "headers"),
            "disabled": spec.disabled,
        }
    else:
        # Local stdio subprocess.
        cfg["mcpServers"][name] = {
            "command": spec.command,
            "args": spec.args,
            "env": _unmask(spec.env, "env"),
            "disabled": spec.disabled,
        }
    _save_mcp(cfg)
    return {"ok": True, "name": name}


@router.delete("/mcp/{name}", dependencies=[Depends(require_token)])
def delete_mcp_server(name: str) -> dict:
    """Remove `name` from muselab's own mcp.json. Does NOT touch Claude
    Code's config files — if the same name exists there too, the external
    entry will reappear in the merged list (no longer overridden). To
    "hide" an external MCP from muselab, use the toggle endpoint instead
    (it writes a stub override into muselab's mcp.json)."""
    cfg = _load_mcp()
    if name not in (cfg.get("mcpServers") or {}):
        raise HTTPException(404, f"MCP server not found: {name}")
    del cfg["mcpServers"][name]
    _save_mcp(cfg)
    return {"ok": True, "name": name}


class MCPToggleReq(BaseModel):
    disabled: bool


@router.patch("/mcp/{name}/toggle", dependencies=[Depends(require_token)])
async def toggle_mcp_server(name: str, req: MCPToggleReq) -> dict:
    """Toggle the enabled/disabled state of an MCP server.

    Three cases:
      a. Entry exists in muselab's mcp.json with full command → flip its
         `disabled` flag in place.
      b. Entry exists only in an external (Claude Code) config → write a
         stub override `{"disabled": <bool>}` into muselab's mcp.json
         under the same name. The merge step (_load_mcp_merged) layers
         the disabled flag on top of the external spec so the UI toggle
         "just works" without re-entering command/args.
      c. Name doesn't exist anywhere → 404.
    """
    # _load_mcp / _load_mcp_merged / _save_mcp are all synchronous disk I/O —
    # and _load_mcp_merged pulls in ~/.claude.json (can be multi-MB of project
    # history), parsing 3-4 files. Off-load each so toggling an MCP server from
    # Settings can't stall the event loop (and every concurrent SSE stream) for
    # tens-to-hundreds of ms. (perf: RED — api_settings.py toggle_mcp_server)
    cfg = await asyncio.to_thread(_load_mcp)
    own_servers = cfg.setdefault("mcpServers", {})
    merged = await asyncio.to_thread(_load_mcp_merged)
    if name not in merged:
        raise HTTPException(404, f"MCP server not found: {name}")

    if name in own_servers:
        own_servers[name]["disabled"] = req.disabled
    else:
        # External-only entry — write a stub override. _load_mcp_merged
        # recognises {disabled: ...} without `command` and layers it
        # onto the external spec.
        own_servers[name] = {"disabled": req.disabled}
    await asyncio.to_thread(_save_mcp, cfg)
    # Apply to every live SDK client too — otherwise the toggle would only
    # take effect on the next client rebuild, leaving the user staring at a
    # toggled-off server that still answers tool calls. SDK exposes
    # client.toggle_mcp_server() for exactly this case.
    from . import chat as _chat
    enabled = not req.disabled
    propagated: list[str] = []
    errors: list[str] = []
    async with _chat._lock:
        live = list(_chat._clients.items())
    for key, client in live:
        try:
            await client.toggle_mcp_server(name, enabled)
            propagated.append(f"{key[0]}@{key[1]}")
        except Exception as e:
            errors.append(f"{key}: {type(e).__name__}: {e}")
    return {
        "ok": True, "name": name, "disabled": req.disabled,
        "propagated": propagated,
        "errors": errors,
    }


@router.post("/mcp/{name}/reconnect", dependencies=[Depends(require_token)])
async def reconnect_mcp_server(name: str) -> dict:
    """Force every live SDK client to re-establish the MCP connection for this
    server. Useful when an MCP process died / network blip — SDK's
    client.reconnect_mcp_server() restarts the transport without rebuilding
    the whole client."""
    from . import chat as _chat
    reconnected: list[str] = []
    errors: list[str] = []
    async with _chat._lock:
        live = list(_chat._clients.items())
    if not live:
        return {"ok": True, "reconnected": [], "errors": [], "note": "no live client"}
    for key, client in live:
        try:
            await client.reconnect_mcp_server(name)
            reconnected.append(f"{key[0]}@{key[1]}")
        except Exception as e:
            errors.append(f"{key}: {type(e).__name__}: {e}")
    return {"ok": True, "reconnected": reconnected, "errors": errors}


@router.get("/mcp/status", dependencies=[Depends(require_token)])
async def mcp_status() -> dict:
    """Aggregate MCP server status from every live SDK client (each may have
    its own connection state). Returns per-client breakdown so the UI can
    show which session's MCP is borked when reconnect is needed."""
    from . import chat as _chat
    async with _chat._lock:
        live = list(_chat._clients.items())
    out: list[dict] = []
    for key, client in live:
        # Cache key is (sid, model, effort) — 3-tuple since 2026-05-21.
        # Unpacking into 2 vars would crash with ValueError; index instead.
        sid, model = key[0], key[1]
        try:
            status = await client.get_mcp_status()
            out.append({"session_id": sid, "model": model, "status": status})
        except Exception as e:
            out.append({"session_id": sid, "model": model,
                         "error": f"{type(e).__name__}: {e}"})
    return {"clients": out}


# ====== Skill discovery ======
#
# We don't enable/disable skills individually here — the SDK takes
# `skills="all"` or a list. We expose what's discoverable so users can browse.

SKILL_USER_DIR = Path.home() / ".claude" / "skills"
SKILL_PROJECT_DIR = Path(__file__).resolve().parent.parent / "skills"
# Plugin skills live two levels deeper under marketplaces — each marketplace
# holds N plugins, each plugin can ship its own skills/ subdir. Discovered
# at request time (no caching) so newly installed plugins surface without
# a muselab restart.
SKILL_PLUGIN_ROOT = Path.home() / ".claude" / "plugins" / "marketplaces"


def _strip_yaml_scalar(v: str) -> str:
    """Unwrap a single-line YAML scalar value the hand parser collected.

    Only the two common cases matter for SKILL frontmatter: a value fully
    wrapped in matching single or double quotes (then unescape the doubled /
    backslash forms), or a bare scalar with a trailing `# comment`. We
    deliberately do NOT try to be a full YAML engine here — that's what the
    optional PyYAML fast path above is for."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        inner = v[1:-1]
        if v[0] == '"':
            return inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner.replace("''", "'")
    return v.strip('"\'')


def _parse_skill_md(p: Path) -> dict | None:
    """Parse the YAML frontmatter of a SKILL.md (or skill.md).

    Prefers PyYAML when it's importable (it's a transitive dependency of
    the toolchain, not a declared one — so we treat it as best-effort and
    fall back to a small hand parser when absent). This fixes the common
    breakages of the old hand parser: quoted values containing colons,
    escaped / doubled quotes, and `key: value  # comment` trailers."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return {"name": p.parent.name, "description": ""}
    end = text.find("\n---", 3)
    if end == -1:
        return {"name": p.parent.name, "description": ""}
    fm = text[3:end].strip()
    out: dict = {"name": p.parent.name, "description": ""}
    # Fast path: a real YAML parser if available.
    try:
        import yaml  # type: ignore
        parsed = yaml.safe_load(fm)
        if isinstance(parsed, dict):
            for k, v in parsed.items():
                if isinstance(k, str):
                    out[k] = v if isinstance(v, str) else ("" if v is None else str(v))
            out.setdefault("name", p.parent.name)
            return out
    except Exception:
        # ImportError (yaml absent) or any malformed-YAML error → fall back
        # to the tolerant hand parser below, which never raises.
        pass
    cur_key = None
    cur_val: list[str] = []
    for line in fm.splitlines():
        if not line.strip():
            continue
        if line.startswith(" ") and cur_key:  # continuation
            cur_val.append(line.strip())
            continue
        if cur_key:
            out[cur_key] = _strip_yaml_scalar(" ".join(cur_val))
        if ":" in line:
            k, _, v = line.partition(":")
            cur_key = k.strip()
            cur_val = [v.strip()]
        else:
            cur_key = None
            cur_val = []
    if cur_key:
        out[cur_key] = _strip_yaml_scalar(" ".join(cur_val))
    return out


def _list_skills_in(dir_: Path, scope: str, source: str = "") -> list[dict]:
    if not dir_.exists() or not dir_.is_dir():
        return []
    out = []
    for child in sorted(dir_.iterdir()):
        if not child.is_dir():
            continue
        for fname in ("SKILL.md", "skill.md"):
            p = child / fname
            if p.exists():
                meta = _parse_skill_md(p) or {}
                entry = {
                    "name": meta.get("name") or child.name,
                    "description": meta.get("description", ""),
                    "scope": scope,
                    "path": str(child),
                }
                if source:
                    entry["source"] = source  # e.g. plugin name for scope=plugin
                out.append(entry)
                break
    return out


def _list_plugin_skills() -> list[dict]:
    """Walk ~/.claude/plugins/marketplaces/<mp>/plugins/<plugin>/skills/ and
    collect skills as scope=plugin entries. Each entry carries `source` =
    "<marketplace>/<plugin>" so the UI can show which plugin owns it.

    Why this matters: Claude Code users who install plugins (via the
    plugin marketplace) get skills bundled with each plugin. The SDK
    surfaces those at runtime, but muselab's discovery loop only looked
    at the top-level user/project scopes — so users saw "muselab knows
    18 skills" when their actual Claude Code setup had 40+.
    """
    out: list[dict] = []
    if not SKILL_PLUGIN_ROOT.exists() or not SKILL_PLUGIN_ROOT.is_dir():
        return out
    try:
        marketplaces = sorted(p for p in SKILL_PLUGIN_ROOT.iterdir() if p.is_dir())
    except OSError:
        return out
    for mp in marketplaces:
        plugins_dir = mp / "plugins"
        if not plugins_dir.is_dir():
            continue
        try:
            plugins = sorted(p for p in plugins_dir.iterdir() if p.is_dir())
        except OSError:
            continue
        for plugin in plugins:
            skills_dir = plugin / "skills"
            if not skills_dir.is_dir():
                continue
            source = f"{mp.name}/{plugin.name}"
            out.extend(_list_skills_in(skills_dir, "plugin", source=source))
    return out


@router.get("/skills", dependencies=[Depends(require_token)])
def list_skills() -> dict:
    """List all skills discoverable from project, user, and plugin scopes.

    project = muselab's own preset skills (this repo's skills/)
    user    = ~/.claude/skills/ (shared with Claude Code CLI)
    plugin  = ~/.claude/plugins/marketplaces/*/plugins/*/skills/

    Same-named skills across scopes are returned as separate entries —
    the UI shows scope badges so users can see when a skill is shadowed
    (e.g. they have a user-scope mermaid-helper AND a project preset)."""
    skills = (_list_skills_in(SKILL_PROJECT_DIR, "project") +
              _list_skills_in(SKILL_USER_DIR, "user") +
              _list_plugin_skills())
    return {"skills": skills}


# ====== Upgrade / version check ======
# (asyncio / shutil / subprocess / sys imports hoisted to module top per E402)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_UPGRADE_LOCK = asyncio.Lock()        # serialize upgrades — never run two at once
_LAST_UPGRADE: dict[str, Any] = {}     # cache last upgrade output for UI replay


# _locate_executable is imported at the top of the module (re-exported
# from settings) — see the top-level import block.


def _current_versions() -> dict:
    """Currently-installed muselab + SDK + CLI versions.

    Two distinct CLIs to track:
      - bundled_cli: ships inside claude-agent-sdk's _bundled/ directory,
        always present, version follows SDK releases. This is what muselab
        actually spawns to talk to Anthropic.
      - system_cli:  optional, installed via `npm install -g
        @anthropic-ai/claude-code`. Only needed once for `claude login`
        to create ~/.claude/.credentials.json (Pro/Max OAuth). After
        login the bundled CLI can read those credentials too.

    The UI should make this distinction clear so users don't think they
    need to install the system CLI when DeepSeek / API-key paths suffice."""
    # Invalidate the import machinery's path-based finder cache so that
    # importlib.metadata sees freshly-installed .dist-info dirs after uv sync.
    # Without this, repeated calls within the same process return the CACHED
    # old version (importlib.metadata.version() reuses MetadataPathFinder's
    # path_importer_cache). _semver_gt(latest, cached-old) stays True, so the
    # upgrade button never disappears after a successful SDK upgrade.
    import importlib
    importlib.invalidate_caches()
    import importlib.metadata as _md
    import re
    # Read SDK version from dist-info (installed package metadata) so a
    # post-upgrade re-probe sees the NEW version. The obvious
    # `from claude_agent_sdk import __version__` reads from sys.modules
    # which still holds the OLD module loaded at process start — uv sync
    # rewrites site-packages but doesn't touch in-memory imports. With
    # the import-based probe `after.sdk == before.sdk` always, so
    # needs_restart stays False and the UI silently skips the restart
    # prompt — users see "升级完成 ✓" then a table that didn't change.
    sdk_version = None
    try:
        sdk_version = _md.version("claude-agent-sdk")
    except Exception:
        # Editable install / namespace package without dist-info — fall
        # back to the module attr (gives the cached in-memory version
        # but at least we report something).
        try:
            from claude_agent_sdk import __version__ as _v
            sdk_version = _v
        except Exception:
            pass

    def _probe(bin_path: str | None) -> str | None:
        if not bin_path:
            return None
        try:
            out = subprocess.run([bin_path, "--version"],
                                  capture_output=True, text=True, timeout=3)
            line = (out.stdout.strip().splitlines() or [""])[0]
            m = re.search(r"\d+\.\d+\.\d+", line)
            return m.group(0) if m else (line or None)
        except Exception:
            return None

    bundled_path = None
    try:
        import claude_agent_sdk as _sdk
        bp = Path(_sdk.__file__).parent / "_bundled" / "claude.exe"
        if not bp.exists():
            bp = Path(_sdk.__file__).parent / "_bundled" / "claude"
        if bp.exists():
            bundled_path = str(bp)
    except Exception:
        pass

    bundled_cli = _probe(bundled_path)

    # System claude lookup — check multiple common install locations and
    # pick the one with the highest version. Reason: `npm install -g
    # @anthropic-ai/claude-code` typically writes to the user's npm prefix
    # (e.g. ~/.npm-global/bin/claude), but the user's $PATH may still
    # resolve `claude` to an older system-wide install at /usr/bin/claude.
    # If we only probed shutil.which("claude") we'd report the shadowed
    # OLD version even right after the user successfully upgraded — UI
    # would keep showing the upgrade-available badge. The fix is to also
    # peek into npm's prefix and the conventional ~/.npm-global path, then
    # report whichever binary actually has the highest semver.
    system_candidates: list[str] = []
    # _locate_executable covers PATH + ~/.npm-global/bin + nvm/Volta dirs
    # that systemd's user PATH rarely sees. shutil.which("claude") alone
    # missed the most common install paths.
    which_claude = _locate_executable("claude")
    if which_claude:
        system_candidates.append(which_claude)
    home_npm = str(Path.home() / ".npm-global" / "bin" / "claude")
    if Path(home_npm).exists() and home_npm not in system_candidates:
        system_candidates.append(home_npm)
    npm_bin = _locate_executable("npm")
    if npm_bin:
        try:
            r = subprocess.run(
                [npm_bin, "config", "get", "prefix"],
                capture_output=True, text=True, timeout=2,
            )
            prefix = (r.stdout or "").strip()
            if prefix and prefix not in ("undefined",):
                cand = str(Path(prefix) / "bin" / "claude")
                if Path(cand).exists() and cand not in system_candidates:
                    system_candidates.append(cand)
        except Exception:
            pass

    system_cli: str | None = None
    system_cli_path: str | None = None
    for cand in system_candidates:
        v = _probe(cand)
        if v and (system_cli is None or _semver_gt(v, system_cli)):
            system_cli = v
            system_cli_path = cand

    return {
        "sdk": sdk_version,
        "bundled_cli": bundled_cli,
        "system_cli": system_cli,
        "system_cli_present": system_cli is not None,
        # New: the actual path muselab is reporting from. Helps users
        # diagnose "I just upgraded but the badge is still there" — they
        # can see whether muselab is looking at the right binary.
        "system_cli_path": system_cli_path,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        # Legacy field kept for backward-compat with any consumer expecting it.
        "cli": bundled_cli or system_cli,
        "cli_present": bool(bundled_cli or system_cli),
    }


async def _latest_versions() -> dict:
    """Query PyPI + npm for the latest released versions of SDK / CLI.
    Returns {sdk: str|None, cli: str|None, errors: [str]}."""
    import httpx
    errors: list[str] = []
    sdk_latest = None
    cli_latest = None
    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            r = await client.get("https://pypi.org/pypi/claude-agent-sdk/json")
            if r.status_code == 200:
                sdk_latest = r.json().get("info", {}).get("version")
            else:
                errors.append(f"pypi HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"pypi: {type(e).__name__}: {e}")
        try:
            r = await client.get("https://registry.npmjs.org/@anthropic-ai/claude-code/latest")
            if r.status_code == 200:
                cli_latest = r.json().get("version")
            else:
                errors.append(f"npm HTTP {r.status_code}")
        except Exception as e:
            errors.append(f"npm: {type(e).__name__}: {e}")
    return {"sdk": sdk_latest, "cli": cli_latest, "errors": errors}


def _semver_gt(a: str | None, b: str | None) -> bool:
    """True if a > b (both 'X.Y.Z' style). Missing → False (don't suggest upgrade)."""
    if not a or not b:
        return False
    try:
        ta = tuple(int(p) for p in a.split(".")[:3])
        tb = tuple(int(p) for p in b.split(".")[:3])
        return ta > tb
    except (ValueError, AttributeError):
        return False


@router.get("/versions", dependencies=[Depends(require_token)])
async def get_versions() -> dict:
    """Current + latest versions for SDK and CLI; flags whether an upgrade
    is available. Used by the Settings panel's "版本与升级" section."""
    # _current_versions() runs importlib.invalidate_caches() + a `claude
    # --version` subprocess (timeout up to 3s) + an npm prefix probe — all
    # blocking. Off-load to a thread so opening Settings → "版本与升级"
    # can't stall the event loop (and every concurrent SSE stream) for
    # seconds. (perf: RED — api_settings.py get_versions)
    current = await asyncio.to_thread(_current_versions)
    latest = await _latest_versions()
    return {
        "current": current,
        "latest": latest,
        "sdk_upgrade_available": _semver_gt(latest.get("sdk"), current.get("sdk")),
        # Only relevant if user has system CLI installed (for `claude login`).
        # Bundled CLI is auto-upgraded when SDK is upgraded.
        "system_cli_upgrade_available": (
            current.get("system_cli_present") and
            _semver_gt(latest.get("cli"), current.get("system_cli"))
        ),
        "expected_cli_version": _expected_cli_version(),
        "last_upgrade": _LAST_UPGRADE.copy() if _LAST_UPGRADE else None,
    }


def _expected_cli_version() -> str | None:
    """SDK bundles a string indicating the CLI version it was built against.
    If user's installed CLI is older, --session-id and other recent flags fail."""
    try:
        from claude_agent_sdk._cli_version import __cli_version__
        return __cli_version__
    except Exception:
        return None


class UpgradeReq(BaseModel):
    targets: list[str] = Field(default_factory=lambda: ["sdk", "cli"])
    """Which packages to upgrade. Default: both."""


@router.post("/upgrade", dependencies=[Depends(require_token)])
async def trigger_upgrade(req: UpgradeReq) -> dict:
    """Run the upgrade flow in-process. Returns step-by-step output for the
    UI to render. Does NOT restart muselab — the running Python process keeps
    serving the old SDK in memory until you restart it (Scheduled Task /
    systemd / launchctl). UI shows the restart command after upgrade ends.

    SECURITY — RCE surface, deliberately gated:
      This endpoint shells out to `uv lock` / `uv sync` / `npm install -g`.
      Package installs run arbitrary install scripts (npm postinstall, build
      hooks), so anyone who can reach this endpoint can achieve code execution
      as the muselab process user. That is acceptable ONLY because the route
      is gated behind `require_token` — the same admin token that already
      grants full archive read/write and unattended bypassPermissions agent
      runs. In muselab's single-tenant, self-hosted threat model the token
      holder is the owner, so /upgrade grants no privilege they don't already
      have. The args are fixed string literals (no user-controlled package
      names) and `uv sync --frozen` installs only what's already pinned in
      uv.lock, so a leaked token can't be steered to install attacker-chosen
      packages here. Self-hosters who want to remove the surface entirely can
      block POST /api/settings/upgrade at their reverse proxy. Do NOT widen
      this to accept caller-supplied package names without re-reviewing.
    """
    if _UPGRADE_LOCK.locked():
        raise HTTPException(409, "upgrade already in progress")
    async with _UPGRADE_LOCK:
        steps: list[dict] = []
        before = _current_versions()
        steps.append({"step": "before", "versions": before})

        if "sdk" in req.targets:
            # uv lock --upgrade-package claude-agent-sdk, then uv sync.
            # Resolve uv via _locate_executable() — systemd-user PATH
            # rarely includes ~/.local/bin where uv lives by default.
            uv_bin = _locate_executable("uv")
            if not uv_bin:
                steps.append({
                    "step": "uv lock",
                    "rc": -1,
                    "output": (
                        "uv binary not found. Looked in PATH and "
                        "~/.local/bin, ~/.cargo/bin, /usr/local/bin, "
                        "/opt/homebrew/bin, /usr/bin. Install uv first: "
                        "https://docs.astral.sh/uv/getting-started/installation/"
                    ),
                })
            else:
                try:
                    p1 = await asyncio.create_subprocess_exec(
                        uv_bin, "lock", "--upgrade-package", "claude-agent-sdk",
                        cwd=str(_REPO_ROOT),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    )
                    out1, _ = await p1.communicate()
                    steps.append({"step": "uv lock", "rc": p1.returncode,
                                  "output": (out1 or b"").decode(errors="replace")[-2000:]})
                    if p1.returncode != 0:
                        raise RuntimeError("uv lock failed")

                    p2 = await asyncio.create_subprocess_exec(
                        uv_bin, "sync", "--frozen",
                        cwd=str(_REPO_ROOT),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    )
                    out2, _ = await p2.communicate()
                    steps.append({"step": "uv sync", "rc": p2.returncode,
                                  "output": (out2 or b"").decode(errors="replace")[-2000:]})
                    if p2.returncode != 0:
                        raise RuntimeError("uv sync failed")
                except Exception as e:
                    steps.append({"step": "sdk upgrade aborted", "rc": -1,
                                  "output": f"{type(e).__name__}: {e}"})

        if "cli" in req.targets:
            # Same PATH problem as uv: node version managers (nvm /
            # Volta) install npm into per-user dirs that systemd's user
            # PATH usually doesn't see.
            npm_bin = _locate_executable("npm")
            if npm_bin:
                try:
                    p3 = await asyncio.create_subprocess_exec(
                        npm_bin, "install", "-g", "@anthropic-ai/claude-code@latest",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    )
                    out3, _ = await p3.communicate()
                    steps.append({"step": "npm install -g claude-code", "rc": p3.returncode,
                                  "output": (out3 or b"").decode(errors="replace")[-2000:]})
                except Exception as e:
                    steps.append({"step": "cli upgrade aborted", "rc": -1,
                                  "output": f"{type(e).__name__}: {e}"})
            else:
                steps.append({
                    "step": "npm not found",
                    "rc": -1,
                    "output": (
                        "Looked in PATH + ~/.local/bin, ~/.cargo/bin, "
                        "~/.nvm/versions/node/*/bin, ~/.volta/bin, "
                        "/usr/local/bin, /opt/homebrew/bin, /usr/bin. "
                        "Install Node.js + npm first."
                    ),
                })

        # Re-read post-upgrade versions
        after = _current_versions()
        steps.append({"step": "after", "versions": after})

        # Detect changes
        sdk_changed = before.get("sdk") != after.get("sdk")
        cli_changed = before.get("cli") != after.get("cli")
        all_ok = all(s.get("rc", 0) == 0 for s in steps if "rc" in s)
        result = {
            "ok": all_ok,
            "steps": steps,
            "sdk_changed": sdk_changed,
            "cli_changed": cli_changed,
            "needs_restart": sdk_changed,   # SDK loaded in-process → restart to pick up
            "restart_hint": _restart_hint(),
        }
        _LAST_UPGRADE.clear()
        _LAST_UPGRADE.update(result)
        return result


# NOTE: The cross-device UI-state endpoints (GET/PUT /ui-state, backed by
# sessions/.ui-state.json) were removed. The chat tab strip, active tab, and
# preview tab strip are now device-local (persisted in each browser's
# localStorage) — syncing them across devices yanked the active tab out from
# under the user and hurt the experience.


@router.post("/restart", dependencies=[Depends(require_token)])
async def restart_service() -> dict:
    """Restart the muselab process so a freshly-installed SDK is loaded.
    Sends the 200 response first, then schedules the actual restart after a
    short delay so the HTTP response has time to flush to the client."""
    asyncio.create_task(_do_restart())
    return {"ok": True, "restarting": True}


async def _do_restart() -> None:
    """Try platform restart command first; fall back to os.execv."""
    import asyncio as _asyncio
    await _asyncio.sleep(0.8)   # let the HTTP response reach the browser
    hint = _restart_hint()
    if hint:
        try:
            rc = subprocess.run(hint, shell=True, timeout=8)
            if rc.returncode == 0:
                return          # systemd/launchctl handled it
        except Exception:
            pass
    # Fallback: replace current process with a fresh copy.
    # Works for both `uv run python -m backend.main` and direct invocations.
    import os as _os
    _os.execv(sys.executable, [sys.executable] + sys.argv)


def _restart_hint() -> str:
    """Platform-specific command to restart muselab so the new SDK is loaded."""
    import platform
    sysname = platform.system()
    if sysname == "Darwin":
        return "launchctl kickstart -k gui/$UID/com.muselab"
    return "systemctl --user restart muselab"


# ============================================================
# Claude Auth — Pro/Max OAuth as a first-class settings provider.
# Treated separately from PROVIDER_KEYS because it isn't an API key —
# auth lives in ~/.claude/.credentials.json (written by `claude login`)
# and identity comes from `claude auth status --json`. The UI renders a
# dedicated card next to the other providers.
# ============================================================
import shutil as _shutil  # noqa: E402  — kept here to colocate with Claude-Auth helpers

_CLAUDE_CRED = Path.home() / ".claude" / ".credentials.json"


def _claude_cli_path() -> str | None:
    """Find the `claude` executable on PATH. Returns None if absent."""
    return _shutil.which("claude")


def _run_claude_auth_status(timeout: float = 8.0) -> dict:
    """Invoke `claude auth status --json` and return parsed dict.
    Returns {"loggedIn": False, "reason": ...} on any failure so the UI
    always gets a deterministic shape — never crashes the endpoint."""
    cli = _claude_cli_path()
    if not cli:
        return {"loggedIn": False, "reason": "cli-not-installed"}
    try:
        proc = subprocess.run(
            [cli, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=timeout,
            # Don't inherit the parent env's ANTHROPIC_* — those would
            # mask the OAuth check and make this report a confusing
            # "logged in via API key" state. The CLI itself reads
            # credentials.json directly so no env is needed.
            env={"PATH": os.environ.get("PATH", ""),
                 "HOME": os.environ.get("HOME", str(Path.home()))},
        )
    except subprocess.TimeoutExpired:
        return {"loggedIn": False, "reason": "cli-timeout"}
    except Exception as e:
        return {"loggedIn": False, "reason": f"cli-error: {type(e).__name__}"}
    if proc.returncode != 0:
        return {"loggedIn": False, "reason": "not-logged-in",
                "stderr": (proc.stderr or "").strip()[:300]}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"loggedIn": False, "reason": "cli-bad-json",
                "stdout": (proc.stdout or "")[:300]}
    return data


def _read_credentials_expiry() -> int | None:
    """Read OAuth token expiry timestamp (ms since epoch) from credentials.json.
    Returns None if file missing / unreadable / field absent."""
    if not _CLAUDE_CRED.exists():
        return None
    try:
        data = json.loads(_CLAUDE_CRED.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data.get("claudeAiOauth", {}).get("expiresAt")


@router.get("/claude-auth/status", dependencies=[Depends(require_token)])
def claude_auth_status() -> dict:
    """Report whether the user is signed in to Claude via Pro/Max OAuth,
    and surface identity (email / org / plan) when available.

    Shape returned to the UI:
      {
        "cli_installed": bool,
        "cli_path": str | null,
        "credentials_file_present": bool,
        "logged_in": bool,
        "email": str | null,
        "org_name": str | null,
        "subscription_type": str | null,   # "max" / "pro" / "free" / null
        "expires_at": int | null,          # ms-since-epoch, OAuth token
        "reason": str | null,              # diagnostic when !logged_in
      }
    """
    cli = _claude_cli_path()
    status = _run_claude_auth_status() if cli else {
        "loggedIn": False, "reason": "cli-not-installed"
    }
    expires_at = _read_credentials_expiry()
    logged_in = bool(status.get("loggedIn", False))
    reason = status.get("reason")
    # `claude auth status` reports loggedIn=True as long as credentials.json
    # exists — it does NOT validate that the OAuth access_token is still
    # fresh. Symptom: a long-idle session shows "已连接" in the UI but every
    # actual chat fails because the token expired and the user has to run
    # `claude login` again in a shell. Cross-check expiresAt here so we
    # surface "token-expired" up front instead of confidently lying.
    if logged_in and expires_at:
        import time as _time
        if expires_at < int(_time.time() * 1000):
            logged_in = False
            reason = "token-expired"
    return {
        "cli_installed": cli is not None,
        "cli_path": cli,
        "credentials_file_present": _CLAUDE_CRED.exists(),
        "logged_in": logged_in,
        "email": status.get("email"),
        "org_name": status.get("orgName"),
        "subscription_type": status.get("subscriptionType"),
        "expires_at": expires_at,
        "reason": reason,
    }


@router.post("/claude-auth/disconnect", dependencies=[Depends(require_token)])
def claude_auth_disconnect() -> dict:
    """Disconnect Claude Auth by moving credentials.json to a .bak sibling.
    Reversible — the user can rename it back if they regret. We DON'T call
    `claude logout` because that wipes the file outright; the .bak move
    matches the user's explicit preference for a reversible disconnect."""
    if not _CLAUDE_CRED.exists():
        return {"ok": True, "already_disconnected": True}
    # Time-stamped backup so repeated connect/disconnect cycles don't clobber
    # an earlier backup. ".bak" without suffix would be lost on the second
    # disconnect when the user reconnects + disconnects again.
    import datetime as _dt
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = _CLAUDE_CRED.with_suffix(f".json.{stamp}.bak")
    try:
        _CLAUDE_CRED.rename(bak)
    except OSError as e:
        raise HTTPException(status_code=500,
                            detail=f"Could not move credentials file: {e}") from e
    return {"ok": True, "backup_path": str(bak)}
