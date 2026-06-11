"""
PoE2 Chat Engine — Gemini AI + Multi-MCP Tool Integration

Core engine that separates Gemini chat from MCP tool integration.
- Gemini is initialized independently (just needs API key + model).
- MCP servers are optional and can be started/stopped independently.
- Tools can be filtered per-user preference before registration.
- Model can be changed without restarting MCP connections.
"""

import asyncio
import json
import logging
import traceback
from typing import Optional

from google import genai
from google.genai import types as genai_types
from mcp import ClientSession
from mcp.client.session_group import ClientSessionGroup
from mcp.client.stdio import StdioServerParameters

logger = logging.getLogger(__name__)

# ── Model presets ──────────────────────────────────────────────
MODEL_PRESETS = [
    {
        "id": "gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "description": "Fast and capable for build analysis",
        "badge": "Recommended",
        "tier": "Free tier",
        "stability": "Stable",
    },
    {
        "id": "gemini-2.5-flash-lite-preview-06-2025",
        "label": "Gemini 2.5 Flash-Lite",
        "description": "Low latency for quick questions",
        "badge": "Fastest",
        "tier": "Free tier",
        "stability": "Preview",
    },
    {
        "id": "gemini-2.0-flash",
        "label": "Gemini 2.0 Flash",
        "description": "Compatible fallback for basic usage",
        "badge": "Legacy",
        "tier": "Free tier",
        "stability": "Stable",
    },
]

DEFAULT_MODEL = "gemini-2.5-flash"

SYSTEM_PROMPT = """You are a Path of Exile 2 build advisor AI assistant.

You have access to MCP tools from multiple servers that can analyze characters, inspect game data (gems, mods, passive tree),
compare builds, validate support gem combinations, check live economy data, search the wiki, and much more.

Guidelines:
- When a user asks about their character, use `analyze_character` or `import_poe_ninja_url` first.
- When asked about gems, use `list_all_supports`, `list_all_spells`, `inspect_support_gem`, or `inspect_spell_gem`.
- When asked about mods, use `search_mods_by_stat`, `get_mod_tiers`, or `inspect_mod`.
- When asked about mechanics, use `explain_mechanic` or `get_formula`.
- Always validate support gem combinations with `validate_support_combination` before recommending them.
- Provide specific, actionable advice with numbers when possible.
- Reference game data from tools — don't guess stats.
- For currency prices, use `poe2_currency_prices` or `poe2_currency_check`.
- For wiki lookups, use `poe2_wiki_search` or `poe2_wiki_page`.
- For item price checks, use `poe2_item_price` or `poe2_exchange_top`.

Available tool categories:
1. Character Analysis: analyze_character, import_poe_ninja_url, compare_to_top_players
2. Gem Data: list_all_supports, list_all_spells, inspect_support_gem, inspect_spell_gem, validate_support_combination
3. Passive Tree: list_all_keystones, inspect_keystone, list_all_notables, inspect_passive_node, get_ascendancy_info
4. Item Mods: list_all_mods, inspect_mod, search_mods_by_stat, get_mod_tiers, validate_item_mods, get_available_mods
5. Base Items: list_all_base_items, inspect_base_item
6. Knowledge: explain_mechanic, get_formula
7. Path of Building: import_pob, export_pob, get_pob_code
8. Trade: search_items, search_trade_items
9. Live Economy: poe2_currency_prices, poe2_currency_check, poe2_item_price, poe2_exchange_top
10. Wiki & DB: poe2_wiki_search, poe2_wiki_page, poe2_db_lookup, poe2_meta_builds
11. Game Integration: poe2_log_summary, poe2_pob_decode, poe2_pob_local_builds, poe2_pob_compare, poe2_parse_item
"""


