import os
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import OpenAI


load_dotenv()


@dataclass
class ChatResponse:
    content: str


class OpenAIChatModel:
    def __init__(self, model_id: str, api_key: str, base_url: str | None, temperature: float, max_tokens: int):
        self.model_id = model_id
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def invoke(self, messages: list[dict]) -> ChatResponse:
        resp = self.client.chat.completions.create(
            model=self.model_id,
            messages=messages,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            extra_body={"enable_thinking": False},
        )
        content = ""
        if resp.choices and resp.choices[0].message:
            content = resp.choices[0].message.content or ""
        return ChatResponse(content=content)


# 加载大语言模型

def get_chat_model() -> OpenAIChatModel:
    model_ID = os.getenv("LLM_MODEL_ID")
    api_key = os.getenv("LLM_API_KEY")
    base_url = os.getenv("LLM_BASE_URL")
    max_tokens_env = os.getenv("LLM_MAX_TOKENS")

    if model_ID is None or api_key is None:
        raise ValueError("LLM_MODEL_ID and LLM_API_KEY must be set in the environment variables.")

    max_tokens = 4096
    if max_tokens_env:
        try:
            max_tokens = max(int(max_tokens_env), 512)
        except Exception:
            max_tokens = 4096

    return OpenAIChatModel(
        model_id=model_ID,
        api_key=api_key,
        base_url=base_url,
        temperature=0.7,
        max_tokens=max_tokens,
    )
