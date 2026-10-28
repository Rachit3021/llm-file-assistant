"""
llm_file_assistant.py
=====================

An interactive assistant that lets an LLM call the tools in fs_tools.py.
Uses Groq's free API (OpenAI-compatible) with open models such as GPT-OSS / Llama.

How tool calling works here
---------------------------
1. We send the conversation plus JSON-schema descriptions of each tool.
2. The model replies either with a normal answer (done) or with `tool_calls`.
3. We execute each requested tool locally and send the results back as
   messages with role "tool".
4. Repeat until the model answers without requesting any tools.

Safety: every path the model supplies is resolved inside WORKSPACE_DIR, so the
model cannot read or write files outside the project folder.

Usage
-----
    python llm_file_assistant.py                       # interactive chat
    python llm_file_assistant.py -q "List all resumes"  # single query
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

import openai
from openai import OpenAI

from fs_tools import list_files, read_file, search_in_file, write_file

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
BASE_URL = "https://api.groq.com/openai/v1"
WORKSPACE = Path(os.getenv("WORKSPACE_DIR", ".")).resolve()
MAX_TOOL_RESULT_CHARS = 8_000  # free tiers have small token limits
MAX_STEPS = 15                 # safety cap on tool-call rounds per user query

SYSTEM_PROMPT = f"""You are a file assistant that helps recruiters work with resume files.
You can only access files through the provided tools. The workspace root is: {WORKSPACE}
Use paths relative to the workspace (e.g. "resumes/resume_john_doe.pdf").

Rules:
- Resumes can be .pdf, .docx or .txt files. When the user says "all resumes",
  call list_files WITHOUT an extension filter so every format is included.
- Never guess filenames. Call list_files first if you are unsure what exists.
- Only describe a file's contents after you have read or searched it with a tool
  in this conversation. Never answer about a file from memory.
- To read all resumes, call read_file once for EVERY file returned by list_files.
- To find resumes mentioning a skill, call search_in_file on EVERY resume.
- When asked to create a summary, read the resume first, then save a concise
  Markdown summary with write_file. Default location: summaries/<name>_summary.md
