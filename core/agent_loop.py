"""In-bot agent-loop frontend over the ops registry ("world pattern").

The second frontend next to core/mcp_server.py: generates pydantic-ai
`Tool`s mechanically from the registry's typed op declarations so a
tool-calling model invoked via `!gpt` can ACTUALLY perform Discord actions
instead of narrating them. All resolution/permission/serialization comes
from core/ops.py; the only policy here is the loop's:

- ACTOR: the invoking user's Member (the real commands.Context passes
  straight through as the OpContext) — never the bot, never guild.me.
  Ops the invoking user can't pass permission gates for fail closed and
  the gate's error is returned INTO the loop as a tool error.
- IN-GUILD CONFINEMENT: allowed_guild_ids is exactly {ctx.guild.id};
  id-resolved targets outside the invoking guild are refused.
- send_message always uses allowed_mentions=none.
- SELF logs contain only op/actor/surface/status/budget; other ops retain params.

Wired up by cogs/gpt.py when a guild's resolved agent universe (the
super-admin `agent_ops_whitelist` ceiling narrowed by the per-guild
`agent_ops_gate`, see core/agent_gate) is non-empty. With no exposed bot tools, GPT uses the same agent runner
without constructing registry tools.
"""
from __future__ import annotations

import logging
from typing import Any, List

from pydantic_ai import Tool

from core.ops import Op, OpScope, registry
from core.agent_gate import self_call_allowed, resolve_agent_tools, guild_call_requires_admin

# Soft tool budget per agentic run, enforced HERE (not via pydantic-ai's
# UsageLimits) so exhaustion degrades into a model-authored answer instead
# of an exception. pydantic-ai's limiter is preemptive about parallel
# batches — a model that answers "check every channel" with 8 parallel
# search calls would blow a hard tool_calls_limit before a single call ran
# (observed live 2026-07-21) and the user gets a canned failure. Instead:
# calls past the budget are REFUSED with an answer-now error, and the last
# few results carry a countdown so the model lands before the cliff.
AGENT_TOOL_BUDGET = 8
# Results start carrying `tool_calls_remaining` when this many are left.
BUDGET_COUNTDOWN_AT = 3

LAST_CALL_NOTE = (
    "That was your LAST action tool call. Finish the run now with your final "
    "answer or stay_quiet if no reply is needed — further action calls will "
    "be refused."
)
BUDGET_EXHAUSTED_ERROR = (
    "Tool budget exhausted — no more action tool calls will run. Finish NOW "
    "using the results you already have: give your final answer, or call "
    "stay_quiet if no reply is needed. Be honest about anything you could "
    "not check; do not hide failures that need explanation."
)


def build_agent_tools(ctx: Any, logger: logging.Logger,
                      op_names: List[str],
                      tool_budget: int = AGENT_TOOL_BUDGET,
                      *, gate_check=None, on_tool_call=None, validity_check=None) -> List[Tool]:
    """Build the pydantic-ai tool list for one agentic `!gpt` run.

    `ctx` is the live commands.Context of the invoking user — it IS the
    OpContext (duck-typed), so permission gates evaluate the invoking
    user's real Member, in their real guild.

    `op_names` is the resolved policy set. Guilds support GUILD and SELF;
    DMs support only SELF with an empty allowed-guild set. SELF policy is
    checked both here and live at dispatch without privileged bypass.

    `gate_check` re-evaluates the per-guild admin gate at DISPATCH from live
    policy, not from a boolean snapshotted at build time (codex review
    2026-08-20): an admin tightening an op to "admin only" mid-run must bind
    on the next call.

    All tools from one call share a `tool_budget` counter (see the
    AGENT_TOOL_BUDGET comment above for why enforcement lives here).
    `on_tool_call` marks a run as unsafe to replay before any dispatch.
    """
    for name in op_names:
        op = registry.require(name)
        if op.scope not in ({OpScope.GUILD, OpScope.SELF} if ctx.guild else {OpScope.SELF}):
            raise ValueError("Unsupported agent tool scope.")
        if op.scope == OpScope.SELF and op.name not in resolve_agent_tools(ctx, {OpScope.SELF}):
            raise ValueError("SELF tool is not enabled for this invoker.")
    allowed = frozenset({ctx.guild.id}) if ctx.guild else frozenset()
    budget = {"used": 0, "cap": tool_budget}
    return [_make_agent_tool(registry.require(op_name), ctx, allowed, logger,
                             budget, gate_check=gate_check, on_tool_call=on_tool_call, validity_check=validity_check)
            for op_name in op_names]