def json_schema_to_gemini_params(schema: dict, depth: int = 0) -> dict:
    """Convert JSON Schema (from MCP tool) to Gemini Schema format."""
    if not isinstance(schema, dict):
        return {"type": genai_types.Type.STRING}

    TYPE_MAP = {
        "string": genai_types.Type.STRING,
        "number": genai_types.Type.NUMBER,
        "integer": genai_types.Type.INTEGER,
        "boolean": genai_types.Type.BOOLEAN,
        "array": genai_types.Type.ARRAY,
        "object": genai_types.Type.OBJECT,
    }

    result = {}
    json_type = schema.get("type", "string")
    result["type"] = TYPE_MAP.get(json_type.lower(), genai_types.Type.STRING)

    if "description" in schema:
        result["description"] = schema["description"]
    if "enum" in schema:
        result["enum"] = schema["enum"]
    if "properties" in schema and depth < 5:
        result["properties"] = {
            k: json_schema_to_gemini_params(v, depth + 1)
            for k, v in schema["properties"].items()
        }
    if "required" in schema:
        result["required"] = schema["required"]
    if "items" in schema and depth < 5:
        result["items"] = json_schema_to_gemini_params(schema["items"], depth + 1)

    return result


class ChatEngine:
    """Core engine: Gemini AI (required) + optional MCP tools."""

    def __init__(self):
        # Gemini state
        self.client: Optional[genai.Client] = None
        self.chat = None
        self._api_key: Optional[str] = None
        self._model: str = DEFAULT_MODEL
        self._gemini_ready: bool = False

        # MCP state
        self._group: Optional[ClientSessionGroup] = None
        self._all_mcp_tools: dict[str, object] = {}  # All tools from MCP servers
        self.server_statuses: dict[str, dict] = {}

        # Tool filtering
        self._enabled_tool_names: Optional[set] = None  # None = all enabled

        # Gemini tool declarations (rebuilt when tools or model change)
        self._gemini_declarations: list = []

    # ── Properties ─────────────────────────────────────────────

    @property
    def gemini_ready(self) -> bool:
        return self._gemini_ready

    @property
    def mcp_connected(self) -> bool:
        return self._group is not None and len(self._all_mcp_tools) > 0

    @property
    def model(self) -> str:
        return self._model

    @property
    def all_tool_names(self) -> list[str]:
        return sorted(self._all_mcp_tools.keys())

    @property
    def active_tool_names(self) -> list[str]:
        if self._enabled_tool_names is None:
            return self.all_tool_names
        return sorted(self._enabled_tool_names & set(self._all_mcp_tools.keys()))

    @property
    def active_tool_count(self) -> int:
        return len(self.active_tool_names)

    @property
    def total_tool_count(self) -> int:
        return len(self._all_mcp_tools)

    # ── Gemini (independent of MCP) ────────────────────────────

    async def init_gemini(self, api_key: str, model: str = None):
        """Initialize Gemini client and start a chat session (no MCP needed)."""
        self._api_key = api_key
        self._model = model or DEFAULT_MODEL

        logger.info(f"Initializing Gemini with model {self._model}")
        self.client = genai.Client(api_key=api_key)

        # Build tool declarations from current MCP tools (if any)
        self._build_gemini_declarations()

        # Create chat session
        tools_config = None
        if self._gemini_declarations:
            tools_config = [genai_types.Tool(function_declarations=self._gemini_declarations)]

        self.chat = self.client.chats.create(
            model=self._model,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=tools_config,
                temperature=0.7,
            ),
        )
        self._gemini_ready = True
        logger.info(f"Gemini ready (model: {self._model}, tools: {len(self._gemini_declarations)})")

    async def change_model(self, model: str):
        """Change the Gemini model and restart the chat session."""
        if not self._api_key:
            raise RuntimeError("No API key set")
        self._model = model
        self._gemini_ready = False
        await self.init_gemini(self._api_key, model)
        logger.info(f"Model changed to {model}")

    # ── MCP (optional, independent of Gemini) ──────────────────

    async def start_mcp(self, servers: list[dict], env_extra: dict[str, str] | None = None):
        """Connect to MCP servers. Can be called after init_gemini().

        Args:
            servers: List of server config dicts (id, name, command, args, enabled).
            env_extra: Optional extra env vars to pass to MCP server processes
                       (merged with os.environ). Used for POESESSID injection.
        """
        logger.info(f"Starting MCP with {len(servers)} server(s)")

        # Clean up previous MCP
        await self.stop_mcp()

        self._group = ClientSessionGroup()
        await self._group.__aenter__()
        self.server_statuses = {}

        enabled_servers = [s for s in servers if s.get("enabled", True)]
        if not enabled_servers:
            logger.warning("No MCP servers enabled")
            return

        for server in enabled_servers:
            sid = server.get("id", "unknown")
            sname = server.get("name", sid)
            command = server.get("command", "")
            args = server.get("args", [])

            if not command:
                self.server_statuses[sid] = {
                    "connected": False, "error": "No command",
                    "name": sname, "tool_count": 0,
                }
                continue

            try:
                logger.info(f"Connecting to '{sname}': {command} {args}")
                server_env = None
                if env_extra:
                    import os as _os
                    server_env = {**_os.environ, **env_extra}
                server_params = StdioServerParameters(command=command, args=args, env=server_env)
                await self._group.connect_to_server(server_params)

                self.server_statuses[sid] = {
                    "connected": True, "error": None,
                    "name": sname,
                    "tool_count": len(self._group.tools),
                }
                logger.info(f"Server '{sname}' connected — {len(self._group.tools)} total tools")

            except Exception as e:
                logger.error(f"Server '{sname}' failed: {e}")
                self.server_statuses[sid] = {
                    "connected": False, "error": str(e),
                    "name": sname, "tool_count": 0,
                }

        connected = any(st["connected"] for st in self.server_statuses.values())
        if not connected:
            logger.warning("All MCP servers failed")
        else:
            self._all_mcp_tools = dict(self._group.tools)
            logger.info(f"MCP ready: {len(self._all_mcp_tools)} tools from {len([s for s in self.server_statuses.values() if s['connected']])} server(s)")

        # Rebuild Gemini declarations with new tools and restart chat
        if self._gemini_ready:
            self._build_gemini_declarations()
            self._restart_chat()

    async def stop_mcp(self):
        """Disconnect from all MCP servers."""
        try:
            if self._group:
                await self._group.__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"MCP cleanup error: {e}")
        finally:
            self._group = None
            self._all_mcp_tools = {}
            self.server_statuses = {}

        # Rebuild without tools and restart chat
        if self._gemini_ready:
            self._build_gemini_declarations()
            self._restart_chat()

    # ── Tool filtering ─────────────────────────────────────────

    def set_enabled_tools(self, tool_names: set[str] | None):
        """Set which tools are enabled. None = all tools enabled."""
        self._enabled_tool_names = tool_names

        # Rebuild declarations and restart chat
        if self._gemini_ready:
            self._build_gemini_declarations()
            self._restart_chat()

    # ── Internal helpers ───────────────────────────────────────

    def _build_gemini_declarations(self):
        """Convert enabled MCP tools to Gemini FunctionDeclaration objects."""
        self._gemini_declarations = []

        active_tools = {}
        for name in self.active_tool_names:
            if name in self._all_mcp_tools:
                active_tools[name] = self._all_mcp_tools[name]

        for tool_name, tool in active_tools.items():
            try:
                params_schema = {}
                if tool.inputSchema:
                    params_schema = json_schema_to_gemini_params(tool.inputSchema)
                if "type" not in params_schema:
                    params_schema["type"] = genai_types.Type.OBJECT

                fd = genai_types.FunctionDeclaration(
                    name=tool_name,
                    description=tool.description or "",
                    parameters=genai_types.Schema(**params_schema) if params_schema else None,
                )
                self._gemini_declarations.append(fd)
            except Exception as e:
                logger.warning(f"Skipping tool {tool_name}: schema conversion error: {e}")

    def _restart_chat(self):
        """Restart the Gemini chat session with current declarations and model."""
        if not self.client:
            return

        tools_config = None
        if self._gemini_declarations:
            tools_config = [genai_types.Tool(function_declarations=self._gemini_declarations)]

        self.chat = self.client.chats.create(
            model=self._model,
            config=genai_types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                tools=tools_config,
                temperature=0.7,
            ),
        )
        logger.info(f"Chat restarted (model: {self._model}, tools: {len(self._gemini_declarations)})")

    # ── Chat ───────────────────────────────────────────────────

    async def send_message(self, message: str) -> dict:
        """
        Send a user message through the Gemini tool-calling loop.

        Returns dict with:
            - "type": "text" | "error"
            - "content": response text
            - "tool_calls": list of tool calls made (for UI display)
        """
        if not self._gemini_ready:
            return {"type": "error", "content": "Gemini not initialized"}

        tool_calls_log = []

        try:
            response = await asyncio.to_thread(self.chat.send_message, message)

            max_iterations = 20
            iteration = 0

            while iteration < max_iterations:
                iteration += 1

                if not response.candidates:
                    return {"type": "text", "content": "(No response from AI)", "tool_calls": tool_calls_log}

                candidate = response.candidates[0]
                if not candidate.content or not candidate.content.parts:
                    break

                function_calls = []
                text_parts = []

                for part in candidate.content.parts:
                    if part.function_call:
                        function_calls.append(part.function_call)
                    elif part.text:
                        text_parts.append(part.text)

                if not function_calls:
                    final_text = "\n".join(text_parts) if text_parts else "(No text response from AI)"
                    return {"type": "text", "content": final_text, "tool_calls": tool_calls_log}

                # Execute tool calls via MCP group (or skip if MCP not connected)
                function_response_parts = []
                for fc in function_calls:
                    tool_name = fc.name
                    tool_args = dict(fc.args) if fc.args else {}

                    tool_calls_log.append({"name": tool_name, "args": tool_args})
                    logger.info(f"Tool call: {tool_name}({json.dumps(tool_args, default=str)[:200]})")

                    if not self._group:
                        result_text = "Error: MCP tools are not connected. Enable MCP to use build analysis tools."
                    else:
                        try:
                            result = await self._group.call_tool(tool_name, tool_args)
                            result_texts = []
                            for content in result.content:
                                if hasattr(content, "text"):
                                    result_texts.append(content.text)
                            result_text = "\n".join(result_texts) if result_texts else "(empty result)"
                        except Exception as e:
                            logger.error(f"Tool error: {tool_name}: {e}")
                            result_text = f"Error: {str(e)}"

                    if len(result_text) > 30000:
                        result_text = result_text[:30000] + "\n... (truncated)"

                    function_response_parts.append(
                        genai_types.Part.from_function_response(
                            name=tool_name,
                            response={"result": result_text},
                        )
                    )

                response = await asyncio.to_thread(self.chat.send_message, function_response_parts)

            return {"type": "text", "content": "Reached maximum tool calling iterations.", "tool_calls": tool_calls_log}

        except Exception as e:
            logger.error(f"Chat error: {e}\n{traceback.format_exc()}")
            return {"type": "error", "content": f"Error: {str(e)}", "tool_calls": tool_calls_log}

    # ── Cleanup ────────────────────────────────────────────────

    async def close(self):
        """Clean up all resources."""
        try:
            if self._group:
                await self._group.__aexit__(None, None, None)
        except Exception as e:
            logger.warning(f"MCP cleanup error: {e}")
        finally:
            self._group = None
            self._all_mcp_tools = {}
            self._gemini_ready = False
            self.client = None
            self.chat = None
            logger.info("ChatEngine closed")
