"""Deterministic enforcement hooks (design §6): quality gates that cannot be
talked out of.

Three guarantees, enforced by code rather than prompt goodwill:

- **Preference quarantine.** No agent outside the allowlist can Read/Grep/Glob
  its way to preferences.yaml — including via a search whose *root* covers it.
- **Write scope.** An agent can only write inside the workspace subtrees its
  contract names; run-control files are off-limits to every agent.
- **Source hygiene.** Every WebFetch is cached content-addressed under
  sources/ with a SourceRecord and an audit-log line, after a sanitizer strips
  tool-call-like / instruction-injection patterns. The sanitizer protects
  *re-injection from the cache* into later contexts; the fetching agent already
  saw the raw content — its defense is the prompt-level untrusted-input rule.

The gate functions are pure (SDK-free, unit-testable); ``build_hooks`` wraps
them into claude_agent_sdk HookMatcher callbacks closed over one contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deeper.schemas.common import SourceRecord, SourceTier
from deeper.workspace import Workspace, _atomic_write, append_log_line

from .contracts import AgentContract

# Design §6 names screener and synthesist; synthesist arrives in Phase C but the
# allowlist is code, not prompt text, so it is complete from day one.
PREFERENCE_READERS = frozenset({"screener", "synthesist"})
PREFERENCES_FILE = "preferences.yaml"

# Files no agent may write, regardless of its contract's allowed paths: the
# orchestrator owns run control, S0 owns the quarantined preferences.
PROTECTED_FILES = frozenset({"state.json", "config.yaml", PREFERENCES_FILE})

AUDIT_LOG = "logs/web-audit.jsonl"


# -- path logic (Windows-safe, filesystem-free) ---------------------------------


def _norm(p: str | Path, root: Path) -> str:
    """Normalize a tool-supplied path for comparison: join relative paths to the
    workspace root (the SDK's cwd), collapse `..`, unify separators and case.
    Deliberately NOT Path.resolve(): that hits the filesystem and follows
    symlinks/junctions, making the gate's answer depend on disk state."""
    q = Path(p)
    if not q.is_absolute():
        q = root / q
    return os.path.normcase(os.path.normpath(str(q)))


def _covers(root: str, target: str) -> bool:
    """True when `target` is `root` or lies underneath it (both normalized)."""
    return target == root or target.startswith(root + os.sep)


# -- pure gates: return a denial reason, or None to allow -----------------------


def quarantine_gate(
    role: str, tool_name: str, tool_input: dict[str, Any], workspace_root: Path
) -> str | None:
    """Deny any Read/Grep/Glob whose target or search root covers
    preferences.yaml, unless the role is on the allowlist (P-quarantine)."""
    if role in PREFERENCE_READERS:
        return None
    prefs = _norm(PREFERENCES_FILE, workspace_root)
    if tool_name == "Read":
        target = tool_input.get("file_path", "")
        if target and _norm(target, workspace_root) == prefs:
            return (
                f"preferences.yaml is quarantined: only {sorted(PREFERENCE_READERS)} "
                f"may read it, and this agent is '{role}' (design §6)"
            )
        return None
    if tool_name in ("Grep", "Glob"):
        # A search rooted at or above preferences.yaml reads its content even
        # though no argument names it — deny by scope, not by filename match.
        search_root = _norm(tool_input.get("path") or ".", workspace_root)
        if _covers(search_root, prefs) or _covers(prefs, search_root):
            return (
                f"{tool_name} scope covers quarantined preferences.yaml: narrow the "
                f"search to a subdirectory (agent '{role}' may not read preferences)"
            )
    return None


def write_scope_gate(
    tool_name: str,
    tool_input: dict[str, Any],
    workspace_root: Path,
    allowed_write_paths: tuple[str, ...],
) -> str | None:
    """Deny file writes outside the contract's allowed workspace subtrees."""
    key = "notebook_path" if tool_name == "NotebookEdit" else "file_path"
    raw = tool_input.get(key, "")
    if not raw:
        return f"{tool_name} call carries no {key}; refusing to write blind"
    target = _norm(raw, workspace_root)
    root = os.path.normcase(os.path.normpath(str(workspace_root)))
    if not _covers(root, target):
        return f"write target {raw!r} is outside the run workspace"
    for protected in PROTECTED_FILES:
        if target == _norm(protected, workspace_root):
            return f"{protected} is orchestrator-owned; no agent may write it"
    for allowed in allowed_write_paths:
        if _covers(_norm(allowed, workspace_root), target):
            return None
    return (
        f"write target {raw!r} is outside this agent's allowed subtree(s) "
        f"{list(allowed_write_paths) or '(none)'}"
    )


# -- source hygiene --------------------------------------------------------------

# Patterns that could smuggle instructions or fake tool traffic from a fetched
# page into a later agent context (design §6). Best-effort defense-in-depth,
# paired with the prompts' untrusted-web-content rule.
_STRIP_MARKER = "[stripped: potential-injection]"
# Hidden-text carriers removed OUTRIGHT (no marker): invisible characters a
# page can hide directives in — zero-width spaces/joiners, bidi controls, BOM,
# word joiners, the Unicode tag block (U+E0000-E007F encodes invisible ASCII).
_INVISIBLE_CHARS = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]|[\U000e0000-\U000e007f]"
)
_INJECTION_PATTERNS = [
    # A system-reminder span; unclosed → conservatively drop to end of text.
    re.compile(r"<system-reminder>.*?(</system-reminder>|\Z)", re.DOTALL | re.IGNORECASE),
    # HTML comments: invisible in any rendered view, so a reader would never
    # see what the model would — the classic hidden-text injection carrier.
    # Unclosed → conservatively drop to end of text.
    re.compile(r"<!--.*?(-->|\Z)", re.DOTALL),
    # Tool-call-like tags (opening or closing) stripped individually — never as
    # spans, so a stray closing tag cannot swallow the rest of the page.
    re.compile(
        r"</?[^<>\n]{0,60}\b(function_calls|function_results|invoke|antml)\b[^<>\n]*>",
        re.IGNORECASE,
    ),
    re.compile(r"^[ \t]*(human|assistant|system)[ \t]*:[^\n]*$", re.MULTILINE | re.IGNORECASE),
    re.compile(
        r"^[^\n]*ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+instructions[^\n]*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Any line carrying tool-traffic JSON keys (line-based: robust to nesting).
    re.compile(r'^[^\n]*"(tool_use|tool_name|tool_input|hookSpecificOutput)"[^\n]*$', re.MULTILINE),
]


def sanitize_source_text(text: str) -> str:
    """Strip tool-call-like, instruction-injection, and hidden-text patterns
    from fetched content before it is cached — cached text gets re-injected
    into other agent contexts (the verifier's and analyst's cache-first
    re-fetches, Gate-C challenge verification), and must arrive as data, never
    directives. Invisible characters go first: a directive spelled in
    zero-width or tag-block characters must not survive to dodge the visible
    patterns."""
    text = _INVISIBLE_CHARS.sub("", text)
    for pattern in _INJECTION_PATTERNS:
        text = pattern.sub(_STRIP_MARKER, text)
    return text


def _extract_fetch_text(tool_response: Any) -> str | None:
    """Pull displayable text out of a WebFetch tool_response defensively — the
    SDK does not pin this payload's shape."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        for key in ("content", "result", "text", "output"):
            value = tool_response.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):  # content-block lists
                texts = [b.get("text") for b in value if isinstance(b, dict)]
                joined = "\n".join(t for t in texts if t)
                if joined:
                    return joined
    return None


def cache_web_fetch(workspace: Workspace, role: str, url: str, content: str | None) -> str | None:
    """Content-address one fetched page into sources/ and append the audit log.

    Returns the content hash, or None when no text was extractable (the audit
    line is written regardless — every fetch is accounted for)."""
    now = datetime.now(UTC)
    content_hash: str | None = None
    if content:
        sanitized = sanitize_source_text(content)
        content_hash = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
        content_path = workspace.path(f"sources/{content_hash}.md")
        if not content_path.exists():  # content-addressed: identical fetch = no-op
            _atomic_write(content_path, sanitized)
            workspace.write_artifact(
                f"sources/{content_hash}.record.yaml",
                SourceRecord(
                    url=url,
                    # The hook cannot judge source quality; T3 is the conservative
                    # floor. Agents carry their own tier judgments in SourceRefs.
                    tier=SourceTier.T3,
                    retrieved_at=now,
                    content_hash=content_hash,
                ),
            )
    append_log_line(
        workspace.path(AUDIT_LOG),
        json.dumps(
            {"url": url, "hash": content_hash, "role": role, "at": now.isoformat()},
            ensure_ascii=False,
        ),
    )
    return content_hash


# -- SDK wiring -------------------------------------------------------------------


def _deny(input_data: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": input_data.get("hook_event_name", "PreToolUse"),
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def build_hooks(contract: AgentContract, workspace: Workspace) -> dict[Any, Any]:
    """The hooks dict for ClaudeAgentOptions, closed over one contract.

    Imports claude_agent_sdk lazily — this is one of only two SDK touchpoints
    in the package (the other is LiveDispatcher._invoke). Callback signatures
    are Any-typed at this boundary: the SDK's hook TypedDicts are structurally
    incompatible with plain dicts, and the pure gates carry the real types."""
    from claude_agent_sdk import HookMatcher

    async def quarantine_cb(input_data: Any, tool_use_id: Any, context: Any) -> Any:
        reason = quarantine_gate(
            contract.role,
            input_data.get("tool_name", ""),
            input_data.get("tool_input") or {},
            workspace.root,
        )
        return _deny(input_data, reason) if reason else {}

    async def write_scope_cb(input_data: Any, tool_use_id: Any, context: Any) -> Any:
        reason = write_scope_gate(
            input_data.get("tool_name", ""),
            input_data.get("tool_input") or {},
            workspace.root,
            contract.allowed_write_paths,
        )
        return _deny(input_data, reason) if reason else {}

    async def source_cache_cb(input_data: Any, tool_use_id: Any, context: Any) -> Any:
        url = (input_data.get("tool_input") or {}).get("url", "")
        if url:
            cache_web_fetch(
                workspace,
                contract.role,
                url,
                _extract_fetch_text(input_data.get("tool_response")),
            )
        return {}

    return {
        "PreToolUse": [
            HookMatcher(matcher="Read|Grep|Glob", hooks=[quarantine_cb]),
            HookMatcher(matcher="Write|Edit|MultiEdit|NotebookEdit", hooks=[write_scope_cb]),
        ],
        "PostToolUse": [HookMatcher(matcher="WebFetch", hooks=[source_cache_cb])],
    }
