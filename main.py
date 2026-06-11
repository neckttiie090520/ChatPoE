"""
ChatPoE — Desktop Chat App

Minimal Windows desktop app using pywebview + Gemini + MCP.
- Gemini API key stored securely via OS keyring (Windows Credential Manager).
- MCP servers are optional — chat works without them.
- Chat history persisted in local SQLite under %LOCALAPPDATA%.
- Auto-connects on launch when settings exist.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Optional

import keyring
import webview

sys.path.insert(0, str(Path(__file__).parent))

from chat_engine import ChatEngine, MODEL_PRESETS, DEFAULT_MODEL
from history import HistoryRepository, _auto_title, friendly_tool_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("poe2-chat")

# ── Paths ──────────────────────────────────────────────────────
THIS_DIR = Path(__file__).parent
SETTINGS_FILE = THIS_DIR / "settings.json"
KEYRING_SERVICE = "poe2-chat"
KEYRING_USERNAME = "gemini-api-key"
KEYRING_TRADE_USER = "poe-trade-session"

# ── Data directory (Local AppData) ─────────────────────────────
from platformdirs import PlatformDirs
DIRS = PlatformDirs(appname="ChatPoE", appauthor="nextzus", roaming=False, ensure_exists=True)
DATA_DIR = DIRS.user_data_path
BACKUP_DIR = DATA_DIR / "backups"
EXPORT_DIR = DATA_DIR / "exports"
BACKUP_DIR.mkdir(exist_ok=True)
EXPORT_DIR.mkdir(exist_ok=True)


# ── MCP Server Detection ───────────────────────────────────────

def detect_mcp_command() -> str:
    """Auto-detect the best way to start poe2-mcp server."""
    if shutil.which("poe2-mcp"):
        return "poe2-mcp"

    for py_ver in ["Python314", "Python313", "Python312", "Python311"]:
        p = Path.home() / "AppData" / "Roaming" / "Python" / py_ver / "Scripts" / "poe2-mcp.exe"
        if p.exists():
            return str(p)

    sibling_path = THIS_DIR.parent / "poe2-mcp" / "launch.py"
    if sibling_path.exists():
        return f'python "{sibling_path}"'

    return "poe2-mcp"


def detect_live_server_path() -> Optional[str]:
    """Auto-detect the poe2-mcp-server (Node.js) dist/index.js path."""
    paths_to_check = [
        THIS_DIR.parent / "poe2-mcp-server" / "dist" / "index.js",
        THIS_DIR / "node_modules" / "poe2-mcp-server" / "dist" / "index.js",
    ]
    for p in paths_to_check:
        if p.exists():
            return str(p)
    return None


def get_default_servers() -> list[dict]:
    """Return default MCP server configurations with auto-detected paths."""
    servers = []

    servers.append({
        "id": "poe2-mcp",
        "name": "PoE2 Build Data",
        "command": detect_mcp_command(),
        "args": [],
        "enabled": True,
        "builtin": True,
    })

    node_path = shutil.which("node")
    live_path = detect_live_server_path()
    servers.append({
        "id": "poe2-live",
        "name": "PoE2 Live Data",
        "command": node_path or "node",
        "args": [live_path] if live_path else [],
        "enabled": bool(node_path and live_path),
        "builtin": True,
    })

    return servers


# ── Secure API Key Storage ─────────────────────────────────────

def get_saved_api_key() -> Optional[str]:
    """Retrieve API key from OS keyring. Returns None if not set."""
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except Exception:
        return None


def save_api_key_secure(api_key: str):
    """Store API key in OS keyring."""
    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, api_key)


def remove_saved_api_key():
    """Remove API key from OS keyring."""
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass


def mask_api_key(key: str) -> str:
    """Return masked version of API key for display (first 4 + ... + last 4)."""
    if not key or len(key) < 12:
        return "••••••••"
    return key[:4] + "••••" + key[-4:]


# ── Trade Session Storage ──────────────────────────────────────

def get_saved_poesessid() -> Optional[str]:
    """Retrieve POESESSID from OS keyring. Returns None if not set."""
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_TRADE_USER)
    except Exception:
        return None


def save_poesessid(poesessid: str):
    """Store POESESSID in OS keyring."""
    keyring.set_password(KEYRING_SERVICE, KEYRING_TRADE_USER, poesessid)


def remove_poesessid():
    """Remove POESESSID from OS keyring."""
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_TRADE_USER)
    except keyring.errors.PasswordDeleteError:
        pass


def mask_poesessid(sid: str) -> str:
    """Return masked version of POESESSID for display (first 8 + ...)."""
    if not sid or len(sid) < 12:
        return "••••••••"
    return sid[:8] + "..."


# ── Settings File (non-secret config only) ─────────────────────

def load_settings() -> dict:
    """Load non-secret settings from settings.json."""
    defaults = {
        "model": DEFAULT_MODEL,
        "mcp_enabled": True,
        "mcp_servers": get_default_servers(),
        "enabled_tools": None,  # None = all tools enabled
    }

    if SETTINGS_FILE.exists():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            # Merge with defaults
            for key in defaults:
                if key in raw:
                    defaults[key] = raw[key]
            # Migration: old format had mcp_command string -> mcp_servers array
            if "mcp_servers" not in raw and "mcp_command" in raw:
                defaults["mcp_servers"] = [
                    {"id": "poe2-mcp", "name": "PoE2 Build Data",
                     "command": raw["mcp_command"], "args": [],
                     "enabled": True, "builtin": True}
                ]
            # Migration: old format had api_key as base64 -> move to keyring
            if "api_key" in raw and raw["api_key"] and not get_saved_api_key():
                try:
                    import base64
                    decoded = base64.b64decode(raw["api_key"]).decode("utf-8")
                    if decoded and len(decoded) > 10:
                        save_api_key_secure(decoded)
                        logger.info("Migrated API key from settings.json to keyring")
                except Exception as e:
                    logger.warning(f"Failed to migrate API key: {e}")
            return defaults
        except Exception:
            pass
    return defaults


def save_settings(settings: dict):
    """Save non-secret settings to settings.json."""
    to_save = {k: v for k, v in settings.items() if k != "api_key"}
    SETTINGS_FILE.write_text(json.dumps(to_save, indent=2), encoding="utf-8")


# ── Model Discovery ────────────────────────────────────────────

def validate_api_key(api_key: str) -> dict:
    """Validate an API key by trying to list models. Returns {valid, models, error}."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=api_key)
        response = client.models.list(config=types.ListModelsConfig(query_base=True, page_size=100))

        available = []
        for model in response.page:
            available.append({
                "name": model.name,
                "display_name": getattr(model, "display_name", ""),
                "input_token_limit": getattr(model, "input_token_limit", 0),
                "output_token_limit": getattr(model, "output_token_limit", 0),
            })

        return {"valid": True, "models": available, "error": None}

    except Exception as e:
        return {"valid": False, "models": [], "error": str(e)}


# ── API Bridge ─────────────────────────────────────────────────

class Api:
    """Bridge between the webview UI and Python backend."""

    def __init__(self):
        self.engine: Optional[ChatEngine] = None
        self.window: Optional[webview.Window] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self.history = HistoryRepository(DATA_DIR / "history.sqlite3")
        self._active_conv_id: Optional[str] = None
        self._pw_cancel: bool = False

    def _ensure_loop(self):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
            t = threading.Thread(target=self._run_loop, daemon=True)
            t.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ── Settings ───────────────────────────────────────────────

    def get_app_state(self) -> dict:
        """Get full app state for UI initialization."""
        api_key = get_saved_api_key()
        settings = load_settings()
        poesessid = get_saved_poesessid()

        return {
            "has_api_key": api_key is not None,
            "api_key_masked": mask_api_key(api_key) if api_key else "",
            "model": settings.get("model", DEFAULT_MODEL),
            "model_presets": MODEL_PRESETS,
            "mcp_enabled": settings.get("mcp_enabled", True),
            "mcp_servers": settings.get("mcp_servers", get_default_servers()),
            "enabled_tools": settings.get("enabled_tools"),
            "gemini_ready": self.engine.gemini_ready if self.engine else False,
            "mcp_connected": self.engine.mcp_connected if self.engine else False,
            "tool_count": self.engine.active_tool_count if self.engine else 0,
            "total_tool_count": self.engine.total_tool_count if self.engine else 0,
            "server_statuses": self.engine.server_statuses if self.engine else {},
            "all_tool_names": self.engine.all_tool_names if self.engine else [],
            "active_conv_id": self._active_conv_id,
            "data_dir": str(DATA_DIR),
            "trade_auth": {
                "connected": poesessid is not None,
                "masked": mask_poesessid(poesessid) if poesessid else "",
            },
        }

    # ── API Key ────────────────────────────────────────────────

    def validate_and_save_key(self, api_key: str) -> dict:
        """Validate API key, save to keyring if valid. Returns {valid, error, masked}."""
        result = validate_api_key(api_key)
        if result["valid"]:
            save_api_key_secure(api_key)
            return {"valid": True, "error": None, "masked": mask_api_key(api_key)}
        return {"valid": False, "error": result["error"], "masked": ""}

    def remove_key(self) -> dict:
        """Remove saved API key and reset engine."""
        remove_saved_api_key()
        if self.engine and self._loop:
            future = asyncio.run_coroutine_threadsafe(self.engine.close(), self._loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass
            self.engine = None
        return {"removed": True}

    # ── Gemini ─────────────────────────────────────────────────

    def init_gemini(self, model: str = "") -> dict:
        """Initialize or re-initialize Gemini with saved API key."""
        self._ensure_loop()
        api_key = get_saved_api_key()
        if not api_key:
            return {"success": False, "error": "No API key saved"}

        if not model:
            model = load_settings().get("model", DEFAULT_MODEL)

        if self.engine and self.engine.gemini_ready and self.engine.model == model:
            return {"success": True, "model": model}

        try:
            if not self.engine:
                self.engine = ChatEngine()

            future = asyncio.run_coroutine_threadsafe(
                self.engine.init_gemini(api_key, model), self._loop
            )
            future.result(timeout=30)
            return {"success": True, "model": model}
        except Exception as e:
            logger.error(f"Gemini init failed: {e}")
            return {"success": False, "error": str(e)}

    def change_model(self, model: str) -> dict:
        """Change the Gemini model."""
        self._ensure_loop()
        if not self.engine or not self.engine.gemini_ready:
            return self.init_gemini(model)

        try:
            future = asyncio.run_coroutine_threadsafe(
                self.engine.change_model(model), self._loop
            )
            future.result(timeout=30)
            # Save model preference
            settings = load_settings()
            settings["model"] = model
            save_settings(settings)
            return {"success": True, "model": model}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── MCP ────────────────────────────────────────────────────

    def start_mcp(self, servers_json: str = "") -> dict:
        """Start MCP servers. Auto-detects if no servers provided."""
        self._ensure_loop()
        if not self.engine or not self.engine.gemini_ready:
            return {"success": False, "error": "Initialize Gemini first"}

        try:
            servers = json.loads(servers_json) if servers_json else load_settings().get("mcp_servers", get_default_servers())
        except json.JSONDecodeError as e:
            return {"success": False, "error": f"Invalid server config: {e}"}

        # Inject POESESSID from keyring into MCP server environment
        poesessid = get_saved_poesessid()
        env_extra = {"POESESSID": poesessid} if poesessid else None

        try:
            future = asyncio.run_coroutine_threadsafe(
                self.engine.start_mcp(servers, env_extra=env_extra), self._loop
            )
            future.result(timeout=120)

            # Save settings
            settings = load_settings()
            settings["mcp_enabled"] = True
            settings["mcp_servers"] = servers
            save_settings(settings)

            return {
                "success": True,
                "tool_count": self.engine.active_tool_count,
                "total_tool_count": self.engine.total_tool_count,
                "servers": [
                    {"id": sid, "name": st.get("name", sid),
                     "connected": st.get("connected", False),
                     "tool_count": st.get("tool_count", 0),
                     "error": st.get("error")}
                    for sid, st in self.engine.server_statuses.items()
                ],
            }
        except Exception as e:
            logger.error(f"MCP start failed: {e}")
            return {"success": False, "error": str(e)}

    def stop_mcp(self) -> dict:
        """Stop MCP servers."""
        self._ensure_loop()
        if not self.engine:
            return {"success": True}

        try:
            future = asyncio.run_coroutine_threadsafe(self.engine.stop_mcp(), self._loop)
            future.result(timeout=10)

            settings = load_settings()
            settings["mcp_enabled"] = False
            save_settings(settings)

            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def set_mcp_enabled(self, enabled: bool) -> dict:
        """Toggle MCP on/off."""
        if enabled:
            return self.start_mcp()
        else:
            return self.stop_mcp()

    # ── Tool Permissions ───────────────────────────────────────

    def set_enabled_tools(self, tools_json: str) -> dict:
        """Set which tools are enabled. JSON array of tool names, or null for all."""
        if not self.engine:
            return {"success": False, "error": "Engine not initialized"}

        try:
            tools = json.loads(tools_json) if tools_json else None
            tool_set = set(tools) if tools else None
        except json.JSONDecodeError:
            return {"success": False, "error": "Invalid tool list"}

        self.engine.set_enabled_tools(tool_set)

        # Save preference
        settings = load_settings()
        settings["enabled_tools"] = tools
        save_settings(settings)

        return {
            "success": True,
            "active_count": self.engine.active_tool_count,
            "total_count": self.engine.total_tool_count,
        }

    # ── Trade API Auth ───────────────────────────────────────────

    def get_trade_auth_status(self) -> dict:
        """Get current trade auth status."""
        poesessid = get_saved_poesessid()
        pw_available = False
        try:
            import playwright  # noqa: F401
            pw_available = True
        except ImportError:
            pass
        return {
            "connected": poesessid is not None,
            "masked": mask_poesessid(poesessid) if poesessid else "",
            "playwright_available": pw_available,
        }

    def start_trade_auth_browser(self) -> dict:
        """Launch Playwright browser to capture POESESSID. Non-blocking."""
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            return {
                "success": False,
                "error": "Playwright not installed",
                "install_cmd": "pip install playwright && playwright install chromium",
            }

        self._ensure_loop()
        self._pw_cancel = False

        async def _do_auth():
            try:
                result = await self._run_playwright_auth()
                if self.window:
                    try:
                        self.window.evaluate_js(
                            f'window._tradeAuthResult && window._tradeAuthResult({json.dumps(result)})'
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.error(f"Trade auth error: {e}")
                if self.window:
                    try:
                        self.window.evaluate_js(
                            f'window._tradeAuthResult && window._tradeAuthResult({json.dumps({"success": False, "error": str(e)})})'
                        )
                    except Exception:
                        pass

        asyncio.run_coroutine_threadsafe(_do_auth(), self._loop)
        return {"started": True}

    async def _run_playwright_auth(self) -> dict:
        """Run the Playwright browser auth flow (async)."""
        from playwright.async_api import async_playwright

        session_cookie = None
        max_wait = 300
        check_interval = 2
        browser = None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context()
                page = await context.new_page()

                await page.goto(
                    "https://www.pathofexile.com/trade2/search/poe2/Standard",
                    wait_until="domcontentloaded", timeout=30000,
                )

                for i in range(0, max_wait, check_interval):
                    if self._pw_cancel:
                        await browser.close()
                        return {"success": False, "error": "Cancelled by user"}

                    await asyncio.sleep(check_interval)
                    cookies = await context.cookies()

                    for cookie in cookies:
                        if cookie["name"] == "POESESSID":
                            session_cookie = cookie["value"]
                            break

                    if session_cookie:
                        break

                await browser.close()
                browser = None

            if not session_cookie:
                return {"success": False, "error": "Login timed out after 5 minutes. Please try again."}

            save_poesessid(session_cookie)
            logger.info(f"Trade auth: POESESSID captured (len={len(session_cookie)})")
            self._restart_mcp_with_poesessid()

            return {"success": True, "masked": mask_poesessid(session_cookie)}

        except Exception as e:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            return {"success": False, "error": str(e)}

    def cancel_trade_auth(self) -> dict:
        """Cancel an in-progress Playwright auth."""
        self._pw_cancel = True
        return {"success": True}

    def save_trade_auth_manual(self, poesessid: str) -> dict:
        """Save a manually entered POESESSID."""
        sid = poesessid.strip()
        if not sid or len(sid) < 20:
            return {"success": False, "error": "Invalid session ID. Must be at least 20 characters."}

        try:
            int(sid, 16)
        except ValueError:
            return {"success": False, "error": "Invalid session ID. Should be a hex string from browser cookies."}

        try:
            save_poesessid(sid)
            self._restart_mcp_with_poesessid()
            return {"success": True, "masked": mask_poesessid(sid)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def remove_trade_auth(self) -> dict:
        """Remove saved POESESSID and restart MCP."""
        try:
            remove_poesessid()
            self._restart_mcp_with_poesessid()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _restart_mcp_with_poesessid(self):
        """Restart MCP servers with current POESESSID from keyring."""
        if not self.engine or not self.engine.mcp_connected:
            return
        self._ensure_loop()
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.engine.stop_mcp(), self._loop
            )
            future.result(timeout=10)
        except Exception:
            pass

        settings = load_settings()
        servers = settings.get("mcp_servers", get_default_servers())
        poesessid = get_saved_poesessid()
        env_extra = {"POESESSID": poesessid} if poesessid else None

        try:
            future = asyncio.run_coroutine_threadsafe(
                self.engine.start_mcp(servers, env_extra=env_extra), self._loop
            )
            future.result(timeout=120)
            logger.info("MCP restarted with updated trade auth")
        except Exception as e:
            logger.error(f"MCP restart after trade auth failed: {e}")

    # ── Chat (with history save) ───────────────────────────────

    def send_message(self, message: str) -> dict:
        """Send a chat message and save to history."""
        if not self.engine or not self.engine.gemini_ready:
            return {"type": "error", "content": "AI is not ready. Please set up your API key first."}
        try:
            self._ensure_loop()

            # Save user message to history (before network request)
            if self._active_conv_id:
                self._save_user_message(self._active_conv_id, message)

            # Send to Gemini
            future = asyncio.run_coroutine_threadsafe(
                self.engine.send_message(message), self._loop
            )
            result = future.result(timeout=300)

            # Save assistant response to history
            if self._active_conv_id:
                self._save_assistant_response(self._active_conv_id, result)

            return result
        except Exception as e:
            logger.error(f"Message error: {e}")
            return {"type": "error", "content": str(e)}

    def _save_user_message(self, conv_id: str, message: str):
        """Save user message and update conversation metadata."""
        try:
            msg = self.history.save_message(conv_id, "user", message)
            # Auto-title on first message
            conv = self.history.get_conversation(conv_id)
            if conv and conv.get("title") == "New chat":
                title = _auto_title(message)
                self.history.update_conversation(conv_id, title=title)
            self.history.update_conversation(conv_id,
                updated_at=self._now_iso(),
                last_message_at=self._now_iso(),
                last_message_preview=message[:100])
        except Exception as e:
            logger.error(f"Failed to save user message to history: {e}")

    def _save_assistant_response(self, conv_id: str, result: dict):
        """Save assistant response, tool calls, and update metadata."""
        try:
            content = result.get("content", "")
            status = "completed" if result.get("type") != "error" else "failed"
            error_msg = result.get("content") if result.get("type") == "error" else None
            model_id = self.engine.model if self.engine else None

            asst_msg = self.history.save_message(conv_id, "assistant", content,
                model_id=model_id, status=status, error_message=error_msg)

            # Save tool calls
            for tc in result.get("tool_calls", []):
                self.history.save_tool_call(asst_msg["id"], tc["name"], "completed",
                    friendly_label=friendly_tool_name(tc["name"]),
                    args_json=json.dumps(tc.get("args", {})))

            # Update conversation
            msg_count = self.history.get_message_count(conv_id)
            self.history.update_conversation(conv_id,
                updated_at=self._now_iso(),
                last_message_at=self._now_iso(),
                last_message_preview=content[:100],
                message_count=msg_count,
                model_id=model_id)
        except Exception as e:
            logger.error(f"Failed to save assistant response to history: {e}")

    @staticmethod
    def _now_iso() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    # ── History API ────────────────────────────────────────────

    def history_list(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """List conversations for sidebar."""
        try:
            return self.history.list_conversations(limit, offset)
        except Exception as e:
            logger.error(f"History list failed: {e}")
            return []

    def history_get(self, conv_id: str) -> dict:
        """Get conversation with all messages."""
        try:
            conv = self.history.get_conversation(conv_id)
            if not conv:
                return {"success": False, "error": "Conversation not found"}
            messages = self.history.get_messages(conv_id, limit=500)
            # Attach tool calls to each message
            for msg in messages:
                if msg.get("role") == "assistant":
                    msg["tool_calls"] = self.history.get_tool_calls(msg["id"])
            return {"success": True, "conversation": conv, "messages": messages}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def history_create(self) -> dict:
        """Create a new conversation and set it as active."""
        try:
            model_id = self.engine.model if self.engine else DEFAULT_MODEL
            conv = self.history.create_conversation("New chat", model_id)
            self._active_conv_id = conv["id"]
            return {"success": True, "conversation": conv}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def history_set_active(self, conv_id: str) -> dict:
        """Set the active conversation (for loading a saved chat)."""
        self._active_conv_id = conv_id
        return {"success": True}

    def history_rename(self, conv_id: str, title: str) -> dict:
        """Rename a conversation."""
        try:
            self.history.update_conversation(conv_id, title=title.strip() or "Untitled")
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def history_pin(self, conv_id: str, pinned: bool) -> dict:
        """Pin or unpin a conversation."""
        try:
            from datetime import datetime, timezone
            self.history.update_conversation(conv_id,
                is_pinned=1 if pinned else 0,
                pinned_at=datetime.now(timezone.utc).isoformat() if pinned else None)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def history_archive(self, conv_id: str, archived: bool) -> dict:
        """Archive or unarchive a conversation."""
        try:
            from datetime import datetime, timezone
            self.history.update_conversation(conv_id,
                is_archived=1 if archived else 0,
                archived_at=datetime.now(timezone.utc).isoformat() if archived else None)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def history_delete(self, conv_id: str) -> dict:
        """Soft-delete a conversation."""
        try:
            self.history.delete_conversation(conv_id)
            if self._active_conv_id == conv_id:
                self._active_conv_id = None
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def history_search(self, query: str) -> list[dict]:
        """Search conversations."""
        try:
            return self.history.search(query)
        except Exception as e:
            logger.error(f"History search failed: {e}")
            return []

    def history_export(self, conv_id: str, fmt: str = "markdown") -> dict:
        """Export a conversation."""
        try:
            content = self.history.export_conversation(conv_id, fmt)
            # Save to exports dir
            conv = self.history.get_conversation(conv_id)
            safe_title = re.sub(r'[^\w\s-]', '', conv.get("title", "export"))[:40]
            ext = "md" if fmt == "markdown" else "json"
            path = EXPORT_DIR / f"{safe_title}.{ext}"
            path.write_text(content, encoding="utf-8")
            return {"success": True, "path": str(path)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def history_backup(self) -> dict:
        """Create a database backup."""
        try:
            path = self.history.create_backup(BACKUP_DIR)
            return {"success": True, "path": str(path)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def history_stats(self) -> dict:
        """Get history statistics."""
        try:
            return self.history.get_stats()
        except Exception as e:
            return {"conversation_count": 0, "message_count": 0, "db_size_bytes": 0, "error": str(e)}

    def history_clear_all(self) -> dict:
        """Clear all chat history."""
        try:
            self.history.clear_all()
            self._active_conv_id = None
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def history_open_folder(self) -> dict:
        """Open the data folder in file explorer."""
        try:
            os.startfile(str(DATA_DIR))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ── Auto-connect ───────────────────────────────────────────

    def auto_connect(self) -> dict:
        """Auto-connect on app launch: init Gemini + start MCP if previously enabled."""
        # Recover interrupted messages from previous session
        try:
            self.history.recover_interrupted()
        except Exception as e:
            logger.warning(f"History recovery failed: {e}")

        api_key = get_saved_api_key()
        if not api_key:
            return {"gemini": False, "mcp": False, "reason": "No API key"}

        settings = load_settings()
        model = settings.get("model", DEFAULT_MODEL)
        mcp_enabled = settings.get("mcp_enabled", True)

        # Init Gemini
        gemini_result = self.init_gemini(model)
        if not gemini_result.get("success"):
            return {"gemini": False, "mcp": False, "error": gemini_result.get("error")}

        result = {"gemini": True, "model": model, "mcp": False}

        # Start MCP if enabled
        if mcp_enabled:
            servers = settings.get("mcp_servers", get_default_servers())
            mcp_result = self.start_mcp(json.dumps(servers))
            result["mcp"] = mcp_result.get("success", False)
            result["mcp_servers"] = mcp_result.get("servers", [])
            result["tool_count"] = mcp_result.get("tool_count", 0)
            result["total_tool_count"] = mcp_result.get("total_tool_count", 0)

        # Apply tool filter if set
        enabled_tools = settings.get("enabled_tools")
        if enabled_tools and self.engine:
            self.engine.set_enabled_tools(set(enabled_tools))
            result["active_tool_count"] = self.engine.active_tool_count

        return result


# ── Main ───────────────────────────────────────────────────────

def main():
    logger.info("Starting ChatPoE...")
    logger.info(f"Data directory: {DATA_DIR}")

    api = Api()
    ui_path = THIS_DIR / "ui" / "index.html"

    window = webview.create_window(
        title="ChatPoE",
        url=str(ui_path),
        js_api=api,
        width=960,
        height=720,
        min_size=(640, 520),
        resizable=True,
        text_select=True,
    )
    api.window = window

    debug = "--debug" in sys.argv
    webview.start(debug=debug)

    # Cleanup
    if api.engine and api._loop:
        try:
            future = asyncio.run_coroutine_threadsafe(api.engine.close(), api._loop)
            future.result(timeout=10)
        except Exception:
            pass
        api._loop.call_soon_threadsafe(api._loop.stop)

    api.history.close()
    logger.info("Goodbye!")


if __name__ == "__main__":
    main()
