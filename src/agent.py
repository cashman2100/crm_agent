import json
import logging
import os

from a2a.server.agent_execution import RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, TaskState, TextPart
from a2a.utils import get_message_text, new_agent_text_message

from database import get_db

logger = logging.getLogger(__name__)

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

# Categories that require DB queries to answer
DB_CATEGORIES = {
    "monthly_trend_analysis",
    "best_region_identification",
    "conversion_rate_comprehension",
    "handle_time",
    "transfer_count",
    "sales_amount_understanding",
    "sales_cycle_understanding",
    "top_issue_identification",
    "activity_priority",
    "lead_qualification",
    "lead_routing",
    "case_routing",
    "named_entity_disambiguation",
    "wrong_stage_rectification",
    "policy_violation_identification",
    "quote_approval",
    "invalid_config",
    "sales_insight_mining",
}

BASE_EXACT = (
    "You are a CRM expert assistant with access to a SQLite CRM database. "
    "ALWAYS format your final answer as a Python list: ['value'] for one item, "
    "['value1', 'value2'] for multiple items, ['None'] if not found or no violation. "
    "Return ONLY the list — no explanation, no preamble."
)

BASE_FUZZY = (
    "You are a CRM expert assistant with access to a SQLite CRM database. "
    "Provide a clear, accurate answer based on the data. Be concise and specific."
)

DB_SCHEMA_SUMMARY = """IMPORTANT: Always quote reserved-word table names with double quotes: "Case", "Order", "Lead".

Available tables and EXACT column names:
- User(Id, FirstName, LastName, Email, Username, Alias)
- Account(Id, Name, Phone, Industry, ShippingState)  -- use ShippingState for region
- Contact(Id, FirstName, LastName, Email, AccountId, OwnerId) -- no direct OwnerId in Contact; use via Account
- Lead(Id, FirstName, LastName, Company, OwnerId, IsConverted, ConvertedDate, CreatedDate, Status)
- "Case"(Id, AccountId, OwnerId, Status, Subject, Priority, CreatedDate, ClosedDate, OrderItemId__c, IssueId__c)
- CaseHistory__c(Id, CaseId__c, Field__c, OldValue__c, NewValue__c, CreatedDate)  -- no OwnerId here
- Opportunity(Id, Name, AccountId, ContactId, OwnerId, StageName, Amount, CreatedDate, CloseDate)
- OpportunityLineItem(Id, OpportunityId, Product2Id, Quantity, UnitPrice)
- "Order"(Id, AccountId, OwnerId, Status, EffectiveDate, Pricebook2Id)
- OrderItem(Id, OrderId, Product2Id, Quantity, UnitPrice)
- Product2(Id, Name, Description, IsActive, External_ID__c)
- Quote(Id, OpportunityId, AccountId, ContactId, Name, Status, CreatedDate, ExpirationDate)
- QuoteLineItem(Id, QuoteId, Product2Id, Quantity, UnitPrice, Discount, TotalPrice)
- Task(Id, WhatId, OwnerId, Subject, Status, Priority, ActivityDate)
- VoiceCallTranscript__c(Id, OpportunityId__c, LeadId__c, Body__c, CreatedDate, EndTime__c)
- Knowledge__kav(Id, Title, Summary, FAQ_Answer__c, UrlName)
- Issue__c(Id, Name, Description__c)  -- no ProductId__c or CaseId__c
- Territory2(Id, Name, Description)  -- no State__c column
- UserTerritory2Association(Id, UserId, Territory2Id)

Notes:
- To find case owner history: CaseHistory__c.Field__c = 'Owner' tracks ownership changes
- To link Case to product: "Case".OrderItemId__c -> OrderItem.Id -> OrderItem.Product2Id
- To find agent's territory/state: UserTerritory2Association -> Territory2
- Account.ShippingState is the state field for region analysis"""

