"""A bounded model -> approved tool -> evidence -> model execution loop.

Inspired by AstrBot's ToolLoopAgentRunner. Uses the existing XZBot JSON contract
and traced provider boundary, not a second event queue or conversation history.
Only scoped local knowledge/history and configured public search are executable here.
"""
from dataclasses import dataclass, field
import asyncio
from . import conversation_config, knowledge_lookup, history_lookup, web_search
from .prompts import prompt
from .providers import ProviderError


@dataclass
class ExecutionBudget:
    max_tool_rounds: int
    max_model_calls: int
    model_calls: int = 0
    tools: list[str] = field(default_factory=list)

    def snapshot(self, *, next_call=False):
        return {"model_call": self.model_calls + int(next_call), "max_model_calls": self.max_model_calls,
                "tool_rounds": len(self.tools), "max_tool_rounds": self.max_tool_rounds,
                "tools": list(self.tools)}

    def consume(self):
        if self.model_calls >= self.max_model_calls:
            raise ProviderError("execution_budget_exhausted")
        self.model_calls += 1


async def run_dialogue(engine, provider, messages, settings, kwargs):
    try:
        async with asyncio.timeout(180):
            return await _run_dialogue(engine, provider, messages, settings, kwargs)
    except TimeoutError:
        if number := kwargs["refs"].get("_trace_id"):
            engine.trajectories.finish(number, "failed", error="execution_timeout")
        raise ProviderError("execution_timeout") from None


async def _run_dialogue(engine, provider, messages, settings, kwargs):
    budget = ExecutionBudget(**conversation_config.read(engine.content_root)["execution"])
    kwargs = dict(kwargs, context_generation=kwargs.get("context_generation", engine.generation(kwargs["scope"])))
    evidence = None
    while True:
        can_tool = len(budget.tools) < budget.max_tool_rounds and budget.model_calls + 1 < budget.max_model_calls
        search_allowed = can_tool and settings.web_search_enabled and "web_search" not in budget.tools
        knowledge_allowed = can_tool and "knowledge_lookup" not in budget.tools
        history_allowed = can_tool and "history_lookup" not in budget.tools
        if not (search_allowed or knowledge_allowed or history_allowed):
            messages[0]["content"] += "\n" + prompt(engine.content_root, "execution_done", settings)
        answer = await engine._complete_traced_once(provider, messages, settings,
            allow_search=search_allowed, allow_knowledge=knowledge_allowed, allow_history=history_allowed, execution=budget, **kwargs)
        query = knowledge_lookup.query(answer, kwargs["kind"], settings) if knowledge_allowed else None
        history_term=history_lookup.query(answer,kwargs['kind'],settings) if history_allowed else None
        if history_term:
            budget.tools.append('history_lookup')
            messages,parent=history_lookup.prepare(engine,messages,settings,kwargs,history_term)
        elif query:
            budget.tools.append("knowledge_lookup")
            messages, parent = knowledge_lookup.prepare(engine, messages, settings, kwargs, query)
        elif search_allowed and engine._wants_search(answer, kwargs["kind"], settings):
            budget.tools.append("web_search")
            messages, evidence, parent = await engine._search_evidence(answer, messages, settings, kwargs)
        else:
            if evidence:
                answer["_web_evidence"] = evidence
                displayed = web_search.cite(answer, evidence, show=settings.web_search_show_sources or
                    web_search.wants_sources(kwargs.get("request_text"))).get("text", "")
                if isinstance(displayed, str):
                    from .core import clean_reply
                    engine.trajectories.record_response(kwargs["refs"]["_trace_id"], clean_reply(displayed), source_text=displayed)
            return answer
        # All follow-up model calls retain the initial privacy generation, scoped
        # references, actual image provenance and the same cloud quota checks.
        kwargs.update(trace_id=None, related_id=parent)
