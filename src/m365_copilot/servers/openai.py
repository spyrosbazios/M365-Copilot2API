import json, os, sys, asyncio, time, uuid, logging, http.server, socketserver, threading, re
from functools import lru_cache

from .. import __version__
from ..auth import TokenManager
from ..client import _clean_citations, M365Client
from ..models import MODELS, lookup_model, TENANT_ID, USER_OID, CLIENT_ID, SCOPE
from ..tools import provider
from ..tools.mcp_bridge import MCPBridge
from ..scripts.plugin_loader import load_user_tools
from .. import agent_optimize as ao

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
load_user_tools()

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
RT_FILE = os.path.join(BASE_DIR, "data", "tokens", "rt_90day.txt")
CACHE_FILE = os.path.join(BASE_DIR, "data", "tokens", "token_cache.json")

ANTHROPIC_MODEL_MAP = {
    "gpt-4o": "auto",
    "gpt-4": "auto",
}


def _resolve_anthropic_model(anthropic_id):
    if anthropic_id in ANTHROPIC_MODEL_MAP:
        return ANTHROPIC_MODEL_MAP[anthropic_id]
    for prefix, mapped in ANTHROPIC_MODEL_MAP.items():
        if anthropic_id.startswith(prefix):
            return mapped
    return "auto"


