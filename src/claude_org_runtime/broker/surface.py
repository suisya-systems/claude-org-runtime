# -*- coding: utf-8 -*-
"""MCP surface definitions for the org-broker (the "what tools exist / how
they are routed" layer).

設計 SoT: docs/design/ja-migration-plan.md §3.1 / §3.3 / §8 (Issue C) と
docs/design/renga-decoupling.md §4.2 (役割別公開面)。canonical golden shape:
renga-peers の pane-control + messaging surface。本モジュールはその golden
shape と**同名・同形 (drop-in 形差ゼロ)** の MCP 面を broker 上に再現する。

スコープ (Issue C, §3.3 推奨 6 点):
- spawn_claude_pane / spawn_codex_pane を renga と同シグネチャの構造化
  ビルダーとして公開し、対話 TUI argv を broker 内部で組む。課金中立 guard は
  「caller argv 検査」ではなく「broker 自身のビルダー出力」に対する
  **default-deny allowlist** にする (blacklist は後追いで閉じない —
  knowledge/raw/.../billing-neutral-spawn-argv-guard.md の教訓)。
- pane target は handle / stable name / 'focused' の三系統で解決する
  (:meth:`Broker.resolve_target`)。
- list_peers / list_panes は cwd を含める (Set D cwd parity, §3.3-4)。
  receive_mode は broker では全 pull 統一のため定数 "pull"、kind は bind 由来。
- set_pane_identity は renga 同形の three-state (omit=据置 / null=クリア /
  str=設定)。auth tier は不変 ``auth_role`` のみで決める (表示 role 非関与)。

tier gating (§4.2): 公開面は token の ``auth_role`` で構造的に変える。
- worker / curator: messaging 4 面のみ。
- dispatcher: messaging + pane 操作 (spawn_pane generic を除く)。
- secretary: 全面 (attention watcher 用 generic spawn_pane を含む)。
インジェクションを踏んだ worker が窓口ペインへ直接打鍵する経路 (send_keys) を
許可設定ではなく**構造的に**断つのが狙い。

This module is a stateless leaf: it holds the protocol constants, the tool
catalogue, the tier map, and the pure argv builders + guards, and routes
``tools/call`` to the stateful :class:`~claude_org_runtime.broker.server.Broker`.
It owns no locks and no queues.
"""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 循環 import 回避 (server -> surface -> server を型のみで切る)
    from .server import Broker
    from .tokens import AgentBind

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "org-broker", "version": "0.1.0"}

# broker では配達は全 pull (check_messages) 統一なので receive_mode は定数。
# renga の push/poll 区別は概念が異なるため定数化する (Set D amendment, §3.3-4)。
RECEIVE_MODE = "pull"

# pane addressing: 全桁数字 → handle 確定 (renga と同契約; "7" という名前の
# pane は名前で引けず id で引く)。'focused' はリテラル。
_ALL_DIGITS = re.compile(r"^\d+$")

# set_pane_identity / spawn 系の name 検証 (renga と同契約: 空 / 全桁数字 /
# 衝突は不可、許可文字は [A-Za-z0-9_-])。衝突検査は broker (bind 表) 側。
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# send_keys の named-key 語彙 (renga golden shape と一致。B で照合済み)。
# 値は「契約上有効なキー名」の集合。実打鍵可能かは backend adapter 能力に依存
# (現 adapter は Enter / Ctrl+C / literal text のみ。raw 全語彙は §4.7 Phase 4)。
_SEND_KEYS_VOCAB = {
    "enter", "return", "tab", "shift+tab", "backtab", "esc", "escape",
    "backspace", "delete", "del", "up", "down", "left", "right",
    "home", "end", "pageup", "pagedown", "space",
} | {f"ctrl+{c}" for c in "abcdefghijklmnopqrstuvwxyz"}


class ToolArgError(ValueError):
    """tools/call の引数不正 (JSON-RPC -32602 invalid params に変換される)。"""


# ---------------------------------------------------------------------------
# tool 入力スキーマ (renga golden shape と同形)
# ---------------------------------------------------------------------------

_TARGET_SCHEMA = {
    "type": "string",
    "description": (
        "Pane to address. Numeric id (from list_panes), stable name, or the "
        "literal 'focused'. All-digit strings are always interpreted as ids."
    ),
}
_DIRECTION_SCHEMA = {
    "type": "string",
    "enum": ["vertical", "horizontal"],
    "description": (
        "`vertical` splits side-by-side (new pane to the right); `horizontal` "
        "splits top/bottom (new pane on the bottom)."
    ),
}

