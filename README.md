# CRM Purple Agent

Purple Agent для соревнования [AgentX-AgentBeats](https://rdi.berkeley.edu/agentx-agentbeats.html) (Berkeley RDI).

**Цель**: Победить в Leaderboard Green Agent-а [Entropic CRMArena](https://agentbeats.dev/agentbeater/entropic-crmarenapro).

## Быстрый старт

```bash
cp .env.example .env
# Вписать API ключ в .env
uv sync
uv run src/server.py
```

## Документация

| Файл | Описание |
|------|----------|
| [docs/competition.md](docs/competition.md) | Контекст соревнования, роли агентов |
| [docs/green_agent.md](docs/green_agent.md) | Как Green Agent оценивает — правила игры, формулы, форматы |
| [docs/dev_plan.md](docs/dev_plan.md) | План разработки по спринтам |
| [docs/submission.md](docs/submission.md) | Как задеплоить и попасть в Leaderboard |

## Репозитории

- **Этот (Purple Agent)**: https://github.com/cashman2100/crm_agent
- **Green Agent (только читаем)**: https://github.com/rkstu/entropic-crmarenapro
- **Leaderboard**: https://agentbeats.dev/agentbeater/entropic-crmarenapro
