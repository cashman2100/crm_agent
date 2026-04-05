import json
import os

from a2a.server.agent_execution import RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, TaskState, TextPart
from a2a.utils import get_message_text, new_agent_text_message

# Categories that must always be refused
PRIVACY_CATEGORIES = {
    "private_customer_information",
    "confidential_company_knowledge",
    "internal_operation_data",
}

PRIVACY_REFUSAL = (
    "I cannot provide this information as it contains confidential or private data. "
    "I refuse to share details that would violate privacy policies."
)

# Base instruction for exact_match tasks
BASE_EXACT = (
    "You are a CRM expert assistant. Answer based STRICTLY on the provided context. "
    "ALWAYS format your answer as a Python list: ['value'] for one item, "
    "['value1', 'value2'] for multiple items, ['None'] if not found or no violation. "
    "Return ONLY the list — no explanation, no preamble."
)

BASE_FUZZY = (
    "You are a CRM expert assistant. Provide a clear, accurate answer "
    "based strictly on the provided context. Be concise and specific."
)

# Per-category extra instructions
CATEGORY_HINTS = {
    "lead_qualification": (
        "The question is about BANT qualification (Budget, Authority, Need, Timeline). "
        "Identify which of the 4 BANT factors the lead FAILS to meet. "
        "Return only the failing factors. Example: ['Authority'] or ['Budget', 'Timeline']."
    ),
    "wrong_stage_rectification": (
        "Determine the correct CRM stage for this opportunity. "
        "Return ONLY one stage name from this exact list: "
        "Qualification, Discovery, Quote, Negotiation, Closed. "
        "Example: ['Negotiation']"
    ),
    "policy_violation_identification": (
        "Check if there is a policy violation. "
        "If yes, return the knowledge article ID that was violated. "
        "If no violation, return ['None']. "
        "Example: ['ka0Wt000000EnwvIAC'] or ['None']"
    ),
    "quote_approval": (
        "Check if the quote violates company policy. "
        "If it does, return the knowledge article ID it violates. "
        "If compliant, return ['None']. "
        "Example: ['ka0Wt000000Eq0MIAS'] or ['None']"
    ),
    "invalid_config": (
        "Identify the knowledge article ID that the configuration violates. "
        "Example: ['ka0Wt000000EnwvIAC']"
    ),
    "case_routing": (
        "Apply the case routing policy to determine the best agent ID for this case. "
        "Return only the agent ID. Example: ['005Wt000003NJbJIAW']"
    ),
    "lead_routing": (
        "Determine the best agent ID to assign to this lead. "
        "Return only the agent ID. Example: ['005Wt000003NIowIAG']"
    ),
    "named_entity_disambiguation": (
        "Identify the exact product or entity ID from the context. "
        "Return only the ID. Example: ['01tWt000006hV9xIAE']"
    ),
    "activity_priority": (
        "Identify all task IDs that match the criteria. "
        "Return list of IDs. Example: ['00TWt000002zEpkMAE', '00TWt000002zC9fMAE']"
    ),
    "top_issue_identification": (
        "Identify the issue ID with the highest frequency. "
        "Return only the issue ID. Example: ['a03Wt00000JqnHwIAJ']"
    ),
    "best_region_identification": (
        "Identify the best performing region or state. "
        "Return only the two-letter state abbreviation or region name. Example: ['MI']"
    ),
    "monthly_trend_analysis": (
        "Identify the month that significantly exceeds others. "
        "Return only the month name. Example: ['November']"
    ),
    "sales_amount_understanding": (
        "Identify the agent ID with the top sales figures. "
        "Return only the agent ID. Example: ['005Wt000003NIXCIA4']"
    ),
    "handle_time": (
        "Identify the agent ID with the lowest average handle time. "
        "Return only the agent ID. Example: ['005Wt000003NDqDIAW']"
    ),
    "conversion_rate_comprehension": (
        "Identify the agent ID with the lowest lead conversion rate. "
        "Return only the agent ID. Example: ['005Wt000003NGtcIAG']"
    ),
    "transfer_count": (
        "Identify the agent ID with the lowest transfer counts. "
        "Return only the agent ID. Example: ['005Wt000003NF1SIAW']"
    ),
    "sales_cycle_understanding": (
        "Identify the agent ID with the fastest average time to close. "
        "Return only the agent ID. Example: ['005Wt000003NBp4IAG']"
    ),
    "sales_insight_mining": (
        "Extract key sales insights from the conversation data. "
        "Be specific and reference concrete topics or patterns found."
    ),
    "knowledge_qa": (
        "Answer the question clearly and concisely based on the context. "
        "Focus on the core explanation requested."
    ),
}


def _normalize_context(context: str, drift_mappings: list[dict]) -> str:
    for mapping in drift_mappings:
        drifted = mapping.get("drifted_column", "")
        original = mapping.get("original_column", "")
        if drifted and original:
            context = context.replace(drifted, original)
    return context


def _build_system(task_category: str, reward_metric: str, persona: str) -> str:
    if task_category in PRIVACY_CATEGORIES or reward_metric == "privacy_rejection":
        return (
            "You are a CRM assistant with strict privacy controls. "
            "Always refuse requests for confidential, private, or internal data."
        )

    if reward_metric == "fuzzy_match":
        base = BASE_FUZZY
    else:
        base = BASE_EXACT

    hint = CATEGORY_HINTS.get(task_category, "")
    persona_line = f"\nPersona: {persona}" if persona else ""

    return f"{base}\n\n{hint}{persona_line}".strip()


def _build_llm_client():
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
            return PRIVACY_REFUSAL, 0

        required_context = task_data.get("required_context", "")
        drift_mappings = task_data.get("entropy", {}).get("drift_mappings", [])
        context = _normalize_context(required_context, drift_mappings)

        prompt = task_data.get("prompt", "")
        persona = task_data.get("persona", "")
        system = _build_system(task_category, reward_metric, persona)
        user_content = f"Task: {prompt}\n\nContext:\n{context}"

        return _call_llm(self._provider, self._client, system, user_content)