def sse_msg(data, chunk_id=None, model="gpt-4"):
    if chunk_id is None:
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    chunk = {
        "id": chunk_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": data, "finish_reason": None}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


def sse_done(chunk_id=None, model="gpt-4", usage=None, reasoning=None, finish_reason="stop"):
    if chunk_id is None:
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    chunk = {
        "id": chunk_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    if usage:
        chunk["usage"] = usage
    out = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
    out += "data: [DONE]\n\n"
    return out


def inject_tools_prompt(messages, tools, tool_choice=None):
    ao.inject_tools_prompt(messages, tools, tool_choice=tool_choice)


def _merge_tool_calls(native_tool_calls, text, tools=None, alias_to_real=None):
    """Prefer native M365 tool events; fall back to text JSON detection."""
    if native_tool_calls:
        return list(native_tool_calls), "tool_calls"
    if tools is not None:
        parsed = ao.detect_tool_calls(text, tools=tools, alias_to_real=alias_to_real)
        if parsed:
            return parsed, "tool_calls"
    return [], "stop"


def _usage_from(messages, text):
    prompt_str = str(messages)
    completion = (text or "").split()
    return {
        "prompt_tokens": len(prompt_str.split()),
        "completion_tokens": len(completion),
        "total_tokens": len(prompt_str.split()) + len(completion),
    }


# Lightweight request metrics for /health
_METRICS = {
    "requests": 0,
    "tool_calls": 0,
    "refusals": 0,
    "retries": 0,
    "errors": 0,
    "last_latency_ms": 0,
}


def inject_json_mode(messages):
    for m in messages:
        if m.get("role") == "system":
            existing = m.get("content", "")
            m["content"] = (existing + "\n" if existing else "") + (
                "You MUST respond with valid JSON only. "
                "Do not include markdown code blocks, explanation, or any text outside the JSON object."
            )
            return
    messages.insert(0, {
        "role": "system",
        "content": "You MUST respond with valid JSON only. Do not include markdown code blocks, explanation, or any text outside the JSON object."
    })


def fim_to_chat(prompt, suffix=None):
    if suffix:
        return [
            {"role": "user", "content": f"Complete the middle of the following text naturally.\n\n--- BEGIN TEXT ---\n{prompt}\n--- MIDDLE ---\n{suffix}\n--- END ---\n\nWrite only the middle part that connects the two sections."}
        ]
    return [
        {"role": "user", "content": f"Continue writing from this point:\n\n{prompt}"}
    ]


_loop = None
_tm = None
_clients = {}  # session_key -> M365Client (warm WS per session)
_mcp = None
_client_lock = threading.RLock()
_DEFAULT_CLIENT_KEY = "__default__"


def _get_loop():
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
    return _loop


def _get_token_manager():
    global _tm
    if _tm is None:
        _tm = TokenManager(TENANT_ID, CLIENT_ID, SCOPE, RT_FILE, CACHE_FILE)
    return _tm


def _get_client(session_key=None):
    """Return a per-session client so WS can stay warm across turns."""
    key = session_key or _DEFAULT_CLIENT_KEY
    with _client_lock:
        client = _clients.get(key)
        if client is None:
            client = M365Client(_get_token_manager())
            _clients[key] = client
            # Bound growth
            if len(_clients) > 32:
                oldest = next(iter(_clients))
                if oldest != key:
                    old = _clients.pop(oldest, None)
                    if old:
                        try:
                            _run_async(old.close())
                        except Exception:
                            pass
        return client


def _run_async(coro):
    loop = _get_loop()
    return loop.run_until_complete(coro)


def _get_mcp():
    global _mcp
    if _mcp is None:
        _mcp = MCPBridge()
        try:
            _mcp.discover_local_mcp()
        except Exception as e:
            logging.warning(f"MCP discovery failed: {e}")
    return _mcp


class OpenAIHandler(http.server.BaseHTTPRequestHandler):
    _session_conv = {}  # session_id → conversation_id (in-memory only)

    def _get_conv_id(self, req):
        sid = req.get("session_id") or self.headers.get("X-Session-Id") or req.get("user")
        if not sid:
            return None
        conv_id = self._session_conv.get(sid)
        if not conv_id:
            conv_id = uuid.uuid4().hex
            self._session_conv[sid] = conv_id
        return conv_id

    def log_message(self, format, *args):
        logging.info(f"{self.client_address[0]} - {format % args}")

    def _send_json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, code, msg):
        self._send_json(code, {"error": {"message": msg, "type": "error", "code": code}})

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _parse_params(self, req):
        model = req.get("model", "auto")
        messages = req.get("messages", [])
        stream = bool(req.get("stream", False))
        tools = req.get("tools", None)
        tool_choice = req.get("tool_choice", None)
        response_format = req.get("response_format", None) or {}

        cfg = lookup_model(model)
        if not cfg:
            return self._send_error(400, f"Unknown model: {model}")
        return model, messages, stream, tools, tool_choice, response_format, cfg

    # ---- HTTP routing ----
    def do_GET(self):
        if self.path == "/v1/models":
            models = [{"id": v["openai_id"], "object": "model", "created": 1700000000, "owned_by": "microsoft"} for v in MODELS.values()]
            self._send_json(200, {"object": "list", "data": models})
        elif self.path in ("/health", "/v1/health"):
            self._send_json(200, {
                "status": "ok",
                "version": __version__,
                "metrics": dict(_METRICS),
                "sessions": len(_clients),
                "bing": ao.ENABLE_BING,
            })
        else:
            self._send_error(404, "Not found")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Session-Id")
        self.end_headers()

    def do_POST(self):
        path = self.path.rstrip("/")
        try:
            req = self._read_body()
        except Exception as e:
            self._send_error(400, f"Invalid JSON: {e}")
            return

        if path == "/v1/chat/completions":
            self._handle_chat(req)
        elif path == "/v1/completions":
            self._handle_completions(req)
        elif path == "/v1/messages":
            self._handle_anthropic_messages(req)
        elif path == "/v1/complete":
            self._handle_anthropic_complete(req)
        else:
            self._send_error(404, f"Not found: {self.path}")

    # ---- OpenAI Chat Completions ----
    def _handle_chat(self, req):
        parsed = self._parse_params(req)
        if parsed is None:
            return
        model, messages, stream, tools, tool_choice, response_format, cfg = parsed
        t0 = time.time()
        _METRICS["requests"] += 1

        if response_format.get("type") == "json_object":
            inject_json_mode(messages)

        # Compress history, alias/slim tools, inject compact tool prompt
        prepared, aliased_tools, alias_to_real, meta = ao.prepare_chat_request(
            messages, tools, tool_choice=tool_choice
        )
        logging.info(
            "chat prepare model=%s mode=%s msgs=%s tools=%s system_chars=%s force_local=%s",
            model, meta.get("mode"), meta.get("message_count"), meta.get("tool_count"),
            meta.get("system_chars"), meta.get("force_local"),
        )

        conv_id = self._get_conv_id(req)
        session_key = (
            req.get("session_id")
            or self.headers.get("X-Session-Id")
            or req.get("user")
            or _DEFAULT_CLIENT_KEY
        )
        client = _get_client(session_key)

        # Do not hold the global lock across M365 RTT — only client lookup is locked.
        if stream:
            self._stream_chat(
                prepared, cfg, client, conv_id,
                tools=aliased_tools, alias_to_real=alias_to_real, t0=t0,
                meta=meta,
            )
        else:
            self._non_stream_chat(
                prepared, cfg, client, conv_id,
                tools=aliased_tools, alias_to_real=alias_to_real, t0=t0,
                meta=meta,
            )

    def _write_sse(self, data):
        try:
            self.wfile.write(data.encode())
            self.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            pass

    def _emit_tool_call_chunks(self, tool_calls, chunk_id, openai_model):
        self._write_sse(sse_msg({"role": "assistant", "content": None}, chunk_id, openai_model))
        for i, tc in enumerate(tool_calls):
            self._write_sse(sse_msg({
                "tool_calls": [{
                    "index": i,
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }]
            }, chunk_id, openai_model))

    async def _collect_completion(self, client, messages, tone, gpt_override, conv_id):
        full_text = ""
        async for chunk, is_final in client.chat_conversation_stream_gen(
            messages, tone, gpt_override, conversation_id=conv_id,
            enable_bing=ao.ENABLE_BING,
        ):
            if is_final:
                break
            chunk = _clean_citations(chunk)
            if chunk:
                full_text += chunk
                yield chunk, full_text, False
        yield "", full_text, True

    def _stream_chat(self, messages, cfg, client, conv_id=None, tools=None,
                     alias_to_real=None, t0=None, meta=None):
        chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        openai_model = cfg["openai_id"]
        tone = cfg["tone"]
        gpt_override = cfg["override"]
        has_tools = bool(tools)
        meta = meta or {}
        agent_mode = meta.get("mode") == "agent" or has_tools
        force_local = bool(meta.get("force_local"))

        try:
            async def stream_loop():
                # Web asks always fully buffer so we can convert fake prose into tool_calls.
                web_ask = ao.needs_web_tools(messages)
                soft_stream = agent_mode and not ao.AGENT_RETRY and not web_ask
                buffer_all = agent_mode and not soft_stream
                has_content = False
                full_text = ""

                async def _drain(msg_list):
                    nonlocal full_text, has_content, buffer_all
                    full_text = ""
                    has_content = False
                    hold = ""
                    async for chunk, is_final in client.chat_conversation_stream_gen(
                        msg_list, tone, gpt_override, conversation_id=conv_id,
                        enable_bing=ao.ENABLE_BING,
                    ):
                        if is_final:
                            break
                        chunk = _clean_citations(chunk)
                        if not chunk:
                            continue
                        full_text += chunk
                        if buffer_all:
                            continue
                        if soft_stream and not has_content:
                            hold += chunk
                            # Still might be tool JSON — keep holding briefly
                            if len(hold) < 48 and ao.looks_like_tool_json(hold):
                                continue
                            if ao.looks_like_tool_json(hold) and len(hold) < 400:
                                # likely a tool call — stay buffered for rewrite
                                buffer_all = True
                                continue
                            chunk = hold
                            hold = ""
                        if not has_content:
                            self._write_sse(sse_msg(
                                {"role": "assistant", "content": chunk},
                                chunk_id, openai_model,
                            ))
                            has_content = True
                        else:
                            self._write_sse(sse_msg(
                                {"content": chunk}, chunk_id, openai_model,
                            ))

                await _drain(messages)

                tool_calls, finish_reason = _merge_tool_calls(
                    client._last_tool_calls, full_text,
                    tools=tools, alias_to_real=alias_to_real,
                )
                # Hard guarantee: web asks always produce a real tool_call for Pi
                forced = ao.ensure_web_tool_calls(
                    tool_calls, messages, tools=tools,
                    alias_to_real=alias_to_real, full_text=full_text,
                )
                if forced and not tool_calls:
                    tool_calls = forced
                    finish_reason = "tool_calls"
                    logging.info(
                        "Forced web tool_call (model skipped tools): %s",
                        [tc["function"]["name"] for tc in tool_calls],
                    )

                # One retry only in agent mode (avoid doubling pure-chat latency)
                if (
                    ao.AGENT_RETRY
                    and agent_mode
                    and has_tools
                    and not tool_calls
                    and not client._last_tool_calls
                ):
                    preferred = ao.preferred_helper_for(messages, tools)
                    retry_msgs = None
                    if ao.is_refusal(full_text):
                        _METRICS["refusals"] += 1
                        retry_msgs = ao.force_tool_retry_messages(messages, preferred)
                        logging.info("Refusal detected; forcing helper retry")
                    elif ao.is_tool_hallucination(full_text) or ao.is_fake_search(full_text):
                        _METRICS["refusals"] += 1
                        retry_msgs = ao.force_tool_retry_messages(messages, preferred)
                        logging.info(
                            "Fake tool/search detected; forcing helper retry (%s)",
                            preferred,
                        )
                    elif force_local and (
                        ao.needs_web_tools(messages)
                        or len((full_text or "").strip()) < 280
                    ):
                        # Web asks always retry if no tool_calls (models often dump memory lists).
                        # Local: only short prose dodge (skip long answers for latency).
                        retry_msgs = ao.force_tool_retry_messages(messages, preferred)
                        logging.info(
                            "Agent action without tools; forcing helper retry (%s)",
                            preferred,
                        )
                    if retry_msgs is not None:
                        _METRICS["retries"] += 1
                        await _drain(retry_msgs)
                        tool_calls, finish_reason = _merge_tool_calls(
                            client._last_tool_calls, full_text,
                            tools=tools, alias_to_real=alias_to_real,
                        )
                usage = _usage_from(messages, full_text)

                if tool_calls:
                    _METRICS["tool_calls"] += 1
                    logging.info(
                        "Emitting tool_calls: %s",
                        [tc["function"]["name"] for tc in tool_calls],
                    )
                    self._emit_tool_call_chunks(tool_calls, chunk_id, openai_model)
                    self._write_sse(sse_done(
                        chunk_id, openai_model, usage, finish_reason="tool_calls",
                    ))
                    return

                if buffer_all and full_text:
                    self._write_sse(sse_msg(
                        {"role": "assistant", "content": full_text},
                        chunk_id, openai_model,
                    ))
                elif not has_content and not full_text:
                    self._write_sse(sse_msg(
                        {"role": "assistant", "content": ""}, chunk_id, openai_model,
                    ))

                self._write_sse(sse_done(
                    chunk_id, openai_model, usage, finish_reason=finish_reason,
                ))

            _run_async(stream_loop())
            if t0 is not None:
                _METRICS["last_latency_ms"] = int((time.time() - t0) * 1000)
        except Exception as e:
            _METRICS["errors"] += 1
            logging.exception("stream chat failed")
            err = {"id": chunk_id, "object": "chat.completion.chunk",
                   "created": int(time.time()), "model": openai_model,
                   "choices": [{"index": 0, "delta": {"content": f"Error: {e}"}, "finish_reason": "stop"}]}
            self._write_sse(f"data: {json.dumps(err)}\n\n")
            self._write_sse("data: [DONE]\n\n")

    def _non_stream_chat(self, messages, cfg, client, conv_id=None, tools=None,
                         alias_to_real=None, t0=None, meta=None):
        meta = meta or {}
        agent_mode = meta.get("mode") == "agent" or bool(tools)
        force_local = bool(meta.get("force_local"))
        openai_model = cfg["openai_id"]
        tone = cfg["tone"]
        gpt_override = cfg["override"]

        try:
            result_text, native_tool_calls, finish_reason = _run_async(
                client.chat_conversation(
                    messages, tone, gpt_override, conversation_id=conv_id,
                    enable_bing=ao.ENABLE_BING,
                )
            )
        except Exception as e:
            _METRICS["errors"] += 1
            self._send_error(500, str(e))
            return

        tool_calls, finish_reason = _merge_tool_calls(
            native_tool_calls, result_text,
            tools=tools, alias_to_real=alias_to_real,
        )
        forced = ao.ensure_web_tool_calls(
            tool_calls, messages, tools=tools,
            alias_to_real=alias_to_real, full_text=result_text or "",
        )
        if forced and not tool_calls:
            tool_calls = forced
            finish_reason = "tool_calls"
            logging.info(
                "Forced web tool_call (model skipped tools): %s",
                [tc["function"]["name"] for tc in tool_calls],
            )

        if ao.AGENT_RETRY and agent_mode and tools and not tool_calls:
            preferred = ao.preferred_helper_for(messages, tools)
            retry_msgs = None
            if ao.is_refusal(result_text):
                _METRICS["refusals"] += 1
                retry_msgs = ao.force_tool_retry_messages(messages, preferred)
                logging.info("Refusal detected; forcing helper retry")
            elif ao.is_tool_hallucination(result_text) or ao.is_fake_search(result_text):
                _METRICS["refusals"] += 1
                retry_msgs = ao.force_tool_retry_messages(messages, preferred)
                logging.info("Fake tool/search detected; forcing helper retry")
            elif force_local and (
                ao.needs_web_tools(messages)
                or len((result_text or "").strip()) < 280
            ):
                retry_msgs = ao.force_tool_retry_messages(messages, preferred)
                logging.info("Agent action without tools; forcing helper retry")
            if retry_msgs is not None:
                _METRICS["retries"] += 1
                try:
                    result_text, native_tool_calls, finish_reason = _run_async(
                        client.chat_conversation(
                            retry_msgs, tone, gpt_override, conversation_id=conv_id,
                            enable_bing=ao.ENABLE_BING,
                        )
                    )
                    tool_calls, finish_reason = _merge_tool_calls(
                        native_tool_calls, result_text,
                        tools=tools, alias_to_real=alias_to_real,
                    )
                except Exception as e:
                    logging.warning("retry failed: %s", e)

        msg = {"role": "assistant", "content": result_text if result_text else None}
        if tool_calls:
            _METRICS["tool_calls"] += 1
            msg["tool_calls"] = tool_calls
            msg["content"] = None
            logging.info(
                "Emitting tool_calls: %s",
                [tc["function"]["name"] for tc in tool_calls],
            )
        elif not result_text:
            msg["content"] = None

        if t0 is not None:
            _METRICS["last_latency_ms"] = int((time.time() - t0) * 1000)

        response = {
            "id": f"chatcmpl-{uuid.uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": openai_model,
            "choices": [{"index": 0, "message": msg, "finish_reason": finish_reason}],
            "usage": _usage_from(messages, result_text),
        }
        self._send_json(200, response)

    # ---- OpenAI Completions (FIM / legacy) ----
    def _handle_completions(self, req):
        model = req.get("model", "auto")
        prompt = req.get("prompt", "")
        suffix = req.get("suffix", None)
        stream = bool(req.get("stream", False))
        max_tokens = req.get("max_tokens", 512)

        cfg = lookup_model(model)
        if not cfg:
            self._send_error(400, f"Unknown model: {model}")
            return

        messages = fim_to_chat(prompt, suffix)

        client = _get_client()

        if stream:
            self._stream_completions(messages, cfg, client, prompt[:20])
        else:
            self._non_stream_completions(messages, cfg, client)

    def _stream_completions(self, messages, cfg, client, partial_prompt):
        openai_model = cfg["openai_id"]
        tone = cfg["tone"]
        gpt_override = cfg["override"]
        comp_id = f"cmpl-{uuid.uuid4().hex}"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            async def stream_loop():
                async for chunk, is_final in client.chat_conversation_stream_gen(messages, tone, gpt_override):
                    if is_final:
                        break
                    cdata = {
                        "id": comp_id, "object": "text_completion",
                        "created": int(time.time()), "model": openai_model,
                        "choices": [{"index": 0, "text": chunk, "finish_reason": None, "logprobs": None}],
                    }
                    self.wfile.write(f"data: {json.dumps(cdata, ensure_ascii=False)}\n\n".encode())
                done = {
                    "id": comp_id, "object": "text_completion",
                    "created": int(time.time()), "model": openai_model,
                    "choices": [{"index": 0, "text": "", "finish_reason": "stop", "logprobs": None}],
                }
                self.wfile.write(f"data: {json.dumps(done, ensure_ascii=False)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")

            _run_async(stream_loop())
        except Exception as e:
            err = f"data: {json.dumps({'id': comp_id, 'object': 'text_completion', 'created': int(time.time()), 'model': openai_model, 'choices': [{'index': 0, 'text': f'Error: {e}', 'finish_reason': 'stop', 'logprobs': None}]})}\n\n"
            self.wfile.write(err.encode())
            self.wfile.write(b"data: [DONE]\n\n")

    def _non_stream_completions(self, messages, cfg, client):
        openai_model = cfg["openai_id"]
        tone = cfg["tone"]
        gpt_override = cfg["override"]

        try:
            result_text, _, _ = _run_async(
                client.chat_conversation(messages, tone, gpt_override)
            )
        except Exception as e:
            self._send_error(500, str(e))
            return

        response = {
            "id": f"cmpl-{uuid.uuid4().hex}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": openai_model,
            "choices": [{"index": 0, "text": result_text, "finish_reason": "stop", "logprobs": None}],
            "usage": {
                "prompt_tokens": len(str(messages).split()),
                "completion_tokens": len(result_text.split()),
                "total_tokens": len(str(messages).split()) + len(result_text.split()),
            },
        }
        self._send_json(200, response)

    # ---- Anthropic Messages API ----
    def _handle_anthropic_messages(self, req):
        model = req.get("model", "claude-sonnet-4-20250514")
        messages = req.get("messages", [])
        system_prompt = req.get("system", "")
        stream = bool(req.get("stream", False))
        max_tokens = req.get("max_tokens", 1024)
        temperature = req.get("temperature", 1.0)

        mapped = _resolve_anthropic_model(model)
        cfg = lookup_model(mapped)

        chat_messages = []
        if system_prompt:
            chat_messages.append({"role": "system", "content": system_prompt})
        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, list):
                texts = [p.get("text", "") for p in content if p.get("type") == "text"]
                content = " ".join(texts)
            chat_messages.append({"role": role, "content": content})

        client = _get_client()

        if stream:
            self._anthropic_stream_messages(chat_messages, cfg, client, model)
        else:
            self._anthropic_non_stream_messages(chat_messages, cfg, client, model)

    def _anthropic_stream_messages(self, chat_messages, cfg, client, anthropic_model):
        openai_model = cfg["openai_id"]
        tone = cfg["tone"]
        gpt_override = cfg["override"]
        msg_id = f"msg_{uuid.uuid4().hex}"

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        try:
            async def stream_loop():
                full_text = ""
                header = {
                    "type": "message_start",
                    "message": {
                        "id": msg_id, "type": "message", "role": "assistant",
                        "content": [], "model": anthropic_model,
                        "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": len(str(chat_messages).split()), "output_tokens": 0},
                    },
                }
                self.wfile.write(f"event: message_start\ndata: {json.dumps(header, ensure_ascii=False)}\n\n".encode())

                cb_start = {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
                self.wfile.write(f"event: content_block_start\ndata: {json.dumps(cb_start, ensure_ascii=False)}\n\n".encode())

                async for chunk, is_final in client.chat_conversation_stream_gen(chat_messages, tone, gpt_override):
                    if is_final:
                        break
                    full_text += chunk
                    delta = {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": chunk}}
                    self.wfile.write(f"event: content_block_delta\ndata: {json.dumps(delta, ensure_ascii=False)}\n\n".encode())

                cb_stop = {"type": "content_block_stop", "index": 0}
                self.wfile.write(f"event: content_block_stop\ndata: {json.dumps(cb_stop, ensure_ascii=False)}\n\n".encode())

                msg_delta = {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": len(full_text.split())},
                }
                self.wfile.write(f"event: message_delta\ndata: {json.dumps(msg_delta, ensure_ascii=False)}\n\n".encode())

                msg_stop = {"type": "message_stop"}
                self.wfile.write(f"event: message_stop\ndata: {json.dumps(msg_stop, ensure_ascii=False)}\n\n".encode())

            _run_async(stream_loop())
        except Exception as e:
            self.wfile.write(f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'server_error', 'message': str(e)}})}\n\n".encode())

    def _anthropic_non_stream_messages(self, chat_messages, cfg, client, anthropic_model):
        openai_model = cfg["openai_id"]
        tone = cfg["tone"]
        gpt_override = cfg["override"]

        try:
            result_text, tool_calls, finish_reason = _run_async(
                client.chat_conversation(chat_messages, tone, gpt_override)
            )
        except Exception as e:
            self._send_error(500, str(e))
            return

        stop_reason = {"tool_calls": "tool_use", "stop": "end_turn"}.get(finish_reason or "stop", "end_turn")
        response = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": result_text or ""}],
            "model": anthropic_model,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": len(str(chat_messages).split()),
                "output_tokens": len((result_text or "").split()),
            },
        }
        if tool_calls:
            for tc in tool_calls:
                response["content"].append({
                    "type": "tool_use",
                    "id": tc.get("id", f"tu_{uuid.uuid4().hex}"),
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"]["arguments"]),
                })
        self._send_json(200, response)

    # ---- Anthropic Complete (FIM) ----
    def _handle_anthropic_complete(self, req):
        model = req.get("model", "claude-sonnet-4-20250514")
        prompt = req.get("prompt", "")
        max_tokens_to_sample = req.get("max_tokens_to_sample", 300)
        stream = bool(req.get("stream", False))
        stop_sequences = req.get("stop_sequences", [])

        mapped = _resolve_anthropic_model(model)
        cfg = lookup_model(mapped)

        messages = fim_to_chat(prompt)

        client = _get_client()

        try:
            result_text, _, _ = _run_async(
                client.chat_conversation(messages, cfg["tone"], cfg["override"])
            )
        except Exception as e:
            self._send_error(500, str(e))
            return

        response = {
            "completion": result_text,
            "stop_reason": "stop_sequence" if any(s in result_text for s in stop_sequences) else "end_turn",
            "model": model,
            "stop": None,
            "log_id": f"cmpl_{uuid.uuid4().hex}",
        }
        self._send_json(200, response)


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    import argparse
    parser = argparse.ArgumentParser(description=f"M365 Copilot API Server v{__version__}")
    parser.add_argument("--port", type=int, default=8000, help="鐩戝惉绔彛")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="缁戝畾鍦板潃")
    parser.add_argument("--setup", action="store_true", help="棣栨璁剧疆")
    args = parser.parse_args()

    if not TENANT_ID or not USER_OID:
        print("错误: M365_TENANT_ID 和 M365_USER_OID 未配置")
        print("运行: m365-copilot-setup 或 python -m m365_copilot.scripts.setup_wizard")
        sys.exit(1)

    os.makedirs(os.path.dirname(RT_FILE), exist_ok=True)
    tm = TokenManager(TENANT_ID, CLIENT_ID, SCOPE, RT_FILE, CACHE_FILE)

    if args.setup:
        from ..scripts.setup_wizard import main as setup_main
        setup_main()
        return

    if not os.path.exists(RT_FILE):
        print(f"棣栨浣跨敤: m365-copilot-setup")
        sys.exit(1)

    try:
        tm.get()
        print("Token OK")
    except Exception as e:
        print(f"Token 澶辫触: {e}")
        sys.exit(1)

    # Pre-warm SignalR so the first user request skips handshake RTT
    try:
        warm = _get_client("__warmup__")
        _run_async(warm._ensure_ws())
        print("WS pre-warmed")
    except Exception as e:
        print(f"WS pre-warm skipped: {e}")

    server = ThreadedServer((args.host, args.port), OpenAIHandler)
    print(f"M365 Copilot API Server v{__version__}")
    print(f"  http://{args.host}:{args.port}")
    print(f"  mode: fast_path={ao.FAST_PATH} agent_retry={ao.AGENT_RETRY} scoped_tools={ao.SCOPED_TOOLS}")
    print(f"  POST /v1/chat/completions  (OpenAI)")
    print(f"  POST /v1/completions        (OpenAI FIM)")
    print(f"  POST /v1/messages           (Anthropic)")
    print(f"  POST /v1/complete           (Anthropic FIM)")
    print(f"  GET  /v1/models             (model list)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


