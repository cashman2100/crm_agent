import argparse
import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard, AgentCapabilities, AgentSkill

from executor import Executor


def build_agent_card(host: str, port: int, card_url: str | None) -> AgentCard:
    base_url = card_url or f"http://{host}:{port}"
    return AgentCard(
        name="CRM Purple Agent",
        description=(
            "A CRM task-solving agent for the AgentX-AgentBeats competition. "
            "Handles 22 categories of B2B CRM tasks including lead qualification, "
            "sales insights, entity disambiguation, and privacy-aware responses."
        ),
        url=f"{base_url}/",
        version="0.1.0",
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="crm_task",
                name="CRM Task Solver",
                description="Solves CRM tasks from the CRMArenaPro dataset",
                tags=["crm", "b2b", "sales", "lead-qualification"],
                examples=[],
            )
        ],
        defaultInputModes=["text"],
        defaultOutputModes=["text"],
    )


def main():
    parser = argparse.ArgumentParser(description="CRM Purple Agent A2A Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9010)
    parser.add_argument("--card-url", default=None)
    args = parser.parse_args()

    agent_card = build_agent_card(args.host, args.port, args.card_url)
    handler = DefaultRequestHandler(
        agent_executor=Executor(),
        task_store=InMemoryTaskStore(),
    )
    app = A2AStarletteApplication(agent_card=agent_card, http_handler=handler)

    uvicorn.run(app.build(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
