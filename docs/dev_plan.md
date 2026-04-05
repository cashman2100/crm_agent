# План разработки Purple Agent

## Приоритеты (по весу в scoring)

1. **FUNCTIONAL (30%) + DRIFT_ADAPTATION (20%) = 50%** — главный фокус
2. **TOKEN/QUERY/TRAJECTORY EFFICIENCY (34%)** — достигается одним-двумя LLM вызовами
3. **ERROR_RECOVERY + HALLUCINATION_RATE (16%)** — аккуратный код

## Целевые показатели

| Измерение | Цель | Как достичь |
|-----------|------|-------------|
| FUNCTIONAL | 80–90% | Сильная LLM + task-specific промпты |
| DRIFT_ADAPTATION | 75–85% | Нормализация drift_mappings перед обработкой |
| TOKEN_EFFICIENCY | 90%+ | One-shot ответы, < 8k токенов |
| QUERY_EFFICIENCY | 95%+ | Нет реальных SQL запросов (задачи текстовые) |
| TRAJECTORY_EFFICIENCY | 95%+ | Решать за 1 шаг |
| ERROR_RECOVERY | 80%+ | Try/except + fallback ответ |
| HALLUCINATION_RATE | 95%+ | Нет невалидных tool calls |

**Ожидаемый итоговый балл**: 80–90 из 100

---

## Спринт 1 — A2A скелет (1–2 дня)

**Цель**: Рабочий агент, который отвечает на запросы (пусть неправильно, но корректно)

- [ ] Взять [RDI-Foundation/agent-template](https://github.com/RDI-Foundation/agent-template) как основу
- [ ] Реализовать `/.well-known/agent-card.json`
- [ ] Принимать A2A JSON-RPC запросы
- [ ] Парсить входящий формат от Green Agent
- [ ] Возвращать ответ в правильном формате с метриками
- [ ] Зарегистрировать на agentbeats.dev → получить Agent ID
- [ ] Тест: `task_limit=1` локально

## Спринт 2 — Умный ответчик (3–5 дней)

**Цель**: Высокое FUNCTIONAL (80%+) и DRIFT_ADAPTATION (75%+)

Ключевое понимание: **задачи текстовые**, не требуют реального SQL.
`required_context` содержит все нужные данные прямо в тексте.

**Архитектура обработки задачи:**
```
1. Разобрать drift_mappings из entropy
2. Нормализовать контекст (заменить дрифтнутые имена обратно на оригинальные)
3. Определить тип задачи → выбрать prompt шаблон
4. LLM вызов с задачей и контекстом
5. Извлечь ответ в правильном формате
6. Вернуть answer + metrics
```

**Нормализация Schema Drift:**
```python
def normalize_context(context: str, drift_mappings: list) -> str:
    for mapping in drift_mappings:
        context = context.replace(
            mapping['drifted_column'],
            mapping['original_column']
        )
    return context
```

**Обработка по типу reward_metric:**
- `exact_match` → извлечь конкретное значение (имя, Yes/No, список)
- `fuzzy_match` → развёрнутый ответ по контексту
- `privacy_rejection` → отказать: "I cannot provide this information as it contains confidential/private data."

**Task-specific промпты:**

| Категории | Стратегия |
|-----------|-----------|
| lead_qualification, case_routing | Анализ по критериям, вернуть список факторов |
| sales_insight_mining, knowledge_qa | Найти конкретные инсайты в тексте |
| best_region_identification, monthly_trend_analysis | Числовой анализ, вернуть название |
| named_entity_disambiguation | Точный match имени из контекста |
| private_customer_information, confidential_company_knowledge | Всегда отказывать |
| wrong_stage_rectification | Логика CRM pipeline, вернуть правильный Stage |

## Спринт 3 — Оптимизация эффективности (2–3 дня)

**Цель**: Efficiency dimensions 90%+

- One-shot: один LLM вызов на задачу → TRAJECTORY_EFFICIENCY = 100
- Не передавать `optional_context` если не нужен → меньше токенов
- Всегда возвращать `metrics` (tokens, queries, tool_calls) в ответе
- Structured output / JSON mode для надёжного извлечения ответа

## Спринт 4 — Тонкая настройка (2–3 дня)

**Цель**: Улучшить слабые категории, попасть в топ

- Запустить полный тест (2140 задач), проанализировать по категориям
- Few-shot примеры для категорий с низким pass rate
- Проверить edge cases для exact_match (регистр, пунктуация, множественное число)
- Протестировать Context Rot: убедиться что агент игнорирует distractor записи

## Спринт 5 — Деплой (1 день)

- [ ] `docker build --platform linux/amd64 -t ghcr.io/cashman2100/crm_agent:latest .`
- [ ] `docker push ghcr.io/cashman2100/crm_agent:latest` (сделать публичным!)
- [ ] Обновить `scenario.toml` в форке Green Agent репозитория
- [ ] Добавить ANTHROPIC_API_KEY в GitHub Secrets
- [ ] Запустить GitHub Actions → смотреть workflow → мержнуть PR
- [ ] Проверить результат на leaderboard

---

## Стек

| Компонент | Выбор |
|-----------|-------|
| LLM | claude-sonnet-4-6 (Anthropic) или gpt-4o |
| Framework | Python + FastAPI |
| A2A base | RDI agent-template |
| Package manager | uv |
| Deployment | Docker → ghcr.io |
