"""Snapshot <-> plain dict (JSON) conversion. Exact round trip: from_dict(to_dict(s)) == s.

Decoding is strict: every field is checked against its declared type (str/int/float/bool/None,
Literal values, tuple shapes, string-keyed scalar maps, nested records). Any mismatch raises
`SnapshotFormatError`. Unknown keys are ignored; a missing field is an error unless the record
declares a default.

Agent compat (FORMAT_VERSION stays 1): an Agent is written with its canonical keys (provider,
session_id, status_quality, status_note, meta, ...) plus the legacy aliases older agentree versions
read and wrote:
  kind          = provider
  oc_session_id = session_id if provider == "opencode", else null
  cc_session_id = session_id if provider == "claude", else null
  port          = meta["port"] if provider == "opencode" (an int), else null
  api_ok        = meta["api_ok"] if provider == "opencode" (true only if it is true), else false
Decoding normalizes legacy records first: no provider / kind means opencode; session_id and
(opencode) meta are taken from the aliases when the canonical keys are absent. Every alias that
is present must agree with the canonical values, else the record is rejected. A legacy record
(no status_quality) gets its quality inferred from the old fields.
"""
from __future__ import annotations

import collections.abc
import dataclasses
import functools
import math
import types
import typing
from typing import Any, Literal, Mapping, Union

from agentree.types import Agent, Repo, Snapshot

FORMAT_VERSION = 1


class SnapshotFormatError(ValueError):
    """Structurally valid JSON that does not match the snapshot schema."""


# --------------------------------------------------------------------------- encode

# provider ids the legacy session-id aliases belong to
_SESSION_ALIASES: dict[str, str] = {"oc_session_id": "opencode", "cc_session_id": "claude"}
_LEGACY_KEYS = ("kind", *_SESSION_ALIASES, "port", "api_ok")
_LEGACY_TYPES: dict[str, Any] = {"kind": str, "oc_session_id": str | None, "cc_session_id": str | None,
                                 "port": int | None, "api_ok": bool}
_META_PROVIDER = "opencode"           # the provider whose meta port / api_ok the aliases mirror


def legacy_aliases(provider: str, session_id: str | None, meta: Mapping[str, Any]) -> dict[str, Any]:
    """The legacy alias values written next to an Agent's canonical keys."""
    port = meta.get("port") if provider == _META_PROVIDER else None
    return {
        "kind": provider,
        **{k: (session_id if provider == p else None) for k, p in _SESSION_ALIASES.items()},
        "port": port if isinstance(port, int) and not isinstance(port, bool) else None,
        "api_ok": provider == _META_PROVIDER and meta.get("api_ok") is True,
    }


