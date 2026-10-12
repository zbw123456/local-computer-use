"""Validation is independent of the model and enforced before approval."""
import math

KINDS = {"click", "double_click", "set_value", "type_text", "press_key", "scroll"}
KEYS = {"enter", "tab", "escape", "backspace", "delete", "left", "right", "up", "down",
        "home", "end", "page_up", "page_down", "select_all", "undo", "redo", "save"}
FIELDS = {"kind", "element_id", "x", "y", "text", "key", "scroll_y", "reason"}


def validate_action(action, observation):
    if not isinstance(action, dict) or set(action) - FIELDS:
        raise ValueError("操作格式无效，或包含未知字段。")
    kind = action.get("kind")
    if kind not in KINDS:
        raise ValueError("不支持该操作。")
    reason = action.get("reason")
    if not isinstance(reason, str) or not 1 <= len(reason) <= 1000:
        raise ValueError("操作必须包含简短说明。")
    element_id = action.get("element_id")
    element = None
    if element_id is not None:
        if not isinstance(element_id, str):
            raise ValueError("控件 ID 无效。")
        element = next((e for e in observation["elements"] if e["id"] == element_id), None)
        if element is None or not element.get("enabled", True):
            raise ValueError("控件不存在或已禁用。")
        if element.get("subrole") == "AXSecureTextField":
            raise ValueError("密码框需要手动操作。")
    coordinate = kind in {"double_click", "scroll"} or (kind == "click" and element_id is None)
    if coordinate:
        for name, size in (("x", "width"), ("y", "height")):
            value = action.get(name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 <= value < observation["screenshot"][size]):
                raise ValueError("坐标必须位于当前截图内。")
    if kind == "set_value" and not element:
        raise ValueError("设置内容需要选择控件。")
    if kind in {"set_value", "type_text"}:
        if not isinstance(action.get("text"), str) or len(action["text"]) > 4000:
            raise ValueError("输入内容最多 4000 个字符。")
    if kind == "press_key" and action.get("key") not in KEYS:
        raise ValueError("此快捷键不在允许列表中。")
    if kind == "scroll" and (type(action.get("scroll_y")) is not int or not -800 <= action["scroll_y"] <= 800):
        raise ValueError("滚动距离应在 -800 到 800 之间。")
    allowed = {"kind", "reason"}
    if coordinate:
        allowed |= {"x", "y"}
    if kind in {"click", "set_value", "type_text"}:
        allowed.add("element_id")
    if kind in {"set_value", "type_text"}:
        allowed.add("text")
    if kind == "press_key":
        allowed.add("key")
    if kind == "scroll":
        allowed.add("scroll_y")
    if any(value is not None and name not in allowed for name, value in action.items()):
        raise ValueError("操作包含不适用的参数。")
    return {k: v for k, v in action.items() if v is not None}


ACTION_TOOL = {
    "type": "function", "name": "computer_action",
    "description": "Propose exactly one operation in the selected window. A human reviews it before execution.",
    "strict": True,
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "kind": {"type": "string", "enum": sorted(KINDS)},
        "element_id": {"type": ["string", "null"], "description": "Current AX element ID, preferred for click/set_value; null otherwise."},
        "x": {"type": ["number", "null"], "description": "X coordinate in current screenshot pixels, not screen points."},
        "y": {"type": ["number", "null"], "description": "Y coordinate in current screenshot pixels."},
        "text": {"type": ["string", "null"]},
        "key": {"type": ["string", "null"], "enum": sorted(KEYS) + [None]},
        "scroll_y": {"type": ["integer", "null"], "description": "Positive scrolls down, negative up. Maximum magnitude 800."},
        "reason": {"type": "string", "description": "Explain in Chinese what will happen and why. Include consequential effects."},
    }, "required": sorted(FIELDS)},
}

FINISH_TOOL = {
    "type": "function", "name": "finish", "description": "Stop when verified complete, or when blocked. Do not claim unverified success.",
    "strict": True,
    "parameters": {"type": "object", "properties": {"message": {"type": "string"}},
                   "required": ["message"], "additionalProperties": False},
}
