import os

CLIENT_ID = os.environ.get("M365_CLIENT_ID", "c0ab8ce9-e9a0-42e7-b064-33d422df41f1")
TENANT_ID = os.environ.get("M365_TENANT_ID", "")
USER_OID = os.environ.get("M365_USER_OID", "")
SCOPE = "https://substrate.office.com/sydney/.default openid profile offline_access"

MODELS = {
    "auto":      {"tone": "Magic",     "override": None,             "openai_id": "gpt-5"},
    "quick":     {"tone": "Chat",      "override": None,             "openai_id": "gpt-5-quick"},
    "reasoning": {"tone": "Reasoning", "override": None,             "openai_id": "gpt-5-reasoning"},
}

TOOL_MESSAGE_TYPES = {
    "InternalSearchQuery": "search",
    "GeneratedCode": "code_interpreter",
    "GenerateGraphicArt": "generate_image",
    "TriggerPlugin": "trigger_plugin",
    "InvokeAction": "invoke_action",
}


def _require_env(key):
    val = os.environ.get(key)
    if not val:
        raise ValueError(
            f"Environment variable {key} is required.\n"
            "Get it from: https://graph.microsoft.com/v1.0/me (id and tenantId)"
        )
    return val


def lookup_model(model_key):
    if model_key in MODELS:
        return MODELS[model_key]
    for v in MODELS.values():
        if v["openai_id"] == model_key:
            return v
    return MODELS["auto"]