def _enc(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        d = {f.name: _enc(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
        if isinstance(obj, Agent):
            d.update(legacy_aliases(obj.provider, obj.session_id, obj.meta))
        return d
    if isinstance(obj, (list, tuple)):
        return [_enc(x) for x in obj]
    if isinstance(obj, Mapping):
        return {str(k): _enc(v) for k, v in obj.items()}
    return obj


def to_dict(snapshot: Snapshot) -> dict:
    d = _enc(snapshot)
    d["version"] = FORMAT_VERSION
    return d


# --------------------------------------------------------------------------- strict decode

@functools.cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _fail(path: str, msg: str) -> SnapshotFormatError:
    return SnapshotFormatError(f"{path or '<root>'}: {msg}")


def decode_value(tp: Any, value: Any, path: str = "") -> Any:
    """Decode a JSON value as type `tp`; raises SnapshotFormatError on any mismatch."""
    if tp is Any:
        return value
    if isinstance(tp, typing.TypeAliasType):          # `type X = ...` (e.g. ExtraValue)
        return decode_value(tp.__value__, value, path)
    if tp is type(None):
        if value is not None:
            raise _fail(path, f"expected null, got {type(value).__name__}")
        return None
    if dataclasses.is_dataclass(tp) and isinstance(tp, type):
        return decode_record(tp, value, path)
    origin = typing.get_origin(tp)
    if origin is Union or origin is types.UnionType:
        arms = typing.get_args(tp)
        if value is None:
            if type(None) in arms:
                return None
            raise _fail(path, "unexpected null")
        errors: list[str] = []
        for arm in arms:
            if arm is type(None):
                continue
            try:
                return decode_value(arm, value, path)
            except SnapshotFormatError as e:
                errors.append(str(e))
        raise _fail(path, "no union arm matched (" + "; ".join(errors) + ")")
    if origin is Literal:
        allowed = typing.get_args(tp)
        if not isinstance(value, str) or value not in allowed:
            raise _fail(path, f"expected one of {allowed!r}, got {value!r}")
        return value
    if origin is tuple:
        args = typing.get_args(tp)
        if not isinstance(value, (list, tuple)):
            raise _fail(path, f"expected array, got {type(value).__name__}")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(decode_value(args[0], x, f"{path}[{i}]") for i, x in enumerate(value))
        if len(value) != len(args):
            raise _fail(path, f"expected {len(args)} items, got {len(value)}")
        return tuple(decode_value(a, x, f"{path}[{i}]") for i, (a, x) in enumerate(zip(args, value)))
    if origin is dict or origin is collections.abc.Mapping:
        kt, vt = typing.get_args(tp) or (Any, Any)
        if not isinstance(value, Mapping):
            raise _fail(path, f"expected object, got {type(value).__name__}")
        out: dict[Any, Any] = {}
        for k, x in value.items():
            if kt is str and not isinstance(k, str):
                raise _fail(path, f"expected string keys, got {type(k).__name__}")
            out[k] = decode_value(vt, x, f"{path}.{k}")
        return out
    if tp is bool:
        if not isinstance(value, bool):
            raise _fail(path, f"expected bool, got {type(value).__name__}")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _fail(path, f"expected int, got {type(value).__name__}")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _fail(path, f"expected number, got {type(value).__name__}")
        if not math.isfinite(value):
            raise _fail(path, f"expected a finite number, got {value!r}")
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise _fail(path, f"expected string, got {type(value).__name__}")
        return value
    raise _fail(path, f"unsupported field type {tp!r}")


def _legacy_quality(rec: Mapping[str, Any], provider: str, meta: Mapping[str, Any]) -> dict[str, Any]:
    """status_quality / status_note for a record written before they existed (from the old
    api_ok / status_source fields; mirrors what the providers set today)."""
    if provider == "opencode":
        if meta.get("api_ok") is True:
            return {"status_quality": "exact"}
        return {"status_quality": "unavailable", "status_note": "Live status unavailable"}
    if provider == "claude":
        src = rec.get("status_source")
        if src == "hooks":
            return {"status_quality": "exact"}
        if src in ("registry", "transcript"):
            return {"status_quality": "approximate", "status_note": "Status is approximate"}
    return {}


def normalize_agent(value: Mapping[str, Any], path: str = "") -> dict[str, Any]:
    """An Agent record with legacy aliases folded into the canonical keys (and dropped).
    Raises SnapshotFormatError for wrongly typed or conflicting aliases."""
    rec = dict(value)

    def field_path(k: str) -> str:
        return f"{path}.{k}" if path else k
    legacy = {k: decode_value(_LEGACY_TYPES[k], rec.pop(k), field_path(k)) for k in _LEGACY_KEYS if k in rec}
    canonical = "provider" in rec
    if canonical:
        provider = decode_value(str, rec["provider"], field_path("provider"))
    else:
        provider = legacy.get("kind", "opencode")
    if "session_id" in rec:
        session_id = decode_value(str | None, rec["session_id"], field_path("session_id"))
    else:
        session_id = next((legacy.get(k) for k, p in _SESSION_ALIASES.items() if p == provider), None)
    if "meta" in rec:
        meta = decode_value(_hints(Agent)["meta"], rec["meta"], field_path("meta"))
    elif provider == _META_PROVIDER:
        meta = {"port": legacy.get("port"), "api_ok": legacy.get("api_ok", False)}
    else:
        meta = {}
    expect = legacy_aliases(provider, session_id, meta)
    for k, v in legacy.items():
        if v != expect[k] or type(v) is not type(expect[k]):
            raise _fail(field_path(k), f"conflicts with the canonical fields ({k}={v!r}, expected {expect[k]!r})")
    if "status_quality" not in rec and not canonical:
        rec.update(_legacy_quality(rec, provider, meta))
    rec.update(provider=provider, session_id=session_id, meta=meta)
    return rec


def decode_record(cls: type, value: Any, path: str = "") -> Any:
    """Decode a dict into dataclass `cls`, strictly. Unknown keys are ignored."""
    if not isinstance(value, Mapping):
        raise _fail(path, f"expected object for {cls.__name__}, got {type(value).__name__}")
    if cls is Agent:
        value = normalize_agent(value, path)
    hints = _hints(cls)
    kw: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        fpath = f"{path}.{f.name}" if path else f.name
        if f.name in value:
            kw[f.name] = decode_value(hints[f.name], value[f.name], fpath)
        elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
            raise _fail(fpath, "missing required field")
    try:
        return cls(**kw)
    except (TypeError, ValueError) as e:     # defensive: should be unreachable after the checks
        raise _fail(path, str(e)) from e


def from_dict(d: Mapping[str, Any]) -> Snapshot:
    """Inverse of to_dict. Raises SnapshotFormatError (a ValueError) on malformed input."""
    if not isinstance(d, Mapping):
        raise _fail("", f"expected object, got {type(d).__name__}")
    v = d.get("version", FORMAT_VERSION)
    if type(v) is not int or v != FORMAT_VERSION:
        raise SnapshotFormatError(f"unsupported snapshot version {v!r}")
    return decode_record(Snapshot, d)


def repo_to_dict(repo: Repo) -> dict:
    return _enc(repo)


def repo_from_dict(d: Mapping[str, Any]) -> Repo:
    return decode_record(Repo, d)
