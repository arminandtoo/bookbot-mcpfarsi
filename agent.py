"""LangGraph ReAct agent over three MCP servers: 30book (print), Taaghche (ebook/audio), our wishlist."""
import asyncio
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from fastmcp import Client
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, trim_messages
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

load_dotenv()


def pass_env(*keys):
    # stdio servers only inherit a safe default env, so our API keys never reach third-party servers
    return {k: os.environ[k] for k in keys if k in os.environ}


MCP_CONFIG = {"mcpServers": {
    "30book": {"command": "uvx", "args": ["30book-mcp"], "env": pass_env("SIBOOK_MCP_PROXY")},
    "taaghche": {"command": "uvx", "args": ["taaghche-mcp"], "env": pass_env("TAAGHCHE_MCP_PROXY")},
    "wishlist": {"command": sys.executable, "args": [str(Path(__file__).with_name("wishlist_server.py"))],
                 "env": pass_env("WISHLIST_DB")},
}}
USER_SCOPED = "wishlist_"  # tools whose user_id we fill from Telegram, never from the model

SYSTEM_PROMPT = """You are a Persian book assistant inside a Telegram bot. Always reply in Persian.

Tools:
- 30book_*: printed books from 30book.com.
- taaghche_*: ebooks and audiobooks from Taaghche.
- wishlist_*: the user's own saved books.

Rules:
- Only state books, prices, ratings and reviews that came from tool results. Never invent them.
- For recommendations ("best novel", "what's trending") use bestsellers / browse / reviews tools.
- When a specific book comes up, check both stores when useful and compare the printed price (30book)
  with the ebook/audiobook price (Taaghche). All prices are in Toman.
- Link every book you mention as <a href="URL">title</a>, with the exact URL from the tool results.
- Save to, list or remove from the wishlist only when the user asks.
- Format for Telegram HTML: only <b>, <i> and <a href>; no Markdown, no other tags; escape &, < and >
  in normal text. Keep it short: a numbered list, one or two lines per book with its price."""


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


def to_schema(tool) -> dict:
    params = copy.deepcopy(tool.input_schema)
    if tool.name.startswith(USER_SCOPED):
        params["properties"].pop("user_id")
        params["required"].remove("user_id")
    return {"type": "function",
            "function": {"name": tool.name, "description": tool.description or "", "parameters": params}}


async def run_tool(client, call: dict, user_id: str) -> ToolMessage:
    args = call["args"]
    if call["name"].startswith(USER_SCOPED):
        args = {**args, "user_id": user_id}  # overrides anything the model tried to pass
    print(f"  -> {call['name']}({args})")
    try:
        result = await client.call_tool(call["name"], args)
        text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    except Exception as e:  # let the model see the error and recover
        text = f"Error: {e}"
    return ToolMessage(content=text, tool_call_id=call["id"])


async def build_agent(client):
    llm = ChatOpenAI(model=os.getenv("MODEL", "openai/gpt-4o-mini"), temperature=0.2,
                     api_key=os.environ["OPENROUTER_API_KEY"], base_url="https://openrouter.ai/api/v1")
    llm = llm.bind_tools([to_schema(t) for t in await client.list_tools()])

    async def agent(state: AgentState):

        history = trim_messages(state["messages"], max_tokens=20_000, token_counter="approximate",
                                start_on="human")
        return {"messages": [await llm.ainvoke([SystemMessage(SYSTEM_PROMPT), *history])]}

    async def tools(state: AgentState, config):
        user_id = config["configurable"]["user_id"]
        calls = state["messages"][-1].tool_calls
        return {"messages": await asyncio.gather(*(run_tool(client, c, user_id) for c in calls))}

    def route(state: AgentState):
        return "tools" if state["messages"][-1].tool_calls else END

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent)
    graph.add_node("tools", tools)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route, ["tools", END])
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=InMemorySaver())


def books_in(reply: str, tool_outputs: list[str]) -> list[dict]:
    """Books from this turn's tool results whose link made it into the reply, in reply order."""
    found = {}

    def walk(obj):
        if isinstance(obj, dict):
            if "url" in obj and "title" in obj:
                found.setdefault(obj["url"], obj)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    for text in tool_outputs:
        try:
            walk(json.loads(text))
        except ValueError:  # error text, not JSON
            pass
    hits = {u: m.start() for u in found if (m := re.search(re.escape(u) + r"(?!\d)", reply))}
    return [found[u] for u in sorted(hits, key=hits.get)]


async def ask(agent, text: str, chat_id, user_id, on_tools=None) -> tuple[str, list[dict]]:
    """Run one turn. on_tools(names) is awaited whenever the model calls tools (for live status)."""
    config = {"configurable": {"thread_id": str(chat_id), "user_id": str(user_id)}}
    reply, tool_outputs = "", []
    async for update in agent.astream({"messages": [HumanMessage(text)]}, config, stream_mode="updates"):
        for out in update.values():
            for msg in out["messages"]:
                if isinstance(msg, ToolMessage):
                    tool_outputs.append(msg.content)
                elif msg.tool_calls:
                    if on_tools:
                        await on_tools([c["name"] for c in msg.tool_calls])
                else:
                    reply = msg.content
    return reply, books_in(reply, tool_outputs)


async def main():  # CLI for testing without Telegram
    async with Client(MCP_CONFIG) as client:
        agent = await build_agent(client)
        while (q := input("\nYou: ").strip()) not in ("q", "exit"):
            if q:
                reply, _ = await ask(agent, q, "cli", "cli")
                print("Bot:", reply)


if __name__ == "__main__":
    asyncio.run(main())
