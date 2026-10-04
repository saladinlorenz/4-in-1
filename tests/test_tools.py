from __future__ import annotations

import json

from agent.tools import build_tools
from agent.tools.files import resolve_in_sandbox


def tool_map(deps):
    return {item.name: item for item in build_tools(deps)}


def test_build_tools_registers_expected_tools(tool_deps):
    names = set(tool_map(tool_deps))
    assert names == {
        "web_search",
        "web_fetch",
        "write_file",
        "read_file",
        "list_files",
        "remember",
        "search_memory",
        "send_notification",
        "get_status",
        "social_create_draft",
        "social_publish",
        "social_list_drafts",
        "model_generate",
        "workflow_create",
        "workflow_run",
        "workflow_status",
    }


def test_write_read_list_roundtrip(tool_deps):
    tools = tool_map(tool_deps)
    result = tools["write_file"](path="notes/todo.txt", content="acheter du café")
    assert result.startswith("Wrote")
    assert tools["read_file"](path="notes/todo.txt") == "acheter du café"
    listing = tools["list_files"](path=".")
    assert "notes/todo.txt" in listing


def test_sandbox_rejects_traversal_and_absolute_paths(tool_deps):
    tools = tool_map(tool_deps)
    assert "escapes the sandbox" in tools["write_file"](path="../evil.txt", content="x")
    assert "absolute paths" in tools["write_file"](path="/etc/passwd", content="x")
    assert "escapes the sandbox" in tools["read_file"](path="..\\..\\secret")


def test_write_rejects_oversized_content(tool_deps):
    tool_deps.files_max_bytes = 10
    tools = tool_map(tool_deps)
    assert "exceeds 10 bytes" in tools["write_file"](path="big.txt", content="x" * 50)


def test_read_missing_file(tool_deps):
    tools = tool_map(tool_deps)
    assert "does not exist" in tools["read_file"](path="missing.txt")


def test_resolve_in_sandbox_normalizes(tool_deps):
    target = resolve_in_sandbox(tool_deps.files_dir, "./a/b.txt")
    assert str(target).replace("\\", "/").endswith("storage/files/a/b.txt")


def test_web_search_formats_results(tool_deps):
    tools = tool_map(tool_deps)
    output = tools["web_search"](query="agent ios", max_results=3)
    assert "Result for agent ios" in output
    assert "https://example.com/a" in output


def test_web_search_reports_errors(tool_deps):
    def broken(query, count):
        raise RuntimeError("network down sk-abcdefghijkl")

    tool_deps.search_fn = broken
    tools = tool_map(tool_deps)
    output = tools["web_search"](query="x")
    assert output.startswith("Search failed:")
    assert "sk-abcdefghijkl" not in output


def test_web_fetch_truncates(tool_deps):
    tool_deps.web_fetch_max_chars = 10
    tools = tool_map(tool_deps)
    output = tools["web_fetch"](url="https://example.com/page")
    assert output.endswith("...[truncated]")


def test_web_fetch_reports_errors(tool_deps):
    def broken(url):
        raise RuntimeError("boom")

    tool_deps.fetch_fn = broken
    tools = tool_map(tool_deps)
    assert tools["web_fetch"](url="https://example.com").startswith("Fetch failed:")


def test_html_to_text_strips_scripts():
    from agent.tools.web_fetch import html_to_text

    html = "<html><script>alert(1)</script><style>b{}</style><body><p>Hello</p><p>World</p></body></html>"
    text = html_to_text(html)
    assert "Hello" in text
    assert "World" in text
    assert "alert" not in text


def test_memory_tools(tool_deps):
    tools = tool_map(tool_deps)
    assert tools["remember"](text="L'utilisateur préfère le français").startswith("Saved")
    found = tools["search_memory"](query="français")
    assert "français" in found
    assert tools["search_memory"](query="inconnu-xyz") == "No memories matched."


def test_notify_and_status_tools(tool_deps, notifier):
    tools = tool_map(tool_deps)
    assert tools["send_notification"](message="tout va bien") == "Notification sent."
    assert notifier.items == ["tout va bien"]

    status = json.loads(tools["get_status"]())
    assert status["app"] == "agentos"
    assert "time" in status


def test_model_generate_tool(tool_deps):
    tool_deps.generate_fn = lambda prompt: f"echo:{prompt}"
    tools = tool_map(tool_deps)
    assert tools["model_generate"](prompt="hi") == "echo:hi"
    assert tools["model_generate"](prompt="").startswith("Generation failed")
    tool_deps.generate_fn = None
    assert tools["model_generate"](prompt="hi").startswith("Generation unavailable")


def test_model_generate_reports_and_redacts_errors(tool_deps):
    def broken(prompt):
        raise RuntimeError("quota reached key=sk-abcdefghijkl")

    tool_deps.generate_fn = broken
    tools = tool_map(tool_deps)
    output = tools["model_generate"](prompt="hi")
    assert output.startswith("Generation failed:")
    assert "sk-abcdefghijkl" not in output


def test_workflow_create_validates_inputs(tool_deps):
    tools = tool_map(tool_deps)
    assert tools["workflow_create"](name="wf", steps="a").startswith("Workflow unavailable")
    assert "not created" in tools["workflow_create"](name="", steps="a")
    assert "name must be" in tools["workflow_create"](name="bad name!", steps="a")
    assert "1 to 20" in tools["workflow_create"](name="wf", steps="")
    assert "empty step" in tools["workflow_create"](name="wf", steps="a\n\nb")
    too_many = "\n".join(str(i) for i in range(21))
    assert "1 to 20" in tools["workflow_create"](name="wf", steps=too_many)


def test_workflow_create_run_status_roundtrip(tool_deps):
    calls: dict = {}

    def create(name, steps):
        calls["create"] = (name, steps)
        return 7

    def run(name, key):
        calls["run"] = (name, key)
        return 42

    def runs(name, limit):
        calls["runs"] = (name, limit)
        return [
            {
                "id": 42,
                "status": "RUNNING",
                "workflow_name": "wf",
                "current_step": 1,
                "created_at": "2026-10-04",
                "error": None,
            }
        ]

    tool_deps.workflow_create_fn = create
    tool_deps.workflow_run_fn = run
    tool_deps.workflow_runs_fn = runs
    tools = tool_map(tool_deps)

    created = tools["workflow_create"](name="wf", steps="one\ntwo")
    assert created == "Workflow 'wf' saved as #7 with 2 steps."
    assert [step["prompt"] for step in calls["create"][1]] == ["one", "two"]
    assert [step["kind"] for step in calls["create"][1]] == ["agent", "agent"]

    started = tools["workflow_run"](name="wf", idempotency_key="daily:2026-10-04")
    assert started == "Run #42 of 'wf' started (joined existing active run)."
    assert calls["run"] == ("wf", "daily:2026-10-04")

    status = tools["workflow_status"]()
    assert "run #42 [RUNNING] wf step=1" in status
    assert calls["runs"] == ("", 5)


def test_workflow_run_not_found_and_status_empty(tool_deps):
    tools = tool_map(tool_deps)
    tool_deps.workflow_run_fn = lambda name, key: None
    assert "not found" in tools["workflow_run"](name="missing")
    tool_deps.workflow_runs_fn = lambda name, limit: []
    assert tools["workflow_status"]() == "No workflow runs found."
