# ChatPoE — Desktop Chat App

Minimal Windows desktop app that combines **Gemini AI** (free tier) with the **poe2-mcp** server for AI-powered Path of Exile 2 build optimization advice.

## Features

- 🖥️ Native Windows desktop window (pywebview)
- 🤖 Gemini 2.5 Flash AI (free tier — 1,500 requests/day)
- 🔧 39 MCP tools for PoE2 data (character analysis, gems, mods, passive tree, etc.)
- 🔩 Tool calls visible in chat (expandable)
- 🎨 Dark PoE2-themed UI
- 📦 Package as single .exe

## Quick Start

### 1. Prerequisites

- **Python 3.9+** installed
- **poe2-mcp** installed: `pip install poe2-mcp`
- **Gemini API key** (free): Get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)

### 2. Install & Run

```bash
cd poe2-chat
pip install -r requirements.txt
python main.py
```

### 3. Usage

1. Click **⚙️ Settings** → paste your Gemini API key
2. Click **Connect** (waits ~10-30 seconds for MCP server to load game data)
3. Start chatting! Example questions:
   - "List all support gems"
   - "Explain how armor works in PoE2"
   - "Search for fire resistance mods"
   - "What keystones are available for life builds?"
   - "Analyze my character CharName from account AccountName"

### Debug Mode

```bash
python main.py --debug
```

Opens Chrome DevTools for troubleshooting.

## Build EXE

```bash
pip install pyinstaller
build.bat
```

Output: `dist/poe2-chat.exe`

> **Note:** Users still need `poe2-mcp` installed (`pip install poe2-mcp`) — the game data is too large (62MB) to bundle.

## Architecture

```
User → UI (HTML/JS) → pywebview API bridge → Python
  → Gemini API (with MCP tools as function declarations)
  → MCP Client (stdio subprocess → poe2-mcp server)
  → Tool calling loop until final answer
```

## Files

| File | Purpose |
|------|---------|
| `main.py` | App entry point, pywebview window, API bridge |
| `chat_engine.py` | Gemini + MCP integration, tool calling loop |
| `ui/index.html` | Chat UI (dark PoE2 theme) |
| `requirements.txt` | Python dependencies |
| `build.bat` | PyInstaller build script |