# 全 13 面の catalogue。tier フィルタは tools_for() が auth_role で行う。
TOOLS = [
    # ----- messaging (worker / curator / dispatcher / secretary) -----------
    {
        "name": "send_message",
        "description": "Send a message to another agent via the broker queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to_id": {"type": "string", "description": "Recipient agent id or name."},
                "message": {"type": "string", "description": "Text to deliver."},
            },
            "required": ["to_id", "message"],
        },
    },
    {
        "name": "check_messages",
        "description": "Drain queued messages addressed to this agent (at-most-once).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_peers",
        "description": "List registered agents visible to this agent (includes cwd).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["machine", "directory", "repo"],
                    "description": "Accepted for wire-compat; broker ignores it.",
                }
            },
        },
    },
    {
        "name": "set_summary",
        "description": "Set a short summary of what this agent is working on.",
        "inputSchema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
    # ----- pane control (dispatcher / secretary) ---------------------------
    {
        "name": "list_panes",
        "description": (
            "List panes in the current tab with id, name, role, focused flag, "
            "geometry (x/y/w/h) and cwd."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "inspect_pane",
        "description": "Snapshot the visible screen of a pane (grid scrape).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": _TARGET_SCHEMA,
                "lines": {
                    "type": "integer", "minimum": 1,
                    "description": "Trim to the bottom N rows. Omit for full screen.",
                },
                "format": {
                    "type": "string", "enum": ["text", "grid"],
                    "description": "'text' (default) plain screen; 'grid' one object per row.",
                },
                "include_cursor": {
                    "type": "boolean",
                    "description": "Include a cursor object ({visible,row,col}). Default false.",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "send_keys",
        "description": "Send raw keystrokes to a pane's PTY (text + named keys).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": _TARGET_SCHEMA,
                "text": {"type": "string", "description": "Literal text sent before keys."},
                "keys": {
                    "type": "array", "items": {"type": "string"},
                    "description": (
                        "Named special keys appended after text: Enter/Return, Tab, "
                        "Shift+Tab/BackTab, Esc/Escape, Backspace, Delete/Del, "
                        "Up/Down/Left/Right, Home, End, PageUp, PageDown, Space, "
                        "Ctrl+<A-Z>. Unknown names return -32602."
                    ),
                },
                "enter": {
                    "type": "boolean",
                    "description": "Append an Enter after text and keys.",
                },
            },
            "required": ["target"],
        },
    },
    {
        "name": "poll_events",
        "description": "Long-poll for pane lifecycle events (cursor-based).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "since": {"type": "string", "description": "Cursor from a prior next_since."},
                "timeout_ms": {
                    "type": "integer", "minimum": 0,
                    "description": "Max ms to block (default 2000; capped 30000).",
                },
                "types": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Optional filter on event type.",
                },
            },
        },
    },
    {
        "name": "close_pane",
        "description": "Close a pane, terminating its process.",
        "inputSchema": {
            "type": "object",
            "properties": {"target": _TARGET_SCHEMA},
            "required": ["target"],
        },
    },
    {
        "name": "set_pane_identity",
        "description": (
            "Rename/(re)assign a pane's display name and/or role. Three-state: "
            "omit=keep, null=clear, str=set. (auth tier is unaffected.)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": _TARGET_SCHEMA,
                "name": {"type": ["string", "null"], "description": "New name, null to clear, omit to keep."},
                "role": {"type": ["string", "null"], "description": "New role, null to clear, omit to keep."},
            },
        },
    },
    {
        "name": "spawn_claude_pane",
        "description": (
            "Split a pane and launch interactive Claude Code wired to the broker "
            "MCP. Structured permission_mode/model fields; extra args appended."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": _DIRECTION_SCHEMA,
                "target": _TARGET_SCHEMA,
                "name": {"type": "string", "description": "Stable id for the new pane."},
                "role": {"type": "string", "description": "Free-form role label."},
                "model": {"type": "string", "description": "Rendered as --model <value>."},
                "permission_mode": {
                    "type": "string",
                    "description": "Rendered as --permission-mode <value>.",
                },
                "args": {
                    "type": "array", "items": {"type": "string"},
                    "description": (
                        "Extra Claude args appended after structured fields. Must NOT "
                        "contain --mcp-config / --permission-mode / --model (use the "
                        "structured fields) — rejected with invalid-params."
                    ),
                },
                "cwd": {"type": "string", "description": "Working directory for the new pane."},
            },
            "required": ["direction"],
        },
    },
    {
        "name": "spawn_pane",
        "description": (
            "Split a pane and run a generic command (no broker token injection). "
            "secretary tier only (attention watcher)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": _DIRECTION_SCHEMA,
                "command": {"type": "string", "description": "Shell command to run in the new pane."},
                "target": _TARGET_SCHEMA,
                "name": {"type": "string", "description": "Stable id for the new pane."},
                "role": {"type": "string", "description": "Free-form role label."},
                "cwd": {"type": "string", "description": "Working directory for the new pane."},
            },
            "required": ["direction"],
        },
    },
    {
        "name": "spawn_codex_pane",
        "description": (
            "Split a pane and launch interactive Codex wired to the broker MCP. "
            "Billing-neutral: non-interactive subcommands are rejected by a "
            "default-deny allowlist."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "direction": _DIRECTION_SCHEMA,
                "target": _TARGET_SCHEMA,
                "name": {"type": "string", "description": "Stable id for the new pane."},
                "role": {"type": "string", "description": "Free-form role label."},
                "args": {
                    "type": "array", "items": {"type": "string"},
                    "description": (
                        "Extra Codex args appended after the codex token. Only "
                        "interactive-TUI flags are allowed; subcommands / bare "
                        "positionals / '--' / unknown flags are rejected."
                    ),
                },
                "cwd": {"type": "string", "description": "Working directory for the new pane."},
            },
            "required": ["direction"],
        },
    },
]