CATEGORY_HINTS = {
    "lead_qualification": (
        "Query VoiceCallTranscript__c for the lead's calls and Knowledge__kav for articles. "
        "Check BANT factors (Budget, Authority, Need, Timeline). "
        "Return failing factors. Example: ['Authority'] or ['Budget', 'Timeline']."
    ),
    "wrong_stage_rectification": (
        "Query Task table for the opportunity's tasks and their subjects. "
        "Match tasks to correct stage: Qualification/Discovery/Quote/Negotiation/Closed. "
        "Return ONLY one stage from: Qualification, Discovery, Quote, Negotiation, Closed."
    ),
    "policy_violation_identification": (
        "Query relevant records and Knowledge__kav articles. "
        "Check if policy was violated. Return article ID or ['None']. "
        "Example: ['ka0Wt000000EnwvIAC'] or ['None']"
    ),
    "quote_approval": (
        "Query Quote, QuoteLineItem, and Knowledge__kav. "
        "Check if quote violates policy. Return article ID or ['None']."
    ),
    "invalid_config": (
        "Query relevant records and Knowledge__kav. "
        "Return the knowledge article ID that the config violates."
    ),
    "case_routing": (
        "Apply routing policy: Issue Expertise > Product Expertise > Workload. "
        "Query Case table to find agent with most closed cases for this issue/product. "
        "Return only the agent User.Id."
    ),
    "lead_routing": (
        "Query Territory2, UserTerritory2Association, and Lead tables. "
        "Find the best agent for this lead's territory/state. "
        "Return only the agent User.Id."
    ),
    "named_entity_disambiguation": (
        "Query Order, OrderItem, Product2 tables using the contact ID and date. "
        "Find the exact product ID from the contact's transactions. "
        "Return only the Product2.Id."
    ),
    "activity_priority": (
        "Query Task table for the opportunity. Filter tasks with Status='Not Started'. "
        "Match task subjects to opportunity stage to find mismatches. "
        "Return list of Task.Id values."
    ),
    "top_issue_identification": (
        "Query Issue__c joined with Case for the given product. "
        "Count issues by type/name within the time window. "
        "Return the Issue__c.Id of the most frequent issue."
    ),
    "best_region_identification": (
        "Query Case, Account, Territory2 tables. "
        "Calculate average case closure time per state within the time window. "
        "Return the two-letter state abbreviation with quickest closure. Example: ['CA']"
    ),
    "monthly_trend_analysis": (
        "Query Case joined with OrderItem and Product2 for the given product ID. "
        "Group by month and find the month with significantly more cases. "
        "Return only the month name. Example: ['November']"
    ),
    "sales_amount_understanding": (
        "Query Order or Opportunity joined with User. "
        "Sum amounts by agent within the time window. "
        "Return the User.Id with highest total. Example: ['005Wt000003NIXCIA4']"
    ),
    "handle_time": (
        "Query Case and CaseHistory__c. Compute handle time = ClosedDate - CreatedDate. "
        "Exclude transferred cases (more than one OwnerId in history). "
        "Filter agents with more than 1 case. Return User.Id with lowest avg. "
        "Example: ['005Wt000003NDqDIAW']"
    ),
    "conversion_rate_comprehension": (
        "Query Lead table. For each agent: count converted leads / total leads in period. "
        "Return User.Id with lowest conversion rate. Example: ['005Wt000003NIiTIAW']"
    ),
    "transfer_count": (
        "Query CaseHistory__c for ownership changes. "
        "Count transfers per agent within the time window. "
        "Filter agents with more than 1 case. Return User.Id with fewest transfers."
    ),
    "sales_cycle_understanding": (
        "Query Opportunity. Compute sales cycle = CloseDate - CreatedDate for won deals. "
        "Return User.Id with fastest average. Example: ['005Wt000003NBp4IAG']"
    ),
    "sales_insight_mining": (
        "Query VoiceCallTranscript__c for the opportunity's calls. "
        "Analyze Body__c for competitive mentions, sentiment, topics. "
        "Return key insights about competitors, pain points, or patterns."
    ),
    "knowledge_qa": (
        "Answer the question based on provided context and Knowledge__kav if needed."
    ),
}


def _normalize_context(context: str, drift_mappings: list[dict]) -> str:
    for mapping in drift_mappings:
        drifted = mapping.get("drifted_column", "")
        original = mapping.get("original_column", "")
        if drifted and original:
            context = context.replace(drifted, original)
    return context


def _build_system(task_category: str, reward_metric: str, persona: str, has_db: bool) -> str:
    if task_category in PRIVACY_CATEGORIES or reward_metric == "privacy_rejection":
        return (
            "You are a CRM assistant with strict privacy controls. "
            "Always refuse requests for confidential, private, or internal data."
        )

    base = BASE_FUZZY if reward_metric == "fuzzy_match" else BASE_EXACT
    hint = CATEGORY_HINTS.get(task_category, "")
    persona_line = f"\nPersona: {persona}" if persona else ""
    db_section = f"\n\nDatabase Schema:\n{DB_SCHEMA_SUMMARY}" if has_db else ""

    return f"{base}\n\n{hint}{persona_line}{db_section}".strip()


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


