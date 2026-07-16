import re, json
from typing import Optional, Dict, Tuple


def _extract_json_objects(text: str):
    """Extract all balanced JSON objects from text using bracket matching."""
    results = []
    i = 0
    while i < len(text):
        if text[i] == '{':
            depth = 1
            j = i + 1
            in_string = False
            escape = False
            while j < len(text) and depth > 0:
                c = text[j]
                if escape:
                    escape = False
                elif c == '\\':
                    escape = True
                elif c == '"' and not escape:
                    in_string = not in_string
                elif not in_string:
                    if c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                j += 1
            if depth == 0:
                results.append(text[i:j])
                i = j
                continue
        i += 1
    return results


def detect_tool_intent(text):
    tl = text.lower()
    if any(w in tl for w in [
        "what time", "current time", "time now", "date today",
        "today's date", "what's the time", "current date",
    ]):
        return "get_current_time"
    if any(w in tl for w in ["calculate", "compute", "evaluate", "solve"]):
        return "calculate"
    if bool(re.search(r"\d+\s*[\+\-\*\/\(\)]", tl)):
        return "calculate"
    if any(w in tl for w in ["dice", "roll", "die", "random number"]):
        return "roll_dice"
    return None


def extract_tool_args(text, tool_name):
    tl = text.lower()
    if tool_name == "calculate":
        for prefix in ["calculate ", "compute ", "evaluate ", "solve "]:
            if prefix in tl:
                expr = tl.split(prefix, 1)[1].strip()
                expr = re.sub(r"[^0-9+\-*/().%\s]", "", expr)
                if expr:
                    return {"expression": expr}
        matches = re.findall(r"[\d\s+\-*/().]+\d", tl)
        if matches:
            expr = matches[0].strip()
            if any(op in expr for op in ["+", "-", "*", "/"]):
                return {"expression": expr}
    if tool_name == "roll_dice":
        m = re.search(r"(\d+)[- ]sided", tl)
        if m:
            return {"sides": int(m.group(1))}
        m = re.search(r"d(\d+)", tl)
        if m:
            return {"sides": int(m.group(1))}
        return {"sides": 6}
    if tool_name == "get_current_time":
        return {}
    return {}


class ToolCallDetector:
    @staticmethod
    def _normalize_args(args):
        if args is None:
            return {}
        if isinstance(args, str):
            try:
                return json.loads(args)
            except json.JSONDecodeError:
                return {"raw": args}
        return args

    @staticmethod
    def _extract_name_and_args(data: dict):
        if not isinstance(data, dict):
            return None, None
        name = data.get("name") or data.get("tool")
        if not name and "function" in data and isinstance(data["function"], dict):
            name = data["function"].get("name")
            if name:
                return name, data["function"].get("arguments")
        if name:
            return name, data.get("arguments")
        return None, None

    @staticmethod
    def _from_parsed(data) -> Optional[Tuple[str, Dict]]:
        if isinstance(data, list):
            for item in data:
                found = ToolCallDetector._from_parsed(item)
                if found:
                    return found
            return None
        if not isinstance(data, dict):
            return None
        # OpenAI-ish: {"tool_calls":[{"function":{...}}]}
        if isinstance(data.get("tool_calls"), list):
            for tc in data["tool_calls"]:
                found = ToolCallDetector._from_parsed(tc)
                if found:
                    return found
        name, args = ToolCallDetector._extract_name_and_args(data)
        if name:
            return name, ToolCallDetector._normalize_args(args)
        return None

    @staticmethod
    def detect(text: str) -> Optional[Tuple[str, Dict]]:
        if not text:
            return None
        # Prefer fenced JSON blocks first
        blocks = re.findall(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL | re.IGNORECASE)
        for block in blocks:
            try:
                data = json.loads(block.strip())
                found = ToolCallDetector._from_parsed(data)
                if found:
                    return found
            except json.JSONDecodeError:
                continue

        # Whole-text JSON
        stripped = text.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                data = json.loads(stripped)
                found = ToolCallDetector._from_parsed(data)
                if found:
                    return found
            except json.JSONDecodeError:
                pass

        for obj_str in _extract_json_objects(text):
            if '"name"' not in obj_str and '"tool"' not in obj_str and '"function"' not in obj_str:
                continue
            try:
                data = json.loads(obj_str)
                found = ToolCallDetector._from_parsed(data)
                if found:
                    return found
            except json.JSONDecodeError:
                continue
        return None
