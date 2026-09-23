"""Cloud pairs used by the single-key profiles; credentials never belong here."""

PRESETS = {
    "openai": {
        "LLM_PROVIDER": "openai",
        "LLM_BASE_URL": "https://api.openai.com/v1",
        "LLM_MODEL": "gpt-4.1-mini",
        "LLM_IS_REASONING_MODEL": "false",
        "EMBEDDING_PROVIDER": "openai",
        "EMBEDDING_BASE_URL": "https://api.openai.com/v1",
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "EMBEDDING_DIMS": "1536",
        "EMBEDDING_SEND_DIMENSIONS": "true",
    },
    "qwen": {
        "LLM_PROVIDER": "openai",
        "LLM_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "LLM_MODEL": "qwen-plus",
        "LLM_IS_REASONING_MODEL": "false",
        "EMBEDDING_PROVIDER": "openai",
        "EMBEDDING_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "EMBEDDING_MODEL": "text-embedding-v4",
        "EMBEDDING_DIMS": "1024",
        "EMBEDDING_SEND_DIMENSIONS": "true",
    },
}
