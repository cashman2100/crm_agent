# Контекст соревнования

## AgentX-AgentBeats

Соревнование от Berkeley RDI: https://rdi.berkeley.edu/agentx-agentbeats.html
Платформа: https://agentbeats.dev

Соревнование проходит в 2 стадии:
1. **Стадия 1** — разработка Green Agents (оценщиков) — **завершена**
2. **Стадия 2** — разработка Purple Agents (участников) — **сейчас идёт**, разбита на спринты

## Роли агентов

```
AgentBeats платформа
        │
   [Green Agent]  ← уже существует, мы его НЕ делаем, только изучаем
   Entropic CRMArena
        │  отправляет CRM задачи и оценивает ответы
        ▼
   [Purple Agent] ← ЭТО МЫ РАЗРАБАТЫВАЕМ
   crm_agent
```

### Green Agent — Судья/Оценщик
- **Уже готов**, сделан командой rkstu
- GitHub: https://github.com/rkstu/entropic-crmarenapro
- Локальная копия: `/Users/andrey/ITMO/entropic-crmarenapro`
- AgentBeats ID: `019ba211-13b7-7e83-9086-c8015a5e4957`
- Берёт CRM задачи → отправляет Purple агенту → оценивает ответы → выставляет баллы

### Purple Agent — Участник соревнования
- **Это мы разрабатываем** в этом репозитории
- GitHub: https://github.com/cashman2100/crm_agent
- Получает CRM задачи от Green агента → думает → даёт ответы
- Регистрируется на agentbeats.dev → попадает в Leaderboard

## Выбранный Green Agent

**Entropic CRMArena** (Business Process Agent Track)
- Занял 1-е место на первой стадии соревнования
- Leaderboard: https://agentbeats.dev/agentbeater/entropic-crmarenapro
- Reference Purple Agent от авторов: https://github.com/rkstu/baseline-crm-agent (AgentBeats ID: `019ba27e-3b82-7d43-8822-51357ccd4861`)

## Участник

- GitHub: cashman2100 (Andrey, ИТМО)
