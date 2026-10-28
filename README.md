# LLM File Assistant: Tool Calling for Resume Files

A Python project that gives an LLM (Anthropic Claude) a set of file-system tools
so it can read, list, search and write resume files from natural-language requests.

**Demo video:** (link coming soon)

## Features
- **read_file**: extracts text from PDF, DOCX, TXT and MD files, with metadata
- **list_files**: lists a directory with size and modified date, with optional extension filter
- **write_file**: writes text files and creates folders automatically
- **search_in_file**: case-insensitive keyword search that returns surrounding context
- **LLM integration**: Claude chooses and chains tools on its own, including parallel calls
- **Safety**: all paths are sandboxed to the workspace folder, and errors come back as structured data the model can recover from

## Setup
```bash
git clone <your-repo-url> && cd llm-file-assistant
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then add your ANTHROPIC_API_KEY
python generate_sample_resumes.py # creates 6 resumes in ./resumes
```
Requires Python 3.9+.

## Usage
Interactive mode:
```bash
python llm_file_assistant.py
```
Single query:
```bash
python llm_file_assistant.py -q "Find resumes mentioning Python experience"
```
Using the tools directly, without an LLM:
```python
from fs_tools import read_file, list_files, search_in_file, write_file
list_files("resumes", ".pdf")
search_in_file("resumes/resume_john_doe.pdf", "python")
```
Run the tests:
```bash
pytest -v
```

## Example session
```
🧑 You: Find resumes mentioning Python experience
🔧 [step 1] list_files({"directory": "resumes"})
   ✅ [{"name": "resume_alex_chen.docx", ...
🔧 [step 2] search_in_file({"filepath": "resumes/resume_alex_chen.docx", "keyword": "Python"})
🔧 [step 2] search_in_file({"filepath": "resumes/resume_emily_watson.txt", "keyword": "Python"})
   ... (one call per resume, executed in the same round)
🤖 Three resumes mention Python: John Doe (6 yrs, FastAPI/Django), Alex Chen
   (pandas/matplotlib for analytics), and Rahul Verma (Python scripting for DevOps).
```

## How tool calling works
1. The user query and the four tool JSON schemas are sent to Claude.
2. Claude returns `tool_use` blocks naming a tool and its arguments.
3. `execute_tool()` validates the paths, runs the Python function, and returns JSON.
4. The results go back as `tool_result` blocks, and the loop repeats until Claude
   answers in plain text.

## Project structure
| File | Purpose |
|------|---------|
| `fs_tools.py` | Part A: the four tools |
| `llm_file_assistant.py` | Part B: tool schemas, executor, agent loop, CLI |
| `generate_sample_resumes.py` | Creates the sample data |
| `test_fs_tools.py` | Unit tests |

## Design decisions
- Tools return errors instead of raising, so the LLM can read the error and adapt.
- Multi-word searches match across line breaks, which matters for PDF text extraction.
- Large tool outputs are truncated to protect the context window.
- `write_file` refuses `.pdf` and `.docx` because writing plain text into them would corrupt them.

## Limitations
- Scanned or image-only PDFs return no text, since there is no OCR.
- Legacy `.doc` files are not supported.