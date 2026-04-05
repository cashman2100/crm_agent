from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import UnsupportedOperationError
from a2a.utils import new_agent_text_message

from agent import Agent


class Executor(AgentExecutor):
    def __init__(self):
        self._agents: dict[str, Agent] = {}

    def _get_agent(self, context_id: str) -> Agent:
        if context_id not in self._agents:
            self._agents[context_id] = Agent()
        return self._agents[context_id]

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.message:
            raise ValueError("Missing message in request")

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id,
            context_id=context.context_id,
        )
        agent = self._get_agent(context.context_id)

        try:
            await agent.run(context.message, context, updater)
        except Exception as e:
            await updater.failed(
                message=new_agent_text_message(f"Agent error: {e}"),
            )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise UnsupportedOperationError()
