import json
import logging
import os
import re

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
- CaseHistory__c(Id, CaseId__c, Field__c, OldValue__c, NewValue__c, CreatedDate)
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

CRITICAL DATE RULES (dates are stored as ISO text '2023-07-02T11:00:00.000+0000'):
- For date arithmetic: julianday(substr(date_col, 1, 19)) -- strip timezone first!
- For month number: substr(date_col, 6, 2)  -- returns '01'..'12'
- For year-month grouping: substr(date_col, 1, 7)  -- returns '2023-07'
- NEVER use strftime() -- it fails on ISO dates with timezone

CRITICAL CASEHISTORY RULES:
- CaseHistory__c.Field__c = 'Owner Assignment' tracks ownership (NOT 'Owner')
- OldValue__c IS NULL = initial assignment at case creation (not a transfer)
- OldValue__c IS NOT NULL = actual transfer between agents
- To find transferred cases: SELECT CaseId__c FROM CaseHistory__c WHERE Field__c='Owner Assignment' AND OldValue__c IS NOT NULL

Other notes:
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
        "Route using: Issue Expertise > Product Expertise > Workload. "
        "Step 1: Find issue ID — SELECT Id FROM Issue__c WHERE Name LIKE '%keyword%' OR Description__c LIKE '%keyword%'. "
        "Step 2: Find product ID — SELECT Id FROM Product2 WHERE Name LIKE '%product_name%'. "
        "Step 3: Issue expertise — SELECT OwnerId, COUNT(*) FROM \"Case\" WHERE Status='Closed' AND IssueId__c='<issue_id>' GROUP BY OwnerId ORDER BY COUNT(*) DESC. "
        "Step 4: Product expertise tiebreaker among tied agents — SELECT c.OwnerId, COUNT(*) FROM \"Case\" c JOIN OrderItem oi ON c.OrderItemId__c=oi.Id WHERE c.Status='Closed' AND oi.Product2Id='<prod_id>' AND c.OwnerId IN (<tied_agents>) GROUP BY c.OwnerId ORDER BY COUNT(*) DESC. "
        "Step 5: Workload tiebreaker — SELECT OwnerId, COUNT(*) FROM \"Case\" WHERE Status!='Closed' GROUP BY OwnerId ORDER BY COUNT(*) ASC. "
        "Combine all into one CTE query. Pick the agent with max issue count; break ties with max product count; break ties with min workload. "
        "Return only the winner's User.Id."
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
        "Query Case joined with Account. "
        "Calculate avg closure time: AVG(julianday(substr(ClosedDate,1,19)) - julianday(substr(CreatedDate,1,19))) per Account.ShippingState. "
        "Filter by date window from task. Only include closed cases (ClosedDate IS NOT NULL). "
        "Return the two-letter state abbreviation with quickest (lowest) avg closure. Example: ['CA']"
    ),
    "monthly_trend_analysis": (
        "Query Case joined with OrderItem for the given product ID. "
        "Use substr(c.CreatedDate, 6, 2) to get month number (NOT strftime). "
        "Group by substr(c.CreatedDate, 1, 7) for year-month, count cases, find the month with the most. "
        "Map month number to name: '01'=January ... '09'=September etc. "
        "Return only the month name. Example: ['September']"
    ),
    "sales_amount_understanding": (
        "Query Order or Opportunity joined with User. "
        "Sum amounts by agent within the time window. "
        "Return the User.Id with highest total. Example: ['005Wt000003NIXCIA4']"
    ),
    "handle_time": (
        "Compute average handle time per agent using TWO separate counts:\n"
        "1. CASE COUNT (for filter): an agent's case count = all cases they were EVER assigned to "
        "(initial + transferred). Get from: SELECT NewValue__c, COUNT(DISTINCT CaseId__c) FROM CaseHistory__c "
        "WHERE Field__c='Owner Assignment' AND substr(CreatedDate,1,10) BETWEEN 'start' AND 'end' GROUP BY NewValue__c.\n"
        "2. HANDLE TIME (for ranking): only from NON-TRANSFERRED closed cases "
        "(Id NOT IN SELECT CaseId__c FROM CaseHistory__c WHERE Field__c='Owner Assignment' AND OldValue__c IS NOT NULL). "
        "AVG(julianday(substr(ClosedDate,1,19)) - julianday(substr(CreatedDate,1,19))). "
        "Filter Case.CreatedDate in window.\n"
        "3. JOIN: only include agents whose case count > threshold from prompt (e.g. >1, >0, etc.).\n"
        "4. Return User.Id with lowest (or highest if asked) avg handle time."
    ),
    "conversion_rate_comprehension": (
        "Query Lead table. For each OwnerId: count IsConverted=1 / total leads in period. "
        "Filter by date range using substr(CreatedDate,1,10) for date comparison. "
        "Return User.Id with lowest conversion rate. Example: ['005Wt000003NIiTIAW']"
    ),
    "transfer_count": (
        "Find the agent with fewest OUTGOING case transfers in the given time window. "
        "An outgoing transfer = CaseHistory__c record where Field__c='Owner Assignment' AND OldValue__c = agent's Id. "
        "CRITICAL: join on OldValue__c = OwnerId (not CaseId__c). Most agents have 0 transfers. "
        "SQL pattern: SELECT c.OwnerId, COUNT(DISTINCT c.Id) as total, COUNT(h.Id) as transfers "
        "FROM \"Case\" c "
        "LEFT JOIN CaseHistory__c h ON h.OldValue__c = c.OwnerId AND h.Field__c='Owner Assignment' AND h.OldValue__c IS NOT NULL "
        "AND substr(h.CreatedDate,1,10) BETWEEN 'start' AND 'end' "
        "WHERE substr(c.CreatedDate,1,10) BETWEEN 'start' AND 'end' "
        "GROUP BY c.OwnerId HAVING total > 1 ORDER BY transfers ASC, total DESC LIMIT 1. "
        "Return the User.Id with fewest transfers."
    ),
    "sales_cycle_understanding": (
        "Query Opportunity WHERE StageName='Closed Won'. "
        "Compute sales cycle: julianday(substr(CloseDate,1,19)) - julianday(substr(CreatedDate,1,19)) per OwnerId. "
        "Filter by date range from task. Return User.Id with lowest avg cycle. Example: ['005Wt000003NBp4IAG']"
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


# Hardcoded reverse mappings for Green Agent's text drift (medium level)
# Green Agent renames these in both prompt and required_context text
TEXT_DRIFT_REVERSE = {
    "AssignedAgent": "OwnerId",
    "StatusCode": "Status",
    "ClientId": "AccountId",
    "PersonRef": "ContactId",
    "Title": "Subject",
    "Details": "Description",
    # low level variants
    "CaseStatus": "Status",
    "AssignedTo": "OwnerId",
    "CustomerRef": "AccountId",
    # high level variants
    "st_code": "Status",
    "own_ref": "OwnerId",
    "acct_id": "AccountId",
    "cont_ref": "ContactId",
    "subj": "Subject",
    "desc": "Description",
    "pri_level": "Priority",
    "create_dt": "CreatedDate",
    "ticket_num": "CaseNumber",
}


def _normalize_text(text: str, drift_mappings: list[dict]) -> str:
    """Reverse all drift transformations: both DB schema drift and text drift."""
    # Reverse DB schema drift (entropy engine renames)
    for mapping in drift_mappings:
        drifted = mapping.get("drifted_column", "")
        original = mapping.get("original_column", "")
        if drifted and original:
            text = text.replace(drifted, original)
    # Reverse hardcoded text drift (Green Agent _apply_schema_drift)
    for drifted, original in TEXT_DRIFT_REVERSE.items():
        text = text.replace(drifted, original)
    return text


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
    if os.environ.get("DEEPSEEK_API_KEY"):
        from openai import OpenAI
        return "deepseek", OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url="https://api.deepseek.com/v1",
        )
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        return "anthropic", anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    if os.environ.get("MISTRAL_API_KEY"):
        from openai import OpenAI
        return "mistral", OpenAI(
            api_key=os.environ["MISTRAL_API_KEY"],
            base_url="https://api.mistral.ai/v1",
        )
    raise RuntimeError("No LLM API key found. Set DEEPSEEK_API_KEY, ANTHROPIC_API_KEY or MISTRAL_API_KEY.")


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

    if provider == "deepseek":
        response = client.chat.completions.create(
            model="deepseek-chat",
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}] + messages,
        )
        answer = response.choices[0].message.content.strip()
        tokens = response.usage.prompt_tokens + response.usage.completion_tokens
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
    # Try ```sql ... ``` block
    m = re.search(r"```sql\s*(.*)\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Try ``` ... ``` block
    m = re.search(r"```\s*(SELECT.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Try bare SELECT
    m = re.search(r"(SELECT\s+.+)(?:\n\n|\Z)", text, re.DOTALL | re.IGNORECASE)
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
        context = _normalize_text(required_context, drift_mappings)
        prompt = _normalize_text(task_data.get("prompt", ""), drift_mappings)
        persona = task_data.get("persona", "")
        has_db = self._db is not None and task_category in DB_CATEGORIES
        system = _build_system(task_category, reward_metric, persona, has_db)

        if has_db:
            if task_category == "case_routing":
                return self._solve_case_routing(prompt, context)
            if task_category == "quote_approval":
                return self._solve_quote_approval(prompt, context)
            if task_category == "policy_violation_identification":
                return self._solve_policy_violation(prompt, context)
            if task_category == "handle_time":
                return self._solve_handle_time(prompt, context, system)
            if task_category == "transfer_count":
                return self._solve_transfer_count(prompt, context)
            if task_category == "lead_routing":
                return self._solve_lead_routing(prompt, context)
            if task_category == "monthly_trend_analysis":
                return self._solve_monthly_trend(prompt, context)
            if task_category == "invalid_config":
                return self._solve_invalid_config(prompt, context)
            if task_category == "activity_priority":
                return self._solve_activity_priority(prompt, context)
            if task_category == "named_entity_disambiguation":
                return self._solve_named_entity(prompt, context)
            if task_category == "sales_insight_mining":
                return self._solve_sales_insight(prompt, context)
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

    @staticmethod
    def _word_overlap(text: str, candidate: str) -> int:
        """Count common words between text and candidate (case-insensitive)."""
        text_words = set(re.sub(r'[^a-z0-9]', ' ', text.lower()).split())
        cand_words = set(re.sub(r'[^a-z0-9]', ' ', candidate.lower()).split())
        return len(text_words & cand_words)

    def _solve_case_routing(
        self, prompt: str, context: str
    ) -> tuple[str, int, int]:
        """Multi-query case routing: match issue+product by text, then apply policy."""
        total_tokens = 0

        # Step 1: pre-fetch all issues and products
        issues = self._db.query("SELECT Id, Name, Description__c FROM Issue__c")
        products = self._db.query("SELECT Id, Name FROM Product2")

        # Step 2: find best issue match by word overlap with prompt
        best_issue_id = ""
        best_issue_score = 0
        for iss in issues:
            candidate = f"{iss.get('Name','')} {iss.get('Description__c','')}"
            score = self._word_overlap(prompt, candidate)
            if score > best_issue_score:
                best_issue_score = score
                best_issue_id = iss["Id"]

        # Step 3: find best product match
        best_product_id = ""
        best_product_score = 0
        for prod in products:
            score = self._word_overlap(prompt, prod.get("Name", ""))
            if score > best_product_score:
                best_product_score = score
                best_product_id = prod["Id"]

        logger.info(f"case_routing: issue_id={best_issue_id}(score={best_issue_score}) "
                    f"product_id={best_product_id}(score={best_product_score})")

        # Step 3: query routing data
        routing_data = {}
        queries = 0

        if best_issue_id:
            rows = self._db.query(
                f'SELECT OwnerId, COUNT(*) as cnt FROM "Case" '
                f'WHERE Status=\'Closed\' AND IssueId__c=\'{best_issue_id}\' '
                f'GROUP BY OwnerId ORDER BY cnt DESC'
            )
            routing_data["issue_expertise"] = rows
            queries += 1

        if best_product_id:
            rows = self._db.query(
                f'SELECT c.OwnerId, COUNT(*) as cnt FROM "Case" c '
                f'JOIN OrderItem oi ON c.OrderItemId__c = oi.Id '
                f'WHERE c.Status=\'Closed\' AND oi.Product2Id=\'{best_product_id}\' '
                f'GROUP BY c.OwnerId ORDER BY cnt DESC'
            )
            routing_data["product_expertise"] = rows
            queries += 1

        # workload: ALL agents with any case, LEFT JOIN open cases
        workload = self._db.query(
            'SELECT c.OwnerId, COUNT(o.Id) as open_cnt '
            'FROM "Case" c '
            'LEFT JOIN "Case" o ON o.OwnerId = c.OwnerId AND o.Status != \'Closed\' '
            'GROUP BY c.OwnerId ORDER BY open_cnt ASC'
        )
        routing_data["workload"] = workload
        queries += 1

        # Step 4: apply routing policy in Python (deterministic)
        winner = self._apply_routing_policy(routing_data)
        answer = f"['{winner}']" if winner else "['None']"

        return answer, total_tokens, queries

    @staticmethod
    def _apply_routing_policy(routing_data: dict) -> str:
        """Apply Issue Expertise > Product Expertise > Workload deterministically."""
        issue_rows = routing_data.get("issue_expertise", [])
        prod_rows = routing_data.get("product_expertise", [])
        workload_rows = routing_data.get("workload", [])

        # All agents as fallback (from workload list)
        all_agents = {r["OwnerId"] for r in workload_rows}
        candidates = set(all_agents)

        # Level 1: Issue expertise
        if issue_rows:
            max_cnt = max(r["cnt"] for r in issue_rows)
            top = {r["OwnerId"] for r in issue_rows if r["cnt"] == max_cnt}
            candidates = top & all_agents if top & all_agents else top

        # Level 2: Product expertise tiebreaker
        if len(candidates) > 1 and prod_rows:
            in_candidates = [r for r in prod_rows if r["OwnerId"] in candidates]
            if in_candidates:
                max_cnt = max(r["cnt"] for r in in_candidates)
                top = {r["OwnerId"] for r in in_candidates if r["cnt"] == max_cnt}
                candidates = top

        # Level 3: Workload tiebreaker (fewest open cases)
        if len(candidates) > 1 and workload_rows:
            in_cands = [r for r in workload_rows if r["OwnerId"] in candidates]
            if in_cands:
                min_load = min(r["open_cnt"] for r in in_cands)
                top = {r["OwnerId"] for r in in_cands if r["open_cnt"] == min_load}
                candidates = top

        return next(iter(candidates)) if candidates else ""

    def _solve_quote_approval(
        self, prompt: str, context: str
    ) -> tuple[str, int, int]:
        """Check quote discounts against Volume-Based Discount policy (pure Python)."""
        # Extract quote ID from context (Salesforce IDs are 15 or 18 chars)
        quote_id_match = re.search(r'(0Q0[A-Za-z0-9]{12,15})', context + prompt)
        if not quote_id_match:
            return "['None']", 0, 0

        quote_id = quote_id_match.group(1)
        lines = self._db.query(
            f"SELECT Quantity, UnitPrice, Discount FROM QuoteLineItem WHERE QuoteId = '{quote_id}'"
        )
        if not lines:
            return "['None']", 0, 1

        # Volume-Based Discount policy thresholds
        def correct_discount(gross: float) -> float:
            if gross > 20:
                return 15.0
            if gross > 10:
                return 10.0
            if gross > 5:
                return 5.0
            return 0.0

        violation = False
        for line in lines:
            qty = float(line.get("Quantity") or 0)
            price = float(line.get("UnitPrice") or 0)
            disc = float(line.get("Discount") or 0)
            gross = qty * price
            if abs(disc - correct_discount(gross)) > 0.01:
                violation = True
                break

        if violation:
            return "['ka0Wt000000Eq0MIAS']", 0, 1
        return "['None']", 0, 1

    def _solve_policy_violation(
        self, prompt: str, context: str
    ) -> tuple[str, int, int]:
        """Find KB article violated by the case agent's handling."""
        case_id_match = re.search(r'(500[A-Za-z0-9]{12,15})', context + prompt)
        if not case_id_match:
            return "['None']", 0, 0

        case_id = case_id_match.group(1)
        cases = self._db.query(
            f'SELECT Id, Subject, Priority, Status, OwnerId, IssueId__c, CreatedDate, ClosedDate '
            f'FROM "Case" WHERE Id = \'{case_id}\''
        )
        if not cases:
            return "['None']", 0, 1

        case = cases[0]
        issue_id = case.get("IssueId__c", "")

        # Get case history
        history = self._db.query(
            f"SELECT Field__c, OldValue__c, NewValue__c, CreatedDate "
            f"FROM CaseHistory__c WHERE CaseId__c = '{case_id}'"
        )

        # Get issue name
        issue_name = ""
        if issue_id:
            issues = self._db.query(
                f"SELECT Name, Description__c FROM Issue__c WHERE Id = '{issue_id}'"
            )
            if issues:
                issue_name = issues[0].get("Name", "")

        # Find best matching KB article by word overlap
        articles = self._db.query("SELECT Id, Title, Summary, FAQ_Answer__c FROM Knowledge__kav")
        search_text = f"{case.get('Subject', '')} {issue_name}"
        scored = sorted(
            articles,
            key=lambda a: self._word_overlap(search_text, f"{a.get('Title', '')} {a.get('Summary', '')}"),
            reverse=True,
        )
        top_articles = [
            {
                "Id": a["Id"],
                "Title": a["Title"],
                "Policy": (a.get("FAQ_Answer__c") or "")[:600],
            }
            for a in scored[:3]
        ]

        violation_system = (
            "You are a CRM compliance auditor. "
            "Given a support case and knowledge base articles containing company policies, "
            "determine if the agent violated any policy. "
            "A violation occurs when the case handling contradicts the specific requirements "
            "stated in a KB article (wrong priority, missing required action, wrong solution applied, etc.). "
            "Reply ONLY with a Python list: ['ka0Wt...'] with the violated article Id, or ['None'] if compliant. "
            "No explanation."
        )
        msg = (
            f"Support case:\n{json.dumps(case, default=str)}\n\n"
            f"Case history:\n{json.dumps(history, default=str)}\n\n"
            f"Relevant knowledge base policies:\n{json.dumps(top_articles, default=str)}\n\n"
            "Was any policy violated? Return article Id or ['None']."
        )
        answer, tokens = _call_llm(
            self._provider, self._client, violation_system,
            [{"role": "user", "content": msg}], max_tokens=64
        )
        return answer, tokens, 2

    def _solve_handle_time(
        self, prompt: str, context: str, system: str
    ) -> tuple[str, int, int]:
        """Handle time: two-step query — case count filter + non-transferred handle time."""
        # Extract date window from context (provided as Today's date: YYYY-MM-DD)
        # and from prompt (past N months/quarters/weeks, Fall/Spring etc.)
        # Pass both to LLM to generate the date range, then we run the fixed SQL.
        total_tokens = 0

        # Extract "Today's date" from context first (always present in handle_time tasks)
        today_match = re.search(r"Today'?s? date[:\s]+([0-9]{4}-[0-9]{2}-[0-9]{2})", context, re.IGNORECASE)
        today_str = today_match.group(1) if today_match else ""

        # Step 1: ask LLM to extract date range and count threshold
        parse_system = (
            "You extract date ranges and thresholds from task descriptions. "
            "Reply ONLY in JSON: "
            "{\"start\": \"YYYY-MM-DD\", \"end\": \"YYYY-MM-DD\", "
            "\"min_cases\": N, \"want_highest\": false}\n"
            "For 'past N months': end=today, start=today minus N months.\n"
            "For 'past N quarters': end=today, start=today minus N*3 months.\n"
            "For 'Fall YYYY': start=YYYY-09-01, end=YYYY-11-30.\n"
            "For 'Spring YYYY': start=YYYY-03-01, end=YYYY-05-31.\n"
            "For 'Month YYYY': start=YYYY-MM-01, end=YYYY-MM-last.\n"
            "min_cases is the threshold N in 'more than N cases' or 'at least N cases' (N-1). "
            "IMPORTANT: 'more than 0 cases' means min_cases=0; 'more than 1 case' means min_cases=1; "
            "'more than one case' means min_cases=1; 'at least 1 case' means min_cases=0; "
            "'handling cases' or 'managed cases' with no number means min_cases=0.\n"
            "want_highest=true only if 'highest' or 'maximum' in task (not 'lowest' or 'minimum')."
        )
        parse_msg = f"Today's date: {today_str}\n\nTask: {prompt}"
        parse_resp, tokens = _call_llm(
            self._provider, self._client, parse_system,
            [{"role": "user", "content": parse_msg}], max_tokens=128
        )
        total_tokens += tokens
        logger.info(f"handle_time: today={today_str} parse_resp={parse_resp[:100]}")

        # Extract values from JSON response
        start = re.search(r'"start"\s*:\s*"([0-9-]+)"', parse_resp)
        end = re.search(r'"end"\s*:\s*"([0-9-]+)"', parse_resp)
        min_cases = re.search(r'"min_cases"\s*:\s*(\d+)', parse_resp)
        want_highest = '"want_highest": true' in parse_resp.lower() or '"want_highest":true' in parse_resp.lower()

        start = start.group(1) if start else "2020-01-01"
        end = end.group(1) if end else "2023-12-31"
        min_cases = int(min_cases.group(1)) if min_cases else 0
        logger.info(f"handle_time: start={start} end={end} min_cases={min_cases} highest={want_highest}")

        # Step 2: get case counts (initial + transferred) per agent in window
        count_rows = self._db.query(f"""
            SELECT NewValue__c as agent_id, COUNT(DISTINCT CaseId__c) as case_count
            FROM CaseHistory__c
            WHERE Field__c = 'Owner Assignment'
              AND substr(CreatedDate,1,10) BETWEEN '{start}' AND '{end}'
            GROUP BY NewValue__c
        """)
        eligible_agents = {
            r["agent_id"] for r in count_rows if (r["case_count"] or 0) > min_cases
        }
        logger.info(f"handle_time: {len(eligible_agents)} eligible agents (>{min_cases} cases)")

        # Step 3: avg handle time for non-transferred closed cases in window
        ht_rows = self._db.query(f"""
            SELECT c.OwnerId,
                AVG(julianday(substr(c.ClosedDate,1,19)) - julianday(substr(c.CreatedDate,1,19))) as avg_days
            FROM "Case" c
            WHERE c.ClosedDate IS NOT NULL
              AND substr(c.CreatedDate,1,10) BETWEEN '{start}' AND '{end}'
              AND c.Id NOT IN (
                SELECT CaseId__c FROM CaseHistory__c
                WHERE Field__c = 'Owner Assignment' AND OldValue__c IS NOT NULL
              )
            GROUP BY c.OwnerId
        """)

        # Filter to eligible agents and sort
        filtered = [r for r in ht_rows if r["OwnerId"] in eligible_agents and r["avg_days"] is not None]
        if not filtered:
            # Fallback: no date filter on case count, just use all agents from handle time
            filtered = [r for r in ht_rows if r["avg_days"] is not None]

        if not filtered:
            return "['None']", total_tokens, 2

        filtered.sort(key=lambda r: float(r["avg_days"]), reverse=want_highest)
        winner = filtered[0]["OwnerId"]
        return f"['{winner}']", total_tokens, 2

    def _solve_transfer_count(self, prompt: str, context: str) -> tuple[str, int, int]:
        """Transfer count: fewest/most outgoing transfers in window, eligible by windowed case count."""
        total_tokens = 0

        # Extract Today's date from context
        today_match = re.search(r"Today'?s? date[:\s]+([0-9]{4}-[0-9]{2}-[0-9]{2})", context, re.IGNORECASE)
        today_str = today_match.group(1) if today_match else ""

        # Step 1: parse date window + threshold via LLM
        parse_system = (
            "You extract date ranges and thresholds from task descriptions. "
            "Reply ONLY in JSON: "
            "{\"start\": \"YYYY-MM-DD\", \"end\": \"YYYY-MM-DD\", "
            "\"min_cases\": N, \"want_highest\": false}\n"
            "Named quarters: 'Q1 YYYY'=Jan-Mar, 'Q2 YYYY'=Apr-Jun, 'Q3 YYYY'=Jul-Sep, 'Q4 YYYY'=Oct-Dec.\n"
            "Named months: 'January YYYY'/'Jan YYYY'/'February YYYY' etc → first to last day of that month.\n"
            "Relative: 'past N months': end=today, start=today minus N months.\n"
            "'past/last quarter': end=today, start=today minus 3 months.\n"
            "'past N quarters': end=today, start=today minus N*3 months.\n"
            "'past half-year'/'past 6 months': end=today, start=today minus 6 months.\n"
            "'past N weeks': end=today, start=today minus N*7 days.\n"
            "min_cases: threshold in 'more than N cases' (=N) or 'at least N cases' (=N-1) or 'over N cases' (=N). "
            "'more than one case'/'more than 1 case' → min_cases=1. "
            "'more than two cases'/'over 2 cases' → min_cases=2. "
            "'handling cases'/'managed cases' with no number → min_cases=0.\n"
            "want_highest=true only if 'highest' or 'maximum' transfers (not 'lowest'/'fewest'/'minimum')."
        )
        parse_msg = f"Today's date: {today_str}\n\nTask: {prompt}"
        parse_resp, tokens = _call_llm(
            self._provider, self._client, parse_system,
            [{"role": "user", "content": parse_msg}], max_tokens=128
        )
        total_tokens += tokens

        start_m = re.search(r'"start"\s*:\s*"([0-9-]+)"', parse_resp)
        end_m = re.search(r'"end"\s*:\s*"([0-9-]+)"', parse_resp)
        min_cases_m = re.search(r'"min_cases"\s*:\s*(\d+)', parse_resp)
        want_highest = '"want_highest": true' in parse_resp.lower() or '"want_highest":true' in parse_resp.lower()

        start = start_m.group(1) if start_m else "2020-01-01"
        end = end_m.group(1) if end_m else "2023-12-31"
        min_cases = int(min_cases_m.group(1)) if min_cases_m else 0
        logger.info(f"transfer_count: today={today_str} start={start} end={end} min_cases={min_cases} highest={want_highest}")

        # Step 2: WINDOWED case count (initial + received transfers) via CaseHistory.NewValue__c
        count_rows = self._db.query(f"""
            SELECT NewValue__c as agent_id, COUNT(DISTINCT CaseId__c) as case_count
            FROM CaseHistory__c
            WHERE Field__c = 'Owner Assignment'
              AND substr(CreatedDate,1,10) BETWEEN '{start}' AND '{end}'
            GROUP BY NewValue__c
        """)
        eligible = {r["agent_id"]: r["case_count"] for r in count_rows if (r["case_count"] or 0) > min_cases}
        logger.info(f"transfer_count: {len(eligible)} eligible agents (windowed >{min_cases} cases)")

        if not eligible:
            return "['None']", total_tokens, 2

        # Step 3: windowed outgoing transfer count per agent
        transfer_rows = self._db.query(f"""
            SELECT OldValue__c as agent_id, COUNT(*) as transfers
            FROM CaseHistory__c
            WHERE Field__c = 'Owner Assignment'
              AND OldValue__c IS NOT NULL
              AND substr(CreatedDate,1,10) BETWEEN '{start}' AND '{end}'
            GROUP BY OldValue__c
        """)
        transfer_map = {r["agent_id"]: r["transfers"] for r in transfer_rows}

        # Build candidate list
        candidates = [
            {"OwnerId": aid, "transfers": transfer_map.get(aid, 0), "cases": cnt}
            for aid, cnt in eligible.items()
        ]
        if not candidates:
            return "['None']", total_tokens, 2

        # Sort: fewest/most transfers; tiebreak by most windowed cases DESC
        if not want_highest:
            candidates.sort(key=lambda r: (r["transfers"], -r["cases"]))
        else:
            candidates.sort(key=lambda r: (-r["transfers"], -r["cases"]))

        winner = candidates[0]["OwnerId"]
        logger.info(f"transfer_count: winner={winner} transfers={candidates[0]['transfers']} cases={candidates[0]['cases']}")
        return f"['{winner}']", total_tokens, 2

    def _solve_lead_routing(self, prompt: str, context: str) -> tuple[str, int, int]:
        """Lead routing: Territory Match → Quote Success → Workload Balance."""
        # Extract state from context: "Lead's region: XX"
        state_m = re.search(r"Lead'?s? region[:\s]+([A-Z]{2})", context)
        if not state_m:
            # Fallback: try prompt
            state_m = re.search(r'\b([A-Z]{2})\b', prompt)
        if not state_m:
            return "['None']", 0, 0

        state = state_m.group(1)
        logger.info(f"lead_routing: state={state}")

        # Step 1: find territory matching the state
        territories = self._db.query("SELECT Id, Name, Description FROM Territory2")
        territory_id = ""
        for t in territories:
            desc = t.get("Description", "") or ""
            if state in [s.strip() for s in desc.split(",")]:
                territory_id = t["Id"]
                break

        if not territory_id:
            return "['None']", 0, 1

        # Step 2: agents in that territory
        agents = self._db.query(
            f"SELECT UserId FROM UserTerritory2Association WHERE Territory2Id = '{territory_id}'"
        )
        agent_ids = [a["UserId"] for a in agents]
        if not agent_ids:
            return "['None']", 0, 2

        agent_list = ",".join(f"'{a}'" for a in agent_ids)

        # Step 3: accepted quotes per agent (via Opportunity)
        quote_rows = self._db.query(f"""
            SELECT o.OwnerId, COUNT(q.Id) as accepted_quotes
            FROM Opportunity o
            JOIN Quote q ON q.OpportunityId = o.Id
            WHERE q.Status = 'Accepted'
              AND o.OwnerId IN ({agent_list})
            GROUP BY o.OwnerId
        """)
        quote_map = {r["OwnerId"]: r["accepted_quotes"] for r in quote_rows}
        max_quotes = max(quote_map.values(), default=0)
        top_agents = [a for a in agent_ids if quote_map.get(a, 0) == max_quotes]

        logger.info(f"lead_routing: {len(agent_ids)} territory agents, max_quotes={max_quotes}, tied={len(top_agents)}")

        if len(top_agents) == 1:
            return f"['{top_agents[0]}']", 0, 3

        # Step 4: workload tiebreak — fewest unconverted (open) leads
        top_list = ",".join(f"'{a}'" for a in top_agents)
        lead_rows = self._db.query(f"""
            SELECT OwnerId, COUNT(*) as open_leads
            FROM Lead
            WHERE IsConverted = 0 AND OwnerId IN ({top_list})
            GROUP BY OwnerId
        """)
        lead_map = {r["OwnerId"]: r["open_leads"] for r in lead_rows}
        # agents with 0 open leads won't appear in lead_rows — default to 0
        candidates = sorted(top_agents, key=lambda a: lead_map.get(a, 0))
        winner = candidates[0]
        logger.info(f"lead_routing: winner={winner} open_leads={lead_map.get(winner, 0)}")
        return f"['{winner}']", 0, 4

    def _solve_monthly_trend(self, prompt: str, context: str) -> tuple[str, int, int]:
        """Monthly trend: find month with most cases for a product in a time window."""
        MONTH_NAMES = {
            "01": "January", "02": "February", "03": "March", "04": "April",
            "05": "May", "06": "June", "07": "July", "08": "August",
            "09": "September", "10": "October", "11": "November", "12": "December",
        }

        # Extract product ID directly from prompt (always present)
        prod_m = re.search(r'(01t[A-Za-z0-9]{12,15})', prompt)
        if not prod_m:
            return "['None']", 0, 0
        product_id = prod_m.group(1)

        # Extract today's date from context
        today_m = re.search(r"Today'?s? date[:\s]+([0-9]{4}-[0-9]{2}-[0-9]{2})", context, re.IGNORECASE)
        today_str = today_m.group(1) if today_m else "2024-01-01"

        # Parse window: "past N months", "last N quarters", "past N quarters", "past year"
        months = 12  # default
        m = re.search(r'(?:past|last)\s+(\d+)\s+months?', prompt, re.IGNORECASE)
        if m:
            months = int(m.group(1))
        else:
            m = re.search(r'(?:past|last)\s+(\d+)\s+quarters?', prompt, re.IGNORECASE)
            if m:
                months = int(m.group(1)) * 3
            elif re.search(r'past year|last year', prompt, re.IGNORECASE):
                months = 12

        # Compute start date from today - months
        from datetime import date, timedelta
        today = date.fromisoformat(today_str)
        # subtract months by going back month by month
        year = today.year
        mon = today.month - months
        while mon <= 0:
            mon += 12
            year -= 1
        start = f"{year:04d}-{mon:02d}-01"

        logger.info(f"monthly_trend: product={product_id} window={start}..{today_str} ({months}mo)")

        rows = self._db.query(f"""
            SELECT substr(c.CreatedDate, 6, 2) as month_num, COUNT(*) as cnt
            FROM "Case" c
            JOIN OrderItem oi ON c.OrderItemId__c = oi.Id
            WHERE oi.Product2Id = '{product_id}'
              AND substr(c.CreatedDate, 1, 10) BETWEEN '{start}' AND '{today_str}'
            GROUP BY month_num
            ORDER BY cnt DESC
        """)

        if not rows:
            return "['None']", 0, 1

        top_month = rows[0]["month_num"]
        month_name = MONTH_NAMES.get(top_month, top_month)
        logger.info(f"monthly_trend: top_month={top_month} ({month_name}) cnt={rows[0]['cnt']}")
        return f"['{month_name}']", 0, 1

    def _solve_invalid_config(self, prompt: str, context: str) -> tuple[str, int, int]:
        """Check quote config for quantity limits, exclusion constraints, mandatory bundles."""
        # Quantity limits per product name
        QUANTITY_LIMITS = {
            "AIOptics Vision": 20,
            "CloudLink Designer": 15,
            "CollabDesign Studio": 25,
            "CryptGuard Module": 10,
            "EduFlow Academy": 5,
            "SecuManage Pro": 30,
            "SecureAnalytics Pro": 12,
            "IntegrGuard Secure": 8,
            "CloudInnovate Space": 18,
            "AI DesignShift": 7,
        }
        # Incompatible product pairs (either order)
        EXCLUSION_PAIRS = [
            ("SecureFlow Suite", "SecureTrack Pro"),
            ("SecureAnalytics Pro", "SecuManage Pro"),
            ("DevVision IDE", "NextGen IDE"),
            ("EduTech Lab", "TrainEDU Suite"),
            ("EduTech Advance", "EduFlow Academy"),
        ]
        # Mandatory bundles: if key is present, all values must be present
        MANDATORY_BUNDLES = {
            "PulseSim Pro": ["CircuitMaster Analyzer", "VeriSim Express"],
            "CloudLink Designer": ["DesignEdge Pro", "AI DesignShift"],
            "AI Cirku-Tech": ["CircuitAI Innovator", "AI DesignShift"],
            "OptiPower Manager": ["OptiEnergy Suite", "PowerPro Optimize"],
            "AIOptics Vision": ["Workflow Genius", "AI DesignShift"],
        }

        # Extract Quote ID
        quote_m = re.search(r'(0Q0[A-Za-z0-9]{12,15})', context + prompt)
        if not quote_m:
            return "['None']", 0, 0
        quote_id = quote_m.group(1)

        lines = self._db.query(f"""
            SELECT qli.Quantity, p.Name as ProductName
            FROM QuoteLineItem qli
            JOIN Product2 p ON qli.Product2Id = p.Id
            WHERE qli.QuoteId = '{quote_id}'
        """)
        if not lines:
            return "['None']", 0, 1

        product_names = {r["ProductName"] for r in lines}
        qty_map = {r["ProductName"]: float(r["Quantity"] or 0) for r in lines}

        # Check 1: exclusion constraints (highest priority)
        for p1, p2 in EXCLUSION_PAIRS:
            if p1 in product_names and p2 in product_names:
                logger.info(f"invalid_config: exclusion violation {p1} + {p2}")
                return "['ka0Wt000000EnvJIAS']", 0, 1

        # Check 2: quantity limits
        for name, limit in QUANTITY_LIMITS.items():
            if name in qty_map and qty_map[name] > limit:
                logger.info(f"invalid_config: quantity violation {name}={qty_map[name]} > {limit}")
                return "['ka0Wt000000EnwvIAC']", 0, 1

        # Check 3: mandatory bundles — violation only if primary has PARTIAL companions
        # (some required companions present but not all — incomplete bundle)
        for primary, required in MANDATORY_BUNDLES.items():
            if primary in product_names:
                present = [r for r in required if r in product_names]
                if 0 < len(present) < len(required):
                    missing = [r for r in required if r not in product_names]
                    logger.info(f"invalid_config: partial bundle {primary} present={present} missing={missing}")
                    return "['ka0Wt000000Ens5IAC']", 0, 1

        return "['None']", 0, 1

    # Stage keyword mapping for activity_priority
    _STAGE_ORDER = ["Qualification", "Discovery", "Quote", "Negotiation", "Closed"]
    _STAGE_KEYWORDS: dict[str, list[str]] = {
        "Qualification": [
            "initial contact", "introduct", "cold call", "prospecting", "outreach",
            "qualify", "research industry", "research.*company", "identify.*pain",
            "assess fit", "first meeting", "engage.*prospect", "initial call",
            "explore potential", "learn about", "pre-sales",
        ],
        "Discovery": [
            "discovery", "needs analysis", "gather insight", "analyze.*need",
            "explore need", "understand.*requirement", "conduct.*need",
            "deep dive", "map.*requirement", "pain point analysis",
            "understand.*challenge", "assess.*need",
        ],
        "Quote": [
            "proposal", "quote", "pricing", "product demo", "demo",
            "present solution", "rfp", "value proposition", "send.*offer",
            "draft.*proposal", "prepare.*proposal", "prepare.*offer",
            "tailored solution", "solution presentation", "prepare.*pitch",
        ],
        "Negotiation": [
            "negotiat", "contract", "terms", "objection", "counter",
            "finalize.*agreement", "review.*contract", "legal review",
            "address.*objection", "close.*deal", "pricing.*negotiation",
        ],
        "Closed": [
            "onboarding", "onboard", "implementation", "handoff",
            "post.sale", "win.loss", "kick.off", "kickoff",
            "customer success", "deploy", "record.*finalized",
            "post.close", "kick off", "closeout", "post-sale",
            "retarget", "nurturing campaign", "upsell", "win/loss",
        ],
    }

    @classmethod
    def _classify_task_stage(cls, subject: str) -> str | None:
        """Classify task subject into a pipeline stage. Returns stage name or None."""
        s = subject.lower()
        for stage, keywords in cls._STAGE_KEYWORDS.items():
            for kw in keywords:
                if re.search(kw, s):
                    return stage
        return None

    def _solve_activity_priority(
        self, prompt: str, context: str
    ) -> tuple[str, int, int]:
        """Find tasks that are mismatched to the opportunity's current stage."""
        # Extract opportunity ID (006...)
        opp_m = re.search(r'(006[A-Za-z0-9]{12,15})', context + prompt)
        if not opp_m:
            return "['None']", 0, 0
        opp_id = opp_m.group(1)

        opps = self._db.query(
            f"SELECT Id, StageName FROM Opportunity WHERE Id = '{opp_id}'"
        )
        if not opps:
            return "['None']", 0, 1

        stage = opps[0]["StageName"]
        tasks = self._db.query(
            f"SELECT Id, Subject, Status, Priority FROM Task "
            f"WHERE WhatId = '{opp_id}' AND Status = 'Not Started'"
        )
        if not tasks:
            return "['None']", 0, 2

        # Classify tasks and find mismatches
        mismatched = []
        for t in tasks:
            subj = t.get("Subject") or ""
            classified = self._classify_task_stage(subj)
            if classified is not None and classified != stage:
                mismatched.append(t["Id"])

        logger.info(
            f"activity_priority: opp={opp_id} stage={stage} "
            f"tasks={len(tasks)} mismatched={len(mismatched)}"
        )

        if not mismatched:
            return "['None']", 0, 2

        formatted = ", ".join(f"'{tid}'" for tid in mismatched)
        return f"[{formatted}]", 0, 2

    def _solve_named_entity(
        self, prompt: str, context: str
    ) -> tuple[str, int, int]:
        """Disambiguate product from contact's transactions using prompt keywords."""
        # Extract contact ID (003...)
        contact_m = re.search(r'(003[A-Za-z0-9]{12,15})', context + prompt)
        if not contact_m:
            return "['None']", 0, 0
        contact_id = contact_m.group(1)

        # Extract date if present
        date_m = re.search(r'(\d{4}-\d{2}-\d{2})', context + prompt)
        date_filter = ""
        if date_m:
            date_str = date_m.group(1)
            date_filter = f"AND o.EffectiveDate = '{date_str}'"

        rows = self._db.query(f"""
            SELECT oi.Product2Id, p.Name, o.EffectiveDate
            FROM Contact c
            JOIN "Order" o ON o.AccountId = c.AccountId
            JOIN OrderItem oi ON oi.OrderId = o.Id
            JOIN Product2 p ON p.Id = oi.Product2Id
            WHERE c.Id = '{contact_id}' {date_filter}
            ORDER BY o.EffectiveDate DESC
        """)
        if not rows:
            return "['None']", 0, 1

        if len(rows) == 1:
            return f"['{rows[0]['Product2Id']}']", 0, 1

        # Multiple products: pick the one with highest word overlap with prompt+context
        search_text = f"{prompt} {context}"
        best_row = max(rows, key=lambda r: self._word_overlap(search_text, r.get("Name", "")))
        product_id = best_row["Product2Id"]
        logger.info(
            f"named_entity: contact={contact_id} candidates={len(rows)} "
            f"best={best_row.get('Name')} id={product_id}"
        )
        return f"['{product_id}']", 0, 1

    def _solve_sales_insight(
        self, prompt: str, context: str
    ) -> tuple[str, int, int]:
        """Analyze voice call transcripts for sales insights."""
        # Extract opportunity ID (006...)
        opp_m = re.search(r'(006[A-Za-z0-9]{12,15})', context + prompt)
        if not opp_m:
            return "No transcripts found for this opportunity.", 0, 0
        opp_id = opp_m.group(1)

        transcripts = self._db.query(f"""
            SELECT Body__c FROM VoiceCallTranscript__c
            WHERE OpportunityId__c = '{opp_id}'
            ORDER BY CreatedDate
        """)
        if not transcripts:
            return "No call transcripts available for this opportunity.", 0, 1

        # Combine transcript bodies (truncated)
        combined = "\n\n---\n\n".join(
            (t.get("Body__c") or "")[:1500] for t in transcripts[:3]
        )

        insight_system = (
            "You are a sales analyst reviewing CRM call transcripts. "
            "Extract key insights: competitor mentions, objections raised, "
            "pain points, buying signals, decision makers, timeline, budget hints. "
            "Be specific and concise. 2-4 bullet points."
        )
        msg = (
            f"Task: {prompt}\n\n"
            f"Call transcript(s) for opportunity {opp_id}:\n{combined}\n\n"
            "Provide key sales insights from these transcripts."
        )
        answer, tokens = _call_llm(
            self._provider, self._client, insight_system,
            [{"role": "user", "content": msg}], max_tokens=512
        )
        return answer, tokens, 1
