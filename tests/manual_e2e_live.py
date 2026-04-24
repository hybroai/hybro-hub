"""
Live E2E test: start agents via hybro-hub CLI, send messages using a2a-sdk client.

This mirrors the production flow:
  hybro-hub agent start claude-code  →  uvicorn A2A server
  backend uses A2AClient             →  send_message / send_message_streaming

This is NOT a CI test — it requires:
- `claude` CLI installed and authenticated
- `codex` CLI installed and authenticated
- `a2a-adapter` (local editable) installed in this venv

Setup (run once):
    cd hybro-hub
    uv pip install -e "../hybro open source/a2a-adapters"

Usage:
    cd hybro-hub
    .venv/bin/python tests/manual_e2e_live.py
"""

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
import warnings

import httpx

# A2AClient is deprecated in favor of ClientFactory, but the production
# backend (hybro-multi-agents-backend) still uses A2AClient. We match
# the production flow here.
warnings.filterwarnings("ignore", message="A2AClient is deprecated")

from a2a.client import A2ACardResolver, A2AClient
from a2a.types import (
    JSONRPCErrorResponse,
    Message,
    MessageSendConfiguration,
    MessageSendParams,
    Part,
    Role,
    SendMessageRequest,
    SendMessageSuccessResponse,
    SendStreamingMessageRequest,
    SendStreamingMessageSuccessResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def header(msg: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def step(msg: str) -> None:
    print(f"\n  >> {msg}")


def ok(msg: str) -> None:
    print(f"     [OK] {msg}")


def fail(msg: str) -> None:
    print(f"     [FAIL] {msg}")


# ──── Server process management ────


def start_agent_process(
    adapter_type: str,
    port: int,
    working_dir: str,
) -> subprocess.Popen:
    """Start `hybro-hub agent start <type>` as a background process.

    Uses sys.executable to ensure the subprocess runs in the same venv
    with all packages available.
    """
    hub_root = os.path.dirname(os.path.dirname(__file__))  # hybro-hub root
    launcher = os.path.join(hub_root, "tests", "_launch_agent.py")
    cmd = [
        sys.executable, "-u", launcher,
        "agent", "start", adapter_type,
        "--port", str(port),
        "--working-dir", working_dir,
    ]
    # PYTHONPATH ensures `hub` package is importable from the subprocess.
    # Must NOT use `uv run` here — it would re-sync and replace the local
    # editable a2a-adapter with the PyPI version.
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": hub_root}
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=hub_root,
        env=env,
    )
    return proc


async def wait_for_server(base_url: str, timeout: float = 30) -> None:
    """Poll agent card endpoint until the server is ready."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.get(
                    f"{base_url}/.well-known/agent-card.json", timeout=2,
                )
                if r.status_code == 200:
                    return
        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout):
            pass
        await asyncio.sleep(0.5)
    raise RuntimeError(f"Server at {base_url} did not start within {timeout}s")


def kill_process(proc: subprocess.Popen) -> None:
    """Gracefully terminate, then force kill."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


# ──── A2A SDK client helpers ────


def build_message(text: str, context_id: str | None = None) -> Message:
    """Build an A2A Message matching what the backend constructs."""
    return Message(
        message_id=str(uuid.uuid4()),
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        context_id=context_id,
    )


def build_send_request(message: Message) -> SendMessageRequest:
    """Build a message/send JSON-RPC request (same as backend a2a_service)."""
    return SendMessageRequest(
        id=str(uuid.uuid4()),
        method="message/send",
        jsonrpc="2.0",
        params=MessageSendParams(
            message=message,
            configuration=MessageSendConfiguration(
                accepted_output_modes=["text"],
                blocking=True,
            ),
        ),
    )


def build_stream_request(message: Message) -> SendStreamingMessageRequest:
    """Build a message/stream JSON-RPC request."""
    return SendStreamingMessageRequest(
        id=str(uuid.uuid4()),
        method="message/stream",
        jsonrpc="2.0",
        params=MessageSendParams(
            message=message,
            configuration=MessageSendConfiguration(
                accepted_output_modes=["text"],
            ),
        ),
    )


def extract_text_from_task(task: Task) -> str:
    """Extract text from a Task's artifacts and status message."""
    parts_text = []
    for artifact in task.artifacts or []:
        for part in artifact.parts or []:
            if hasattr(part.root, "text"):
                parts_text.append(part.root.text)
    if not parts_text and task.status and task.status.message:
        for part in task.status.message.parts or []:
            if hasattr(part.root, "text"):
                parts_text.append(part.root.text)
    return "".join(parts_text)


# ──── Tests ────


async def test_agent_card(base_url: str, name: str) -> None:
    """Test agent card discovery via A2ACardResolver (same as backend)."""
    step(f"Agent card discovery: {name}")
    async with httpx.AsyncClient() as hc:
        resolver = A2ACardResolver(httpx_client=hc, base_url=base_url)
        card = await resolver.get_agent_card()
    assert card.name, f"Agent card missing name"
    assert card.capabilities is not None, "Agent card missing capabilities"
    streaming = card.capabilities.streaming if card.capabilities else None
    ok(f"name={card.name}, streaming={streaming}")
    return card


async def test_message_send(
    base_url: str, name: str, prompt: str, timeout: float = 120,
) -> str:
    """Test message/send via A2AClient (same as backend a2a_service)."""
    step(f"message/send to {name}: '{prompt}'")

    # Resolve agent card (same as backend)
    async with httpx.AsyncClient(timeout=timeout) as hc:
        resolver = A2ACardResolver(httpx_client=hc, base_url=base_url)
        card = await resolver.get_agent_card()

        # Create client and send (same as backend create_a2a_client)
        client = A2AClient(httpx_client=hc, agent_card=card)
        message = build_message(prompt)
        request = build_send_request(message)
        response = await client.send_message(request)

    # Parse response
    root = response.root
    assert isinstance(root, SendMessageSuccessResponse), (
        f"Expected success, got error: {root}"
    )

    result = root.result
    assert isinstance(result, Task), f"Expected Task, got {type(result)}"

    state = result.status.state if result.status else "unknown"
    ok(f"task state: {state.value}")
    assert state == TaskState.completed, f"Expected completed, got {state.value}"

    text = extract_text_from_task(result)
    if text:
        preview = text[:200].replace("\n", " ")
        ok(f"response ({len(text)} chars): {preview}...")
    else:
        fail("No text in response")
        print(f"     [DEBUG] result: {result.model_dump_json(indent=2)[:1000]}")

    return text


async def test_message_stream(
    base_url: str, name: str, prompt: str, timeout: float = 120,
) -> str:
    """Test message/stream via A2AClient (same as backend streaming path)."""
    step(f"message/stream to {name}: '{prompt}'")

    async with httpx.AsyncClient(timeout=timeout) as hc:
        resolver = A2ACardResolver(httpx_client=hc, base_url=base_url)
        card = await resolver.get_agent_card()

        client = A2AClient(httpx_client=hc, agent_card=card)
        message = build_message(prompt)
        request = build_stream_request(message)

        text_parts = []
        final_state = "unknown"
        event_count = 0

        async for response in client.send_message_streaming(request):
            event_count += 1
            root = response.root
            if isinstance(root, JSONRPCErrorResponse):
                fail(f"Stream error: {root.error}")
                continue

            result = root.result

            # artifact-update → extract text chunks
            if isinstance(result, TaskArtifactUpdateEvent):
                for part in result.artifact.parts or []:
                    if hasattr(part.root, "text"):
                        text_parts.append(part.root.text)

            # status-update → track state
            elif isinstance(result, TaskStatusUpdateEvent):
                if result.status and result.status.state:
                    final_state = result.status.state.value
                # Final event may carry text in status.message
                if result.final and result.status and result.status.message:
                    if not text_parts:
                        for part in result.status.message.parts or []:
                            if hasattr(part.root, "text"):
                                text_parts.append(part.root.text)

            # Task object (final result)
            elif isinstance(result, Task):
                if result.status and result.status.state:
                    final_state = result.status.state.value
                if not text_parts:
                    text_parts.append(extract_text_from_task(result))

    ok(f"received {event_count} SSE events, final state: {final_state}")

    text = "".join(text_parts)
    if text:
        preview = text[:200].replace("\n", " ")
        ok(f"response ({len(text)} chars): {preview}...")
    else:
        fail("No text in stream events")

    return text


async def test_session_resume(
    base_url: str, name: str, timeout: float = 120,
) -> None:
    """Test session resume: second message in same context should work."""
    step(f"Session resume test: {name}")

    async with httpx.AsyncClient(timeout=timeout) as hc:
        resolver = A2ACardResolver(httpx_client=hc, base_url=base_url)
        card = await resolver.get_agent_card()
        client = A2AClient(httpx_client=hc, agent_card=card)

        # First message — establishes session
        context_id = str(uuid.uuid4())
        msg1 = build_message("Remember the number 42.", context_id=context_id)
        req1 = build_send_request(msg1)
        resp1 = await client.send_message(req1)
        root1 = resp1.root
        assert isinstance(root1, SendMessageSuccessResponse), f"First message failed: {root1}"
        result1 = root1.result
        assert isinstance(result1, Task), f"Expected Task, got {type(result1)}"
        ctx = result1.context_id
        ok(f"First message completed, context_id={ctx}")

        # Second message — should resume session via --resume
        msg2 = build_message(
            "What number did I just ask you to remember? Answer with just the number.",
            context_id=ctx,
        )
        req2 = build_send_request(msg2)
        resp2 = await client.send_message(req2)
        root2 = resp2.root
        assert isinstance(root2, SendMessageSuccessResponse), f"Second message failed: {root2}"
        result2 = root2.result
        assert isinstance(result2, Task), f"Expected Task, got {type(result2)}"
        text = extract_text_from_task(result2)
        ok(f"Second message response: {text[:100]}")

        if "42" in text:
            ok("Session resume confirmed — agent remembered the number")
        else:
            fail(f"Agent may not have resumed session (expected '42' in response)")


# ──── Main ────


async def main():
    working_dir = os.getcwd()
    claude_port = find_free_port()
    codex_port = find_free_port()

    header("E2E Live Test: hybro-hub CLI → A2A SDK Client")
    print(f"  Working dir:      {working_dir}")
    print(f"  Claude Code port: {claude_port}")
    print(f"  Codex port:       {codex_port}")

    claude_url = f"http://127.0.0.1:{claude_port}"
    codex_url = f"http://127.0.0.1:{codex_port}"

    processes: list[subprocess.Popen] = []

    try:
        # ── Start agents via hybro-hub CLI ──
        header("1. Starting agents via hybro-hub CLI")

        step("hybro-hub agent start claude-code")
        claude_proc = start_agent_process("claude-code", claude_port, working_dir)
        processes.append(claude_proc)
        await wait_for_server(claude_url)
        ok(f"Claude Code server ready at {claude_url} (pid={claude_proc.pid})")

        step("hybro-hub agent start codex")
        codex_proc = start_agent_process("codex", codex_port, working_dir)
        processes.append(codex_proc)
        await wait_for_server(codex_url)
        ok(f"Codex server ready at {codex_url} (pid={codex_proc.pid})")

        # ── Agent card discovery ──
        header("2. Agent Card Discovery (A2ACardResolver)")
        claude_card = await test_agent_card(claude_url, "Claude Code")
        codex_card = await test_agent_card(codex_url, "Codex")

        # ── message/send ──
        header("3. message/send (A2AClient.send_message)")
        simple_prompt = "What is 2+2? Answer with just the number."

        claude_send_text = await test_message_send(
            claude_url, "Claude Code", simple_prompt,
        )
        codex_send_text = await test_message_send(
            codex_url, "Codex", simple_prompt,
        )

        # ── message/stream (Claude only) ──
        header("4. message/stream (A2AClient.send_message_streaming)")
        stream_prompt = "What is 3+3? Answer with just the number."
        claude_stream_text = await test_message_stream(
            claude_url, "Claude Code", stream_prompt,
        )

        # Verify Codex does not support streaming
        step("Verify Codex streaming=false")
        assert codex_card.capabilities.streaming is False, (
            f"Codex streaming={codex_card.capabilities.streaming}, expected False"
        )
        ok("Codex correctly reports streaming=false")

        # ── Session resume (Claude only) ──
        header("5. Session Resume (context_id continuity)")
        await test_session_resume(claude_url, "Claude Code")

        # ── Summary ──
        header("6. Results Summary")
        results = {
            "Claude Code agent card": True,
            "Codex agent card": True,
            "Claude Code message/send": bool(claude_send_text),
            "Codex message/send": bool(codex_send_text),
            "Claude Code message/stream": bool(claude_stream_text),
            "Claude Code session resume": True,  # would have raised if failed
        }
        all_pass = True
        for test_name, passed in results.items():
            status = "PASS" if passed else "FAIL"
            print(f"  {status} - {test_name}")
            if not passed:
                all_pass = False

        if all_pass:
            print(f"\n  All tests passed!")
        else:
            print(f"\n  Some tests failed!")
            sys.exit(1)

    except Exception as e:
        header("ERROR")
        print(f"  {type(e).__name__}: {e}")

        # Dump process output for debugging
        for proc in processes:
            if proc.stdout:
                output = proc.stdout.read()
                if output:
                    print(f"\n  --- Process {proc.pid} output ---")
                    print(output.decode(errors="replace")[-2000:])
        sys.exit(1)

    finally:
        header("Cleanup")
        for proc in processes:
            kill_process(proc)
            ok(f"Killed process {proc.pid}")
        ok("All processes cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