def _make_agent_tool(op: Op, ctx: Any, allowed: frozenset,
                     logger: logging.Logger, budget: dict,
                     gate_check=None, on_tool_call=None, validity_check=None) -> Tool:
    async def tool_fn(**raw) -> dict:
        if on_tool_call is not None:
            on_tool_call()
        if validity_check is not None and not validity_check():
            logger.warning("agent-op %s actor=%s refused: turn invalid", op.name, ctx.author.id)
            return {"ok": False, "error": "Turn is no longer valid."}
        # Per-guild admin gate, re-evaluated LIVE at dispatch (not a snapshot):
        # a server admin set this op to "admin only" for agent use and the
        # invoking user is not an admin. Refuse before spending budget or
        # touching Discord. (Admins and "everyone"-gated ops pass; the op's own
        # hardcoded floor is still enforced by registry.call_ids below.)
        denied = (not self_call_allowed(ctx, op) if op.scope == OpScope.SELF else
                  (gate_check or (lambda name: guild_call_requires_admin(ctx, name)))(op.name))
        if denied:
            logger.warning(
                "agent-op %s actor=%s REFUSED (guild gate: admin only)",
                op.name, ctx.author.id,
            )
            return {"ok": False,
                    "error": ("SELF tool is not permitted for this invoker."
                              if op.scope == OpScope.SELF else
                              f"'{op.name}' is set to admin-only for the agent in "
                              "this server; a server admin must ask for it.")}
        budget["used"] += 1
        remaining = budget["cap"] - budget["used"]
        if remaining < 0:
            logger.info(
                "agent-op %s actor=%s REFUSED (tool budget %s exhausted)",
                op.name, ctx.author.id, budget["cap"],
            )
            return {"ok": False, "error": BUDGET_EXHAUSTED_ERROR}
        # Identity belt: dispatch resolves by NAME but schema/scope were
        # captured from `op`; inert while the cog set is fixed at boot (#86).
        if registry.get(op.name) is not op:
            logger.info(
                "agent-op %s actor=%s REFUSED (op re-registered mid-run)",
                op.name, ctx.author.id,
            )
            return {"ok": False,
                    "error": f"Tool '{op.name}' changed while this run was in "
                             "flight (it was re-registered). Call refused; "
                             "ask again to use the updated tool."}
        # send_message never pings: enforced by the op itself (see
        # core/ops.py send_message — never-ping is the registry default).
        result = await registry.call_ids(op.name, ctx, allowed_guild_ids=allowed,
                                         **raw)
        if op.scope == OpScope.SELF:
            logger.info("agent-op %s actor=%s surface=%s status=%s remaining=%s",
                        op.name, ctx.author.id, "guild" if ctx.guild else "DM",
                        "ok" if result.ok else "error", remaining)
        else:
            logger.info("agent-op %s actor=%s params=%s -> %s",
                        op.name, ctx.author.id, raw,
                        "ok" if result.ok else f"error: {result.error}")
        payload = op.result_payload(result)
        if remaining <= BUDGET_COUNTDOWN_AT:
            payload["tool_calls_remaining"] = remaining
            if remaining == 0:
                payload["budget_note"] = LAST_CALL_NOTE
        return payload

    return Tool.from_schema(
        tool_fn,
        name=op.name,
        description=op.description,
        json_schema=op.to_json_schema(),
        sequential=op.sequential,
    )


# Single-token sentinel a nudged model uses to say "false alarm, my reply
# was fine" — the original reply is then posted unchanged, so a false flag
# costs one silent API call and the channel never sees a second message.
NUDGE_FALSE_ALARM_SENTINEL = "OK"

# Corrective user turn for a run whose reply NAMES a tool but executed zero
# tool calls (the narrated-call signature). Option 2 is what keeps false
# positives invisible: the model self-clears with the sentinel instead of
# restating (or arguing with) its own reply in public.
NUDGE_PROMPT = (
    "[SYSTEM CHECK — automated, not from a user] Your reply above names a "
    "tool, but ZERO tool calls were made, so nothing was executed and your "
    "reply has NOT been posted yet. Choose one:\n"
    "1. If you meant to perform an action: emit the real function call(s) "
    "through the native tool-call channel NOW, then finish with the final "
    "reply text for the channel, or call stay_quiet if no reply is needed. "
    "Do not describe the calls in text.\n"
    "2. If your reply was already a complete answer that needed no tool: "
    f"respond with exactly {NUDGE_FALSE_ALARM_SENTINEL} (nothing else) and "
    "your original reply will be posted unchanged."
)


def looks_like_narrated_call(text, tool_names):
    """True when a zero-tool-call reply reads as a verbalized tool invocation.

    Deliberately narrow: fires only on an ENABLED tool's snake_case name
    appearing verbatim, or explicit "run tool" phrasing — strings that don't
    occur in genuine chat prose. (The 2026-07-05 attempt matched everyday
    words like "search"/"done"/"I'll" and flagged normal answers constantly;
    that regex is the cautionary tale, not the template.) False positives
    that remain (e.g. the user asks "what tools do you have?" and the reply
    honestly lists them) are absorbed by the sentinel path in _run_agentic.
    """
    if not text:
        return False
    lowered = text.lower()
    if "run tool" in lowered or "run the tool" in lowered:
        return True
    return any(name in lowered for name in tool_names)


def is_nudge_false_alarm(text):
    """Did the nudged re-run answer with the bare false-alarm sentinel?"""
    return (text or "").strip().strip(".!").upper() == NUDGE_FALSE_ALARM_SENTINEL



async def run_agent_turn(llm, provider_config, api_messages, ctx, tool_names,
                         closing_request, metadata=None, tool_budget=AGENT_TOOL_BUDGET,
                         *, logger=None, gate_check=None, on_tool_call=None,
                         validity_check=None, max_retries=None, log_usage=None):
    """Execute one turn with one tool set/counter across nudge and fallback."""
    logger = logger or ctx.bot.logger
    log_usage = log_usage or (lambda response: log_agentic_usage(logger, response))
    retry_options = {} if max_retries is None else {"max_retries": max_retries}
    # Snapshot sensitivity before execution: unload/re-registration must not
    # make a provider exception containing a prior SELF exchange loggable.
    redact_errors = any(registry.get(n) and registry.get(n).scope == OpScope.SELF
                        for n in tool_names)
    try:
        return await _run_agent_turn(llm, provider_config, api_messages, ctx, tool_names,
                                     closing_request, metadata, tool_budget, logger,
                                     gate_check, on_tool_call, validity_check,
                                     retry_options, log_usage)
    except Exception:
        if redact_errors:
            raise RuntimeError("Agent turn failed (SELF exchange redacted).") from None
        raise


async def _run_agent_turn(llm, provider_config, api_messages, ctx, tool_names,
                          closing_request, metadata, tool_budget, logger,
                          gate_check, on_tool_call, validity_check,
                          retry_options, log_usage):
    from pydantic_ai import ToolOutput
    from pydantic_ai.exceptions import UsageLimitExceeded

    tools = build_agent_tools(
        ctx, logger, tool_names,
        gate_check=gate_check, tool_budget=tool_budget, on_tool_call=on_tool_call, validity_check=validity_check,
    ) if tool_names else []
    # Local to this invocation: no registry entry, MCP exposure, or state
    # shared with other runs. An output function terminates the Agent;
    # an ordinary function tool would require another model response.
    quiet = False

    async def stay_quiet() -> str:
        """End this turn without an automatic final Discord reply.

        Use when no reply is needed. Complete actions and inspect their
        results first; do not hide failures that need explanation.
        Previously sent messages remain. Takes no arguments.
        """
        nonlocal quiet
        quiet = True
        return ""

    output_type = [str, ToolOutput(stay_quiet, name="stay_quiet")]
    available_names = [t.name for t in tools] + ["stay_quiet"]
    logger.info(
        f"agentic gpt run: guild={getattr(ctx.guild, 'id', 'DM')} channel={ctx.channel.id} "
        f"actor={ctx.author.id} provider={provider_config.provider} "
        f"model={provider_config.model} tools={available_names}"
    )
    command_turn = closing_request
    try:
        response = await llm.run_agent(
            provider_config,
            api_messages,
            tools=tools,
            output_type=output_type,
            metadata=metadata, **retry_options,
            # The command text is repeated as the closing user turn so the
            # actionable instruction is unambiguous even when the channel
            # scrape attributed the invoking message oddly (e.g. a
            # bot-authored mention landing in history as an assistant turn).
            user_prompt=command_turn,
            max_tool_calls=tool_budget * 2,
        )
        log_usage(response)

        # Narrated-call backstop: the reply names an enabled tool but zero
        # tools ran — almost certainly a verbalized invocation (observed
        # live 2026-07-21: "run tool search_history with channel_id is
        # ..."). One corrective re-run; the sentinel path keeps false
        # positives invisible (original reply posts unchanged), so the
        # channel sees exactly one message either way.
        tool_calls = response.usage.tool_calls if response.usage else 0
        if not quiet and tool_calls == 0 and looks_like_narrated_call(response.text, available_names):
            logger.info(
                "agentic reply names a tool but made no tool calls — nudging once")
            retry = await llm.run_agent(
                provider_config,
                api_messages + [
                    {"role": "user", "content": command_turn},
                    {"role": "assistant", "content": response.text},
                ],
                tools=tools,
                output_type=output_type,
                metadata=metadata, **retry_options,
                user_prompt=NUDGE_PROMPT,
                max_tool_calls=tool_budget * 2,
            )
            log_usage(retry)
            if is_nudge_false_alarm(retry.text):
                logger.info("nudge was a false alarm — keeping the original reply")
            else:
                response = retry
    except UsageLimitExceeded:
        # Only reachable if the model ignores 2x the soft budget worth of
        # refusals (pathological). Even then: a model-authored best-effort
        # answer, never a canned failure string.
        logger.warning("agentic run hit hard tool cap")
        quiet = False
        fallback = await llm.run_agent(provider_config, api_messages + [
            {"role": "user", "content": command_turn},
            {"role": "user", "content": (
                "[SYSTEM] The tool budget ran out before this request "
                "finished. Answer in plain text with your best effort "
                "from what you know, and say briefly what you could not "
                "verify.")},
        ], tools=[], metadata=metadata, **retry_options, output_type=output_type)
        log_usage(fallback)
        return None if quiet else fallback.text

    if quiet:
        logger.info(
            "agentic stay_quiet: guild=%s channel=%s actor=%s",
            getattr(ctx.guild, "id", "DM"), ctx.channel.id, ctx.author.id,
        )
        return None
    return response.text


def log_agentic_usage(logger, response):
    if response.usage:
        logger.info(
            f"agentic usage: provider={response.usage.provider} model={response.usage.model} "
            f"prompt={response.usage.prompt_tokens} completion={response.usage.completion_tokens} "
            f"total={response.usage.total_tokens} est_cost_usd={response.usage.estimated_cost_usd} "
            f"tool_calls={response.usage.tool_calls}"
        )



MESSAGE_ID_GUIDANCE = (
    "Message IDs are harness-supplied metadata identifying existing Discord "
    "messages, not part of anyone's message text or a format to continue. "
    "Use them only in structured tool arguments, taking visible IDs directly "
    "rather than guessing or searching. Your final reply is just the "
    "message body, posted in the channel answering the trigger; Discord assigns "
    "its ID after posting."
)