# tier 公開面 (§4.2)。auth_role で構造的に絞る。messaging は全 tier 共通。
_MESSAGING_TOOLS = {"send_message", "check_messages", "list_peers", "set_summary"}
# pane 操作 (spawn_pane generic を除く)。dispatcher / secretary。
_OPS_TOOLS = {
    "list_panes", "inspect_pane", "send_keys", "poll_events", "close_pane",
    "set_pane_identity", "spawn_claude_pane", "spawn_codex_pane",
}
# generic spawn_pane は secretary のみ (attention watcher 起動)。
_SECRETARY_ONLY_TOOLS = {"spawn_pane"}

_OPS_TIERS = {"dispatcher", "secretary"}
_SECRETARY_TIER = {"secretary"}


def _allowed_tools(auth_role: str) -> set[str]:
    allowed = set(_MESSAGING_TOOLS)
    if auth_role in _OPS_TIERS:
        allowed |= _OPS_TOOLS
    if auth_role in _SECRETARY_TIER:
        allowed |= _SECRETARY_ONLY_TOOLS
    return allowed


def tools_for(auth_role: str) -> list[dict]:
    """auth_role が到達できる tool catalogue を返す (tools/list の tier フィルタ)。"""
    allowed = _allowed_tools(auth_role)
    return [t for t in TOOLS if t["name"] in allowed]


# ---------------------------------------------------------------------------
# 課金中立 spawn argv ビルダー (default-deny allowlist)
# ---------------------------------------------------------------------------
#
# 教訓 (knowledge/raw/archive/2026-06-10-billing-neutral-spawn-argv-guard.md):
# headless 排除を blacklist で後追いすると flag 後サブコマンド / `--` バイパスを
# 取り逃す。**対話用 flag を列挙し、それ以外の token を一律拒否する default-deny
# allowlist** にすると一発で閉じる。値を取る flag は arity を持たせ (i+=2)、
# argv[0] は basename 判定 (絶対パス起動を false-reject しない)。
# allowlist は保守契約とセット: 新しい正規対話 flag が増えたら拡張する。
# **headless 系 (-p / --print / exec 等) は決して入れない**。

# --- Claude (interactive TUI) ---
# broker が注入する --mcp-config / --model / --permission-mode を含む対話 flag。
_CLAUDE_VALUE_FLAGS = {
    "--mcp-config", "--model", "--permission-mode", "--add-dir", "--settings",
    "--append-system-prompt", "--setting-sources", "--session-id",
    "--permission-prompt-tool", "--mcp-config-file",
}
_CLAUDE_BOOL_FLAGS = {
    "--strict-mcp-config", "--continue", "--verbose", "--debug", "--ide",
}
# defense-in-depth: 値位置や allowlist 漏れの headless flag を二段で弾く。
_CLAUDE_HEADLESS_BLACKLIST = {
    "-p", "--print", "--headless", "--output-format", "--input-format",
    "--include-partial-messages", "--replay-user-messages",
}
# caller の args[] に持たせてはいけない (broker 構造化フィールドと衝突)。
_CLAUDE_RESERVED_IN_ARGS = {
    "--mcp-config", "--model", "--permission-mode", "--strict-mcp-config",
    "--mcp-config-file",
}

