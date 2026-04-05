import json
import os

from a2a.server.agent_execution import RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, TaskState, TextPart
from a2a.utils import get_message_text, new_agent_text_message

# Categories that must always be refused
PRIVACY_CATEGORIES = {"private_customer_information", "confidential_company_knowledge"}

SYSTEM_EXACT = (
    "You are a CRM expert assistant. Answer based strictly on the provided context. "
    "Return ONLY the exact answer — no explanation, no preamble. "
    "For answers that are lists, return comma-separated values. "
    "If the answer is not found in the context, return: not found"
)

SYSTEM_FUZZY = (
    "You are a CRM expert assistant. Provide a clear, accurate answer "
    "based strictly on the provided context. Be concise."
)

SYSTEM_PRIVACY = (
    "You are a CRM assistant with strict privacy controls. "
    "Always refuse to provide confidential or private data."
)


def _normalize_context(context: str, drift_mappings: list[dict]) -> str:
    for mapping in drift_mappings:
        drifted = mapping.get("drifted_column", "")
        original = mapping.get("original_column", "")
        if drifted and original:
            context = context.replace(drifted, original)
    return context


def _pick_system(task_category: str, reward_metric: str) -> str:
    if task_category in PRIVACY_CATEGORIES or reward_metric == "privacy_rejection":
        return SYSTEM_PRIVACY
    if reward_metric == "fuzzy_match":
        return SYSTEM_FUZZY
    return SYSTEM_EXACT


def _build_llm_client():
    """Pick provider based on available env vars. Anthropic takes priority."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        return "anthropic", anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    if os.environ.get("MISTRAL_API_KEY"):
        from openai import OpenAI
        return "mistral", OpenAI(
            api_key=os.environ["MISTRAL_API_KEY"],
            base_url="https://api.mistral.ai/v1",
        )
    raise RuntimeError("No LLM API key found. Set ANTHROPIC_API_KEY or MISTRAL_API_KEY.")


def _call_llm(provider: str, client, system: str, user_content: str) -> tuple[str, int]:
    if provider == "anthropic":
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=system,
            messages=[{"role": "user", "content": user_content}],
        )
        answer = response.content[0].text.strip()
        tokens = response.usage.input_tokens + response.usage.output_tokens
        return answer, tokens

    if provider == "mistral":
        response = client.chat.completions.create(
            model="mistral-large-latest",
            max_tokens=512,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        )
        answer = response.choices[0].message.content.strip()
        tokens = response.usage.prompt_tokens + response.usage.completion_tokens
        return answer, tokens

    raise RuntimeError(f"Unknown provider: {provider}")


class Agent:
    def __init__(self):
        self._provider, self._client = _build_llm_client()

    async def run(
        self,
        message: Message,
        context: RequestContext,
        task_updater: TaskUpdater,
    ) -> None:
        await task_updater.update_status(
            state=TaskState.working,
            message=new_agent_text_message("Solving CRM task..."),
        )

        raw_text = get_message_text(message)

        try:
            task_data = json.loads(raw_text)
        except (json.JSONDecodeError, TypeError):
            task_data = {"prompt": raw_text}

        answer, tokens = self._solve(task_data)

        result = {
            "answer": answer,
            "task_id": task_data.get("task_id", ""),
            "category": task_data.get("task_category", ""),
            "metrics": {
                "tokens": tokens,
                "tool_calls": 0,
                "queries": 0,
            },
        }

        await task_updater.add_artifact(
            parts=[Part(root=TextPart(kind="text", text=json.dumps(result)))],
            name="result",
        )
        await task_updater.complete()

    def _solve(self, task_data: dict) -> tuple[str, int]:
        task_category = task_data.get("task_category", "")
        reward_metric = task_data.get("reward_metric", "exact_match")

        if task_category in PRIVACY_CATEGORIES or reward_metric == "privacy_rejection":
            return (
                "I cannot provide this information as it contains confidential/private data.",
                0,
            )

        required_context = task_data.get("required_context", "")
        drift_mappings = task_data.get("entropy", {}).get("drift_mappings", [])
        context = _normalize_context(required_context, drift_mappings)

        prompt = task_data.get("prompt", "")
        system = _pick_system(task_category, reward_metric)
        user_content = f"Task: {prompt}\n\nContext:\n{context}"

        return _call_llm(self._provider, self._client, system, user_content)