def build_agentic_guidance(tool_names, guild_id=None, channel_id=None):
    """System-prompt lines for agentic runs: available tools, target ids,
    and — critically — the MECHANICS of tool invocation.

    The mechanics block exists because models (observed live: grok narrating
    "run tool search_history with channel_id is ..." as plain text) sometimes
    verbalize an intended call instead of emitting it. The loop is a
    pydantic-ai Agent over the OpenAI-compatible function-calling API: a real
    call rides the structured tool_calls channel of the response; a text-only
    response ends the run and is posted to Discord verbatim. The guidance
    states that contract explicitly instead of hoping the model infers it.

    Module-level (not a cog method) so a test harness can import and
    exercise the exact shipped text without constructing a bot.
    """
    lines = [
        "",
        *([
            "You have REAL Discord tools available: " + ", ".join(tool_names) + ".",
            f"- Current guild id: {guild_id}. Current channel id: {channel_id}.",
            "- The invoking user and trigger message IDs are in the closing request context.",
            "- " + MESSAGE_ID_GUIDANCE,
        ] if guild_id is not None else [
            "You have tools available: " + ", ".join(tool_names) + ".",
            "- SELF tools act only for the invoking user; never supply a user identity.",
        ]),
        *([
            "- For an explicit Discord reply to a specific message, call "
            "send_message with reference_message_id, then stay_quiet.",
        ] if "send_message" in tool_names else []),
        "",
        "HOW TOOL CALLS WORK (mechanics — this part is exact, not stylistic):",
        "- You are in an agent loop over the chat-completions API with function "
        "calling enabled. The ONLY way to run a tool is to emit a native "
        "function call — the tool's name plus a JSON arguments object — through "
        "the API's structured tool-call channel. A tool call is NOT text; "
        "nothing you write in your visible reply can execute anything.",
        "- Every response you produce is one of three things: (a) one or "
        "more function calls — they execute for real and their results come "
        "back for you to continue with; (b) plain text — the run ENDS "
        "immediately and that text is posted to the channel as your reply; "
        "or (c) the terminal stay_quiet call — the run ends WITHOUT a final "
        "channel reply. Decide which one BEFORE responding.",
        "- Call stay_quiet with no arguments when no final reply is needed "
        "(for example, the user requested only a reaction or asked you not "
        "to reply). Finish any requested actions and inspect their results "
        "BEFORE calling it. Do not hide failed actions that need explanation. "
        "It only suppresses the automatic final reply; messages already sent "
        "by tools remain. Never use an empty text response to signal silence.",
        "- Because of (b), writing out an intended call as words — e.g. "
        "\"run tool search_history with channel_id is 1234 contains is pizza\" "
        "— executes nothing: the run just ends and that sentence gets posted "
        "to the channel verbatim, where everyone sees a malfunction. If you "
        "intend a tool call, EMIT the function call instead of describing it.",
        *([
            "- Worked example: someone asks \"do i play factorio\" and the answer "
            "isn't in the visible history. Correct: emit the function call "
            "search_history with arguments {\"channel_ids\": [" + str(channel_id) +
            "], \"author_id\": <invoking_user_id>, \"contains\": \"factorio\", "
            "\"limit\": 100}, wait for the results, then answer in plain text. "
            "Wrong: any reply that merely talks about searching.",
        ] if "search_history" in tool_names else []),
        "",
        "- When an action would genuinely help (react, reply elsewhere, edit, "
        "search, list), use the matching tool. Prefer doing over describing.",
        "- If no tool fits the request, just reply normally in text — a plain "
        "conversational answer is a perfectly good response, and most messages "
        "only need one. Don't reach for a tool when the user just wants an answer.",
        "- Only claim you did something if you ACTUALLY called the tool for it in "
        "this turn and saw its result. Never say a reaction was added, a message "
        "sent/edited/deleted, or history searched unless that tool ran — don't "
        "pretend or role-play a tool result. If you couldn't do it, say so plainly.",
    ]
    # Per-tool behavioral notes ride on the op declarations themselves
    # (core/ops.py `agent_guidance`), so guidance stays in lockstep with
    # whatever set of tools a guild has enabled — no hand-maintained
    # if-ladder here to drift out of sync with the registry.
    per_tool = []
    for name in tool_names:
        op = registry.get(name)
        if op is not None and op.agent_guidance:
            per_tool.append(f"- {op.agent_guidance}")
    if per_tool:
        lines.append("")
        lines.append("Per-tool notes:")
        lines.extend(per_tool)
    return lines