- If a tool returns an error, explain it plainly and try a sensible alternative.
- Finish with a short answer saying what you found or which files you created.
"""

# --------------------------------------------------------------------------- #
# Tool schemas (OpenAI function-calling format)
# Optional parameters accept null, because some models send null for "not set".
# --------------------------------------------------------------------------- #
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a resume or document and return its extracted text plus metadata "
                "(size, modified date, word count, page count). Supports .pdf, .docx, .txt, .md."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the file, e.g. 'resumes/resume_john_doe.pdf'",
                    }
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files in a directory with name, size and modified date. "
                "Optionally filter by extension such as '.pdf' or '.docx'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "Directory path, e.g. 'resumes'"},
                    "extension": {
                        "type": ["string", "null"],
                        "description": "Optional extension filter, e.g. '.pdf'. Omit to list all files.",
                    },
                },
                "required": ["directory"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write plain text (e.g. Markdown) to a file. Creates parent folders if needed. "
                "Overwrites by default; set append=true to append. Cannot write .pdf or .docx."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Destination, e.g. 'summaries/x.md'"},
                    "content": {"type": "string", "description": "Text content to write"},
                    "append": {"type": ["boolean", "null"], "description": "Append instead of overwrite"},
                },
                "required": ["filepath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_file",
            "description": (
                "Case-insensitive search for a keyword or phrase inside one file. "
                "Returns the match count and each match with surrounding context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "File to search"},
                    "keyword": {"type": "string", "description": "Word or phrase, e.g. 'Python'"},
                },
                "required": ["filepath", "keyword"],
            },
        },
    },
]

TOOL_REGISTRY = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "search_in_file": search_in_file,
}

PATH_ARGS = ("filepath", "directory")


# --------------------------------------------------------------------------- #
# Tool execution
# --------------------------------------------------------------------------- #
def resolve_in_workspace(user_path: str) -> str:
    """Resolve a model-supplied path and make sure it stays inside WORKSPACE."""
    candidate = Path(user_path).expanduser()
    if not candidate.is_absolute():
        candidate = WORKSPACE / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(WORKSPACE):
        raise PermissionError(f"Access outside the workspace is not allowed: {user_path}")
    return str(candidate)


def _relativise(obj):
    """Show workspace-relative paths to the model (shorter and clearer)."""
    if isinstance(obj, dict):
        return {k: _relativise(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_relativise(v) for v in obj]
    if isinstance(obj, str) and obj.startswith(str(WORKSPACE)):
        return os.path.relpath(obj, WORKSPACE)
    return obj


def execute_tool(name: str, args: dict) -> tuple[str, bool]:
    """
    Run a tool and return (json_string_result, is_error).
    Never raises, so every failure becomes information the model can act on.
    """
    func = TOOL_REGISTRY.get(name)
    if func is None:
        return json.dumps({"success": False, "error": f"Unknown tool: {name}"}), True

    try:
        # Drop null values so the tool's own defaults are used
        args = {k: v for k, v in dict(args).items() if v is not None}
        for key in PATH_ARGS:
            if key in args:
                args[key] = resolve_in_workspace(args[key])
        result = func(**args)
    except PermissionError as exc:
        result = {"success": False, "error": str(exc)}
    except TypeError as exc:  # wrong or missing arguments from the model
        result = {"success": False, "error": f"Invalid arguments for {name}: {exc}"}
    except Exception as exc:
        result = {"success": False, "error": f"{type(exc).__name__}: {exc}"}

    is_error = (isinstance(result, dict) and result.get("success") is False) or (
        isinstance(result, list) and len(result) == 1 and result[0].get("success") is False
    )

    result = _relativise(result)
    text = json.dumps(result, ensure_ascii=False, default=str)
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + "... [TRUNCATED: result too long]"
    return text, is_error


# --------------------------------------------------------------------------- #
# Assistant
# --------------------------------------------------------------------------- #
class FileAssistant:
    """Keeps conversation history and runs the tool-calling loop."""

    def __init__(self, model: str = MODEL, verbose: bool = True):
        # max_retries makes the SDK wait and retry automatically on rate limits
        self.client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url=BASE_URL, max_retries=5)
        self.model = model
        self.verbose = verbose
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    def reset(self) -> None:
        """Clear history but keep the system prompt."""
        self.messages = self.messages[:1]

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def ask(self, query: str) -> str:
        """Send a user query; return the model's final text answer."""
        self.messages.append({"role": "user", "content": query})

        for step in range(1, MAX_STEPS + 1):
            response = self.client.chat.completions.create(
                model=self.model,
                messages=self.messages,
                tools=TOOL_SCHEMAS,
                tool_choice="auto",
                temperature=0.2,
            )
            msg = response.choices[0].message

            # No tool requested -> this is the final answer
            if not msg.tool_calls:
                answer = (msg.content or "").strip()
                self.messages.append({"role": "assistant", "content": answer})
                return answer

            if msg.content and msg.content.strip():
                self._log(f"\n💭 {msg.content.strip()}")

            # Record the assistant's tool request in history
            self.messages.append(
                {
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )

            # Execute every requested tool and send each result back
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}") or {}
                except json.JSONDecodeError:
                    output = json.dumps({"success": False, "error": "Arguments were not valid JSON"})
                    is_error = True
                else:
                    self._log(f"🔧 [step {step}] {name}({json.dumps(args)[:150]})")
                    output, is_error = execute_tool(name, args)
                self._log(f"   {'❌' if is_error else '✅'} {output[:160]}{'...' if len(output) > 160 else ''}")
                self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})

        note = f"(Stopped after {MAX_STEPS} tool steps. Try a narrower request.)"
        self.messages.append({"role": "assistant", "content": note})
        return note


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
EXAMPLES = [
    "Read all resumes in the resumes folder",
    "Find resumes mentioning Python experience",
    "Create a summary file for resume_john_doe.pdf",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="LLM File Assistant (Groq tool calling)")
    parser.add_argument("-q", "--query", help="Run a single query and exit")
    parser.add_argument("--quiet", action="store_true", help="Hide tool-call logs")
    args = parser.parse_args()

    if not os.getenv("GROQ_API_KEY"):
        sys.exit("GROQ_API_KEY is not set. Create a .env file with your Groq key.")

    assistant = FileAssistant(verbose=not args.quiet)

    if args.query:
        print(f"\n🤖 {assistant.ask(args.query)}")
        return

    print(f"LLM File Assistant | model: {MODEL} | workspace: {WORKSPACE}")
    print("Try:\n  " + "\n  ".join(EXAMPLES))
    print("Type 'exit' to quit, 'reset' to clear history.\n")

    while True:
        try:
            query = input("🧑 You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not query:
            continue
        if query.lower() in {"exit", "quit"}:
            break
        if query.lower() == "reset":
            assistant.reset()
            print("History cleared.\n")
            continue
        try:
            print(f"\n🤖 {assistant.ask(query)}\n")
        except openai.APIError as exc:
            print(f"API error: {exc}\nTip: type 'reset' and try again.\n")


if __name__ == "__main__":
    main()