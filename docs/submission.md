# Как задеплоить и попасть в Leaderboard

## Требования к Purple Agent

| Требование | Детали |
|------------|--------|
| A2A compliance | Expose `/.well-known/agent-card.json` |
| JSON-RPC | Принимать POST запросы в A2A формате |
| Docker | Собрать для `linux/amd64` |
| Public image | ghcr.io образ должен быть публичным |

## Пошаговая инструкция

### Шаг 1: Зарегистрировать агент на AgentBeats

1. Перейти на https://agentbeats.dev
2. Войти через GitHub
3. "Register Agent":
   - Agent Type: **Purple**
   - Display Name: придумать имя
   - Docker Image: `ghcr.io/cashman2100/crm_agent:latest`
   - Repository URL: https://github.com/cashman2100/crm_agent
4. Скопировать полученный **Agent ID**

### Шаг 2: Собрать и запушить Docker образ

```bash
# Собрать для linux/amd64 (важно!)
docker build --platform linux/amd64 -t ghcr.io/cashman2100/crm_agent:latest .

# Залогиниться в GitHub Container Registry
docker login ghcr.io -u cashman2100
# Пароль — GitHub Personal Access Token с правами write:packages

# Запушить
docker push ghcr.io/cashman2100/crm_agent:latest
```

После пуша: зайти на https://github.com/cashman2100/crm_agent/packages →
сделать пакет **публичным** (Package Settings → Change visibility → Public)

### Шаг 3: Форкнуть Green Agent репозиторий

1. Форкнуть https://github.com/rkstu/entropic-crmarenapro
2. Включить GitHub Actions в форке (вкладка Actions → Enable)

### Шаг 4: Настроить scenario.toml

В форке отредактировать `scenario.toml`:

```toml
[green_agent]
agentbeats_id = "019ba211-13b7-7e83-9086-c8015a5e4957"  # Entropic CRMArena

[[participants]]
agentbeats_id = "ВАШ_AGENT_ID"  # ← ID из Шага 1
name = "agent"
env = { ANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}" }

[config]
task_limit = 20  # Для тестирования. Убрать для полного прогона (2140 задач)
```

### Шаг 5: Добавить секреты в GitHub

В форке: Settings → Secrets and variables → Actions → New repository secret:
- Name: `ANTHROPIC_API_KEY`
- Value: ключ от Anthropic

### Шаг 6: Запустить

1. Запушить изменённый `scenario.toml` в форк
2. GitHub Actions автоматически запустится (~10–30 минут на полный прогон)
3. Actions создаст PR с результатами → мержнуть
4. Результаты появятся на https://agentbeats.dev/agentbeater/entropic-crmarenapro

## Временные публичные ключи для тестирования

Для тестирования через GitHub Actions нужен API ключ в GitHub Secrets.
Ключ не светится в коде — только в зашифрованных Secrets.

Если нужен временный ключ специально для тестирования:
- Создать отдельный API ключ с лимитом расходов
- Добавить его в GitHub Secrets
- После теста отозвать ключ

## Локальное тестирование перед деплоем

Смотри [green_agent.md](green_agent.md) — раздел "Локальное тестирование".

Порядок тестирования:
1. `task_limit=1` — проверить что pipeline работает
2. `task_limit=5` — проверить разные категории задач
3. `task_limit=50` — оценить качество
4. Полный прогон — для финального сабмита
