"""Recorded model usage grouped by human turns, without timing inference.

Claude usage is repeated per content block. Codex token_count events can repeat
the last response. Deduplicate stable identities/cumulative checkpoints before
adding counts. Missing counts stay null; encrypted text length is never tokens.
"""
from __future__ import annotations

import bisect
import datetime as dt
import json
import uuid


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else None


def counts(usage, harness):
    base = number(usage.get("input_tokens"))
    cached = number(usage.get("cache_read_input_tokens" if harness == "claude" else "cached_input_tokens"))
    written = number(usage.get("cache_creation_input_tokens" if harness == "claude" else "cache_write_input_tokens"))
    if harness == "claude":
        # Anthropic's three input counters are disjoint, unlike OpenAI's.
        total = sum((base, cached, written)) if all(v is not None for v in (base, cached, written)) else None
    else:
        total = base
    output = number(usage.get("output_tokens"))
    detail = usage.get("output_tokens_details") or {}
    reasoning = number(usage.get("reasoning_output_tokens", detail.get("reasoning_tokens", detail.get("thinking_tokens"))))
    uncached = total - cached if total is not None and cached is not None and total >= cached else None
    remaining = output - reasoning if output is not None and reasoning is not None and output >= reasoning else None
    return {"input": total, "cached": cached, "cache_write": written, "uncached": uncached,
            "output": output, "reasoning": reasoning, "other_output": remaining}


