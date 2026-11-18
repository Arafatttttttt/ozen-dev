# API-контракт v1: `POST /api/v1/accounting/sync`

Один запрос — один документ. Тело — **полный снимок** документа (state transfer, не дельта):
приёмник делает upsert по `document_id`.

## Заголовки запроса

| Заголовок | Обязателен | Описание |
|---|---|---|
| `Content-Type` | да | `application/json; charset=utf-8` |
| `Idempotency-Key` | да | UUID сообщения из очереди. Повторная доставка с тем же ключом → `200 duplicate`, без повторной проводки. |
| `X-Ozen-Attempt` | нет | Номер попытки (1, 2, …) — для логов приёмника. |
| `Authorization` | по настройке | `Bearer <token>` |

## Тело

| Поле | Тип | Обяз. | Правила |
|---|---|---|---|
| `document_id` | UUID | ✔ | `УникальныйИдентификатор()` ссылки документа |
| `client_name` | string | ✔ | 1–255 символов, не пустая после trim |
| `amount` | number | ✔ | > 0, до 2 знаков; равен сумме `items[].amount` (допуск 0.01) |
| `items` | array | ✔ | 1–1000 строк |
| `items[].name` | string | ✔ | 1–255 символов |
| `items[].quantity` | number | ✔ | > 0 |
| `items[].price` | number | ✔ | ≥ 0 |
| `items[].amount` | number | ✔ | ≥ 0 |
| `object_type` | enum | ✔ | `ПродажаАбонемента` \| `Возврат` \| `Оплата` |
| `operation` | enum | — | `upsert` (по умолчанию) \| `cancel` (документ распроведён) |
| `version` | int | — | Монотонная версия снимка, мс UTC. Более старая версия не перезаписывает новую. |
| `message_id` | UUID | — | Дублирует `Idempotency-Key` |
| `document_number`, `document_date`, `client_id`, `currency`, `source`, `schema_version` | — | — | Справочные поля; `currency` — ISO 4217, по умолчанию `KZT` |

Неизвестные поля игнорируются — 1С может расширять пакет, не ломая приёмник.

Пример: [payload-example.json](payload-example.json).

## Ответы

| Код | Тело | Что делает 1С |
|---|---|---|
| `200` | `{"status": "accepted" \| "updated" \| "cancelled" \| "duplicate" \| "stale_ignored", "document_id", "accounting_entry_id", "received_at"}` | `Отправлено` |
| `400` | `{"status": "rejected", "error": "validation_error", "detail": [{"field", "message", "type"}]}` | `Ошибка` сразу — повтор битого пакета бессмысленен |
| `500` | `{"status": "error", "error": "internal_error", "detail": "..."}` | Повтор с экспоненциальной задержкой |
| нет ответа | — | Повтор с задержкой; в `Ошибка` не переводится |

Валидация выполняется **до** имитации сбоя: некорректный пакет всегда получает `400`, а не случайный `500`.

## Служебные эндпоинты эмулятора

| Метод | Путь | Назначение |
|---|---|---|
| `POST` | `/api/v1/accounting/validate` | Проверка пакета без сохранения и без сбоев (удобно для Теста 1) |
| `GET` | `/api/v1/accounting/documents[/{id}]` | Что «проведено» в бухгалтерии |
| `GET` | `/api/v1/admin/stats` | Счётчики: requests, accepted, duplicate, rejected, simulated_failures |
| `PUT` | `/api/v1/admin/chaos` | `{"failure_rate": 0.0..1.0}` — менять долю сбоев на лету |
| `GET` | `/health`, `/docs` | Health-check, Swagger UI |