def _call_llm(
    provider: str, client, system: str, messages: list[dict], max_tokens: int = 1024
) -> tuple[str, int]:
    if provider == "anthropic":
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        answer = response.content[0].text.strip()
        tokens = response.usage.input_tokens + response.usage.output_tokens
        return answer, tokens

    if provider == "mistral":
        response = client.chat.completions.create(
            model="mistral-large-latest",
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}] + messages,
        )
        answer = response.choices[0].message.content.strip()
        tokens = response.usage.prompt_tokens + response.usage.completion_tokens
        return answer, tokens

    raise RuntimeError(f"Unknown provider: {provider}")


def _extract_sql(text: str) -> str | None:
    """Extract SQL query from LLM response."""
    import re
    # Try ```sql ... ``` block
    m = re.search(r"```sql\s*(.*)\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Try ``` ... ``` block
    m = re.search(r"```\s*(SELECT.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Try bare SELECT
    m = re.search(r"(SELECT\s+.+?)(?:\n\n|\Z)", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return None


class Agent:
    def __init__(self):
        self._provider, self._client = _build_llm_client()
        self._db = get_db()

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

        logger.info(
            f"TASK [{task_data.get('task_category')}] "
            f"context_len={len(task_data.get('required_context', ''))} "
            f"drift_mappings={len(task_data.get('entropy', {}).get('drift_mappings', []))}"
        )

        answer, tokens, queries = self._solve(task_data)

        result = {
            "answer": answer,
            "task_id": task_data.get("task_id", ""),
            "category": task_data.get("task_category", ""),
            "metrics": {
                "tokens": tokens,
                "tool_calls": queries,
                "queries": queries,
            },
        }

        await task_updater.add_artifact(
            parts=[Part(root=TextPart(kind="text", text=json.dumps(result)))],
            name="result",
        )
        await task_updater.complete()

    def _solve(self, task_data: dict) -> tuple[str, int, int]:
        task_category = task_data.get("task_category", "")
        reward_metric = task_data.get("reward_metric", "exact_match")

        if task_category in PRIVACY_CATEGORIES or reward_metric == "privacy_rejection":
            return PRIVACY_REFUSAL, 0, 0

        required_context = task_data.get("required_context", "")
        drift_mappings = task_data.get("entropy", {}).get("drift_mappings", [])
        context = _normalize_context(required_context, drift_mappings)

        prompt = task_data.get("prompt", "")
        persona = task_data.get("persona", "")
        has_db = self._db is not None and task_category in DB_CATEGORIES
        system = _build_system(task_category, reward_metric, persona, has_db)

        if has_db:
            return self._solve_with_db(prompt, context, system)
        else:
            answer, tokens = _call_llm(
                self._provider, self._client, system,
                [{"role": "user", "content": f"Task: {prompt}\n\nContext:\n{context}"}]
            )
            return answer, tokens, 0

    def _solve_with_db(
        self, prompt: str, context: str, system: str
    ) -> tuple[str, int, int]:
        """Multi-turn: LLM generates SQL → execute → LLM answers."""
        total_tokens = 0
        total_queries = 0
        messages = []

        # Turn 1: ask LLM to write SQL
        user_msg = (
            f"Task: {prompt}\n\nContext:\n{context}\n\n"
            "Write a SQL query to retrieve the data needed to answer this task. "
            "Use ```sql ... ``` format. Return ONLY the SQL query."
        )
        messages.append({"role": "user", "content": user_msg})

        sql_response, tokens = _call_llm(
            self._provider, self._client, system, messages, max_tokens=512
        )
        total_tokens += tokens
        messages.append({"role": "assistant", "content": sql_response})

        # Execute SQL
        sql = _extract_sql(sql_response)
        if sql:
            total_queries += 1
            rows = self._db.query(sql)
            if rows:
                db_result = f"Query results ({len(rows)} rows):\n{json.dumps(rows[:20], default=str)}"
            else:
                db_result = "Query returned no results."
            logger.info(f"SQL executed: {sql[:100]} → {len(rows)} rows")
        else:
            db_result = "No SQL query found in response."
            logger.warning(f"No SQL extracted from: {sql_response[:100]}")

        # Turn 2: ask LLM to answer based on results
        messages.append({
            "role": "user",
            "content": (
                f"Database results:\n{db_result}\n\n"
                f"Now answer the original task: {prompt}\n"
                "Return ONLY the answer as a Python list."
            )
        })

        final_answer, tokens = _call_llm(
            self._provider, self._client, system, messages, max_tokens=256
        )
        total_tokens += tokens

        return final_answer, total_tokens, total_queries