class UsageIndex:
    def __init__(self):
        self.turns = [{"line": 1, "title": "Before first retained user prompt", "timestamp": None, "prompt": False}]
        self.steps = []
        self.by_id = {}
        self.checkpoints = set()
        self.checkpoint_steps = {}
        self.user_ids = set()
        self.tool_ids = set()
        self.tools = []
        self.last_total = None
        self.epoch = 0
        self.owner = None
        self.born = None
        self.ownership = "native"

    def provenance(self, obj, line):
        payload = obj.get("payload", obj)
        if obj.get("type") == "session_meta" and self.owner is None:
            self.owner = payload.get("id")
            source = payload.get("source") or payload.get("thread_source") or {}
            parent = payload.get("parent_thread_id") or payload.get("forked_from_id")
            if parent or isinstance(source, dict) and "subagent" in source:
                self.ownership = "unknown"
            try:
                self.born = int(dt.datetime.fromisoformat(
                    (payload.get("timestamp") or obj["timestamp"]).replace("Z", "+00:00")).timestamp())
            except (KeyError, TypeError, ValueError, AttributeError):
                pass
        started = None
        if obj.get("type") == "turn_context":
            try:
                identity = uuid.UUID(payload.get("turn_id", ""))
                if identity.version == 7:
                    started = (identity.int >> 80) // 1000
            except (ValueError, AttributeError):
                pass
        if obj.get("type") == "event_msg" and payload.get("type") == "task_started" and not str(payload.get("turn_id", "")).startswith("rollout-"):
            started = number(payload.get("started_at"))
        if started is not None and self.born is not None:
            ownership = "inherited" if started < self.born else "native"
            if ownership == "native" and self.ownership != "native":
                self.turns.append({"line": line, "title": "Agent task", "timestamp": obj.get("timestamp"), "prompt": False})
            self.ownership = ownership
        return self.ownership

    def consume(self, harness, obj, line, components, model):
        ownership = self.provenance(obj, line) if harness == "codex" else "native"
        users = [c for c in components if c.get("role") == "user" and c.get("origin") != "stream"
                 and c.get("visibility") in {"normal", "summary"} and c.get("kind") in {"message", "attachment"}]
        payload = obj.get("payload", obj)
        event_id = obj.get("uuid") or payload.get("id") or f"line:{line}"
        if users and event_id not in self.user_ids:
            self.user_ids.add(event_id)
            prompt = users[0].get("text", "Attachment").split("## My request:")[-1]
            self.turns.append({"line": line, "title": " ".join(prompt.split())[:140],
                               "timestamp": obj.get("timestamp"), "prompt": True})
        for component in components:
            if component.get("phase") == "request":
                identity = component.get("call_id") or (line, component["index"])
                if identity not in self.tool_ids:
                    self.tool_ids.add(identity)
                    self.tools.append({"line": line, "name": component.get("tool_name"),
                                       "timestamp": component.get("timestamp"), "ownership": ownership})
        if harness == "claude":
            message = obj.get("message") or {}
            if obj.get("type") != "assistant":
                return
            if message.get("model") == "<synthetic>":
                return
            identity = message.get("id") or obj.get("requestId") or obj.get("uuid") or f"line:{line}"
            usage = message.get("usage") or {}
            # Some providers expose several server-side iterations per response.
            iterations = usage.get("iterations") or []
            basis = "message usage" if usage else "usage not retained"
            identity = "message:" + identity
            step = {"line": line, "timestamp": obj.get("timestamp"), "model": message.get("model") or model,
                    "identity": identity, "request_id": obj.get("requestId"), "basis": basis,
                    "server_iterations": len(iterations) or None, **counts(usage, harness)}
            if identity in self.by_id:
                original = self.steps[self.by_id[identity]]
                if usage and any(original.get(key) != step.get(key) for key in counts(usage, harness)):
                    original["usage_line"] = line
                for key, value in step.items():
                    if key not in {"line", "timestamp"} and value is not None and (key != "basis" or usage):
                        original[key] = value
                return
        else:
            recorded = obj.get("type") == "token_usage_record"
            if not recorded and (obj.get("type") != "event_msg" or payload.get("type") != "token_count"):
                return
            info = payload.get("info") or {}
            if recorded and self.owner and payload.get("thread_id"):
                ownership = "native" if payload["thread_id"] == self.owner else "inherited"
            total = payload.get("thread_token_usage") if recorded else info.get("total_token_usage")
            usage = payload.get("usage") if recorded else info.get("last_token_usage")
            if usage and total and usage.get("input_tokens") == usage.get("output_tokens") == total.get("input_tokens") == total.get("output_tokens") == 0:
                self.last_total = total
                return
            identity = ("response:" + str(payload["response_id"])) if recorded and payload.get("response_id") else f"usage:{line}"
            if identity in self.by_id:
                return
            if total and self.last_total and any(number(v) is not None and v < self.last_total.get(k, 0) for k, v in total.items()):
                self.epoch += 1
            checkpoint = (ownership, self.epoch, json.dumps(total, sort_keys=True)) if total else None
            if checkpoint and checkpoint in self.checkpoints:
                # Prefer a response-ID receipt over a mirrored aggregate event.
                previous = self.checkpoint_steps.get(checkpoint)
                if recorded and previous is not None:
                    self.steps[previous].update({"identity": identity, "line": line,
                                                 "timestamp": obj.get("timestamp"), "basis": "response usage"})
                    self.by_id[identity] = previous
                return
            if checkpoint:
                self.checkpoints.add(checkpoint)
            basis = "response usage" if recorded else "last response usage"
            if not usage:
                if not total:
                    return
                if self.last_total and all(number(v) is not None and v >= self.last_total.get(k, 0) for k, v in total.items()):
                    usage = {key: value - self.last_total.get(key, 0) for key, value in total.items()}
                    basis = "cumulative delta · may span calls"
                else:
                    self.last_total = total
                    return
            self.last_total = total or self.last_total
            step = {"line": line, "timestamp": obj.get("timestamp"), "model": model,
                    "identity": identity, "basis": basis, "server_iterations": None,
                    **counts(usage, harness)}
            if checkpoint:
                self.checkpoint_steps[checkpoint] = len(self.steps)
        step["ownership"] = ownership
        self.by_id[identity] = len(self.steps)
        self.steps.append(step)

    def decorate(self, items, harness):
        positions = [step["line"] for step in self.steps]
        boundaries = [turn["line"] for turn in self.turns]
        for item in items:
            target = next((p for p in item.get("components", []) if p.get("phase") == "request"), item)
            if not (target.get("role") in {"assistant", "reasoning"} or target.get("phase") == "request"):
                continue
            position = self.by_id.get("message:" + str(target.get("model_call_id"))) if harness == "claude" else bisect.bisect_left(positions, target["line"])
            if position is None or position >= len(self.steps):
                continue
            step = self.steps[position]
            if bisect.bisect_right(boundaries, target["line"]) != bisect.bisect_right(boundaries, step["line"]):
                continue
            item["usage"] = {key: step.get(key) for key in ("input", "output", "reasoning", "cached", "identity", "line", "ownership")}
            item["usage"]["association"] = "message ID" if harness == "claude" else "recorded event order"

    def public(self, line, after=0):
        turns = []
        starts = [turn["line"] for turn in self.turns]
        grouped = [[] for _ in starts]
        tools = [0] * len(starts)
        for step in self.steps:
            grouped[bisect.bisect_right(starts, step["line"]) - 1].append(step)
        for tool in self.tools:
            if tool["ownership"] == "native":
                tools[bisect.bisect_right(starts, tool["line"]) - 1] += 1
        keys = ("input", "cached", "cache_write", "uncached", "output", "reasoning")
        for i, turn in enumerate(self.turns):
            steps = grouped[i]
            if not turn["prompt"] and not steps and not tools[i]:
                continue
            native = [s for s in steps if s["ownership"] == "native"]
            sums = {key: sum(s[key] for s in native if s[key] is not None) for key in keys}
            known = {key: sum(s[key] is not None for s in native) for key in keys}
            turns.append({**turn, "end_line": starts[i + 1] - 1 if i + 1 < len(starts) else None,
                          "steps": len(native), "tool_calls": tools[i], "totals": sums, "known": known,
                          "inherited_steps": sum(s["ownership"] == "inherited" for s in steps),
                          "unattributed_steps": sum(s["ownership"] == "unknown" for s in steps)})
        if not turns:
            return {"turns": [], "selected": None, "steps": [], "next_after": None}
        selected = next((t for t in reversed(turns) if t["line"] <= line), turns[0])
        position = bisect.bisect_right(starts, selected["line"]) - 1
        candidates = [s for s in grouped[position] if s["line"] > after]
        page = candidates[:200]
        shown_turns = turns[-500:]
        if selected not in shown_turns:
            shown_turns.insert(0, selected)
        return {"turns": shown_turns, "selected": selected, "steps": page,
                "turn_count": len(turns), "next_after": page[-1]["line"] if len(candidates) > 200 else None}