# --- Codex (interactive TUI) ---
# codex の対話 TUI が受ける flag のみ。サブコマンド (exec/review/*-server/apply/
# sandbox/completion 等) は bare positional として default-deny で落ちる。
_CODEX_VALUE_FLAGS = {
    "--model", "-m", "--config", "-c", "--cd", "-C", "--image", "-i",
    "--sandbox", "-s", "--ask-for-approval", "-a", "--profile",
}
_CODEX_BOOL_FLAGS = {"--oss", "--search", "--full-auto"}


def _basename(path: str) -> str:
    return os.path.basename(path.replace("\\", "/"))


def _reject_reserved_claude_args(args: list[str]) -> None:
    for tok in args:
        flag = tok.split("=", 1)[0]
        if flag in _CLAUDE_RESERVED_IN_ARGS:
            raise ToolArgError(
                f"args[] must not contain {flag!r}; use the structured "
                "model / permission_mode fields (broker injects --mcp-config)"
            )


def _guard_interactive_claude_argv(argv: list[str]) -> None:
    """broker が組んだ claude argv が対話 TUI 確定であることを構造検証する。

    default-deny: allowlist 外 token (flag 後サブコマンド / bare positional /
    `--` / 未知 flag / headless flag) は一律拒否する。
    """
    if not argv or _basename(argv[0]) != "claude":
        raise ToolArgError("claude builder argv[0] must be the claude binary")
    i = 1
    while i < len(argv):
        tok = argv[i]
        flag = tok.split("=", 1)[0]
        if flag in _CLAUDE_HEADLESS_BLACKLIST:
            raise ToolArgError(f"headless flag {flag!r} is forbidden (billing-neutral guard)")
        if tok == "--":
            raise ToolArgError("'--' is forbidden (would introduce positionals/subcommand)")
        if tok.startswith("--") and "=" in tok:
            if flag not in _CLAUDE_VALUE_FLAGS:
                raise ToolArgError(f"claude arg {flag!r} not in interactive allowlist")
            i += 1
            continue
        if tok in _CLAUDE_VALUE_FLAGS:
            if i + 1 >= len(argv):
                raise ToolArgError(f"claude flag {tok!r} expects a value")
            val_flag = argv[i + 1].split("=", 1)[0]
            if val_flag in _CLAUDE_HEADLESS_BLACKLIST:
                raise ToolArgError(f"headless flag {val_flag!r} in value position is forbidden")
            i += 2
            continue
        if tok in _CLAUDE_BOOL_FLAGS:
            i += 1
            continue
        raise ToolArgError(
            f"claude arg {tok!r} not in interactive allowlist "
            "(subcommands/bare positionals/unknown flags are rejected)"
        )


def build_claude_argv(
    *,
    mcp_config_json: str,
    model: str | None = None,
    permission_mode: str | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """spawn_claude_pane の対話 TUI argv を組む (renga の dev-channel 合成の代替)。

    broker が --mcp-config を注入し (token 入り JSON)、model / permission_mode は
    構造化フィールドから一度だけ描画する。caller の extra_args は構造化フィールドと
    衝突する予約 flag を持てない (renga parity)。最後に default-deny guard を通す。
    """
    extra_args = list(extra_args or [])
    _reject_reserved_claude_args(extra_args)
    argv = ["claude", "--mcp-config", mcp_config_json]
    if permission_mode:
        argv += ["--permission-mode", permission_mode]
    if model:
        argv += ["--model", model]
    argv += extra_args
    _guard_interactive_claude_argv(argv)
    return argv


def _guard_interactive_codex_argv(argv: list[str]) -> None:
    """broker が組んだ codex argv が対話 TUI 確定であることを構造検証する。

    **セキュリティ重要 (§3.3-6)**: default-deny allowlist。argv[0] basename ==
    codex かつ、以降は対話 TUI 用 allowlist の flag/value のみ許可。exec /
    review / mcp-server / app-server / exec-server / apply / sandbox /
    completion / 未知サブコマンド / bare positional / `--` は一律拒否する
    (これらはすべて allowlist 外 token として落ちる)。
    """
    if not argv or _basename(argv[0]) != "codex":
        raise ToolArgError("codex builder argv[0] must be the codex binary")
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            raise ToolArgError(
                "'--' is forbidden for codex (would pass through a subcommand/positional)"
            )
        if tok.startswith("--") and "=" in tok:
            flag = tok.split("=", 1)[0]
            if flag not in _CODEX_VALUE_FLAGS:
                raise ToolArgError(f"codex arg {flag!r} not in interactive allowlist")
            i += 1
            continue
        if tok in _CODEX_VALUE_FLAGS:
            if i + 1 >= len(argv):
                raise ToolArgError(f"codex flag {tok!r} expects a value")
            i += 2
            continue
        if tok in _CODEX_BOOL_FLAGS:
            i += 1
            continue
        # 未知 flag / サブコマンド (exec, review, *-server, apply, sandbox,
        # completion, ...) / bare positional はすべてここで拒否される。
        raise ToolArgError(
            f"codex arg {tok!r} not in interactive allowlist "
            "(exec/review/*-server/apply/sandbox/completion/subcommands/"
            "bare positionals/unknown flags are rejected — this builder only "
            "emits an interactive TUI; billing-neutral default-deny guard)"
        )


def build_codex_argv(*, extra_args: list[str] | None = None) -> list[str]:
    """spawn_codex_pane の対話 TUI argv を組む。default-deny guard を通す。"""
    argv = ["codex", *list(extra_args or [])]
    _guard_interactive_codex_argv(argv)
    return argv


# ---------------------------------------------------------------------------
# name 検証 (set_pane_identity / spawn 系で共有)
# ---------------------------------------------------------------------------

def validate_pane_name(name: str) -> None:
    """renga 同契約の name 検証 (空 / 全桁数字 / 不正文字を -32602)。衝突は別途。"""
    if not name:
        raise ToolArgError("name cannot be empty")
    if _ALL_DIGITS.match(name):
        raise ToolArgError("name cannot be all-digits (collides with handle addressing)")
    if not _NAME_PATTERN.match(name):
        raise ToolArgError("name allows only [A-Za-z0-9_-]")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

def _ok(result: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def _err(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def dispatch_tool(broker: "Broker", bind: "AgentBind", name: str, args: dict) -> dict:
    """ツール実行 (allowlist 分岐 + tier gating)。引数不正は ToolArgError
    (handler 側で -32602 に変換)。tier 外 / 未知 tool は isError。

    元 ``Broker.call_tool`` の本体。faithful port のため list_peers /
    set_summary は broker 内部状態 (``_binds`` / ``_lock``) を直接読む。lock
    内では I/O / journal を呼ばない (server 側の DELETE デッドロック回避契約と整合)。
    """
    # --- tier gating: auth_role (不変) のみで公開面を決める ----------------
    if name not in {t["name"] for t in TOOLS}:
        return _err(f"[unknown_tool] {name}")
    if name not in _allowed_tools(bind.auth_role):
        # 構造的に到達不能であるべき面 (worker→send_keys 等)。許可設定ではなく
        # tier で断つ (§4.2 の狙い)。
        return _err(f"[tool_not_authorized] {name} is not available to role {bind.auth_role!r}")

    # --- messaging --------------------------------------------------------
    if name == "send_message":
        to_id, message = args.get("to_id"), args.get("message")
        if not isinstance(to_id, str) or not isinstance(message, str):
            raise ToolArgError("send_message requires string to_id and message")
        return _ok(broker.enqueue(bind, to_id, message))

    if name == "check_messages":
        return _ok({"messages": broker.drain(bind)})

    if name == "list_peers":
        with broker._lock:
            peers = [
                {
                    "id": b.agent_id,
                    "name": b.name,
                    "role": b.role,
                    "summary": b.summary,
                    "cwd": b.cwd,
                    "kind": b.kind,
                    "receive_mode": RECEIVE_MODE,
                }
                for b in broker._binds.values()
                if b.registered and not b.revoked
            ]
        return _ok({"peers": peers})

    if name == "set_summary":
        summary = args.get("summary")
        if not isinstance(summary, str):
            raise ToolArgError("set_summary requires string summary")
        with broker._lock:
            bind.summary = summary
        return _ok({"ok": True})

    # --- pane control -----------------------------------------------------
    if name == "list_panes":
        return _ok({"panes": broker.list_panes_view()})

    if name == "inspect_pane":
        target = args.get("target")
        if not isinstance(target, str):
            raise ToolArgError("inspect_pane requires string target")
        lines = args.get("lines")
        if lines is not None and (not isinstance(lines, int) or lines < 1):
            raise ToolArgError("lines must be a positive integer")
        fmt = args.get("format", "text")
        if fmt not in ("text", "grid"):
            raise ToolArgError("format must be 'text' or 'grid'")
        include_cursor = bool(args.get("include_cursor", False))
        return broker.inspect_pane_view(target, lines, fmt, include_cursor)

    if name == "send_keys":
        target = args.get("target")
        if not isinstance(target, str):
            raise ToolArgError("send_keys requires string target")
        text = args.get("text")
        if text is not None and not isinstance(text, str):
            raise ToolArgError("send_keys text must be a string")
        keys = args.get("keys") or []
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            raise ToolArgError("send_keys keys must be a list of strings")
        enter = bool(args.get("enter", False))
        # 語彙検証 (renga parity: 未知キー名は -32602)。
        for k in keys:
            if k.lower() not in _SEND_KEYS_VOCAB:
                raise ToolArgError(f"unknown key name {k!r}")
        return broker.send_keys_to(target, text, keys, enter)

    if name == "poll_events":
        since = args.get("since")
        if since is not None and not isinstance(since, str):
            raise ToolArgError("since must be a string cursor")
        timeout_ms = args.get("timeout_ms", 2000)
        if not isinstance(timeout_ms, int) or timeout_ms < 0:
            raise ToolArgError("timeout_ms must be a non-negative integer")
        types = args.get("types")
        if types is not None and (
            not isinstance(types, list) or not all(isinstance(t, str) for t in types)
        ):
            raise ToolArgError("types must be a list of strings")
        return _ok(broker.poll_events(since, timeout_ms, types))

    if name == "close_pane":
        target = args.get("target")
        if not isinstance(target, str):
            raise ToolArgError("close_pane requires string target")
        return broker.close_pane_target(target)

    if name == "set_pane_identity":
        target = args.get("target", "focused")
        if not isinstance(target, str):
            raise ToolArgError("set_pane_identity target must be a string")
        # three-state: key 不在=据置 / None=クリア / str=設定。
        has_name, has_role = "name" in args, "role" in args
        new_name = args.get("name")
        new_role = args.get("role")
        if has_name and new_name is not None:
            if not isinstance(new_name, str):
                raise ToolArgError("name must be a string or null")
            validate_pane_name(new_name)
        if has_role and new_role is not None and not isinstance(new_role, str):
            raise ToolArgError("role must be a string or null")
        return broker.set_pane_identity(
            target, has_name, new_name, has_role, new_role
        )

    # --- spawn ------------------------------------------------------------
    if name in ("spawn_claude_pane", "spawn_pane", "spawn_codex_pane"):
        direction = args.get("direction")
        if direction not in ("vertical", "horizontal"):
            raise ToolArgError("direction must be 'vertical' or 'horizontal'")
        target = args.get("target", "focused")
        pane_name = args.get("name")
        if pane_name is not None:
            if not isinstance(pane_name, str):
                raise ToolArgError("name must be a string")
            validate_pane_name(pane_name)
        role = args.get("role")
        if role is not None and not isinstance(role, str):
            raise ToolArgError("role must be a string")
        cwd = args.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ToolArgError("cwd must be a string")
        extra = args.get("args") or []
        if not isinstance(extra, list) or not all(isinstance(a, str) for a in extra):
            raise ToolArgError("args must be a list of strings")

        if name == "spawn_claude_pane":
            model = args.get("model")
            if model is not None and not isinstance(model, str):
                raise ToolArgError("model must be a string")
            permission_mode = args.get("permission_mode")
            if permission_mode is not None and not isinstance(permission_mode, str):
                raise ToolArgError("permission_mode must be a string")
            return broker.spawn_claude(
                direction, target, pane_name, role, model, permission_mode, extra, cwd
            )
        if name == "spawn_codex_pane":
            return broker.spawn_codex(direction, target, pane_name, role, extra, cwd)
        # spawn_pane (generic)
        command = args.get("command")
        if command is not None and not isinstance(command, str):
            raise ToolArgError("command must be a string")
        return broker.spawn_generic(direction, target, pane_name, role, command, cwd)

    # 到達不能 (catalogue にあるが分岐漏れ)。保険。
    return _err(f"[unknown_tool] {name}")
