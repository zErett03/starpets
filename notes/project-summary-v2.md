# StarPets — выжимка v2 (самодостаточная, для нового чата)

> Это единый стартовый контекст. Заменяет исходную «выжимку» и `session-notes-2026-06-29.md`.
> Папка проекта прикреплена к чату. **Изменения в файлах — только после разрешения** (репо склонирован, подключён к Git на ПК).
> Дата консолидации: 2026-06-30.

---

## Что за проект

Автоарбитраж предметами Adopt Me: покупка на StarPets (ex-buyers API) → перепродажа на ggsel → доставка через Roblox-трейд.

- **Стек:** FastAPI + APScheduler + PostgreSQL на Railway
- **Репо:** `github.com/bebr2014/starpets`. Локально: `…\StarPets\starpets\` (ветка `master`).
- **Прод:** `https://starpets-production.up.railway.app`
- **Пуш/деплой:** `git push origin HEAD:master` → Railway авто-деплоит (рестарт; `entrypoint.sh` накатывает `alembic upgrade head` на старте).
- Каждый деплой = рестарт: обрывает текущий `SyncPrices` и сбрасывает его 30-мин таймер (норма). Не пушить во время живой доставки.

---

## Архитектура

FastAPI-сервис — **обёртка над партнёрским API StarPets «ex-buyers»** (хост `market.neuralgeneration.com/api`). Подпись HMAC-SHA512: заголовки `Api_Key`, `X-Api-Key`=account_id, `Signature`; параметры `timestamp`+`recvWindow` (биржевая схема). Покупку/трейды/доставку выполняет StarPets, мы оркестрируем.

Есть **официальная документация** API (PDF от Мухаммада @mohilles — был приложен в прошлой сессии). Ключевое из неё — в разделе «Справка по API» ниже.

Цепочка доставки: `buy → withdrawal(create_trade) → friendship → Roblox-исполнение`. Вход покупателя к боту — **Join на профиле бота на roblox.com** (НЕ телепорт к другу — даёт Error 773). Бот принимает заявку в друзья с задержкой ~1 мин после `/friendship`. Окно онлайна бота ~10 мин.

---

## Текущее состояние (что сделано и работает)

### ✅ Фикс доставки (friendship) — задеплоен, проверен на ПК
Был баг: `friendship` слался ровно один раз на T=0 (в `deliver.py:227`) — до того как покупатель добавил бота → впустую. Повторно не слался → «бот не добавил».
Фикс — 3 слоя:
1. `/delivery` re-send при заходе на страницу (троттл 20с) — `api/__init__.py`.
2. Авто-рефреш 20с на dispatched-странице.
3. **Серверный re-send в мониторе** каждые ~30с первые 10 мин (главный, кросс-девайсный).
Проверено на ПК (заказ 23 доставлен). **Не проверено:** боевой тест с телефона.

### ✅ Курсорный мониторинг — задеплоен (миграция 0009)
Был баг слепоты: `/ex-buyers/trades/updates` отдаёт ≤50 событий, старейшие-первыми; окно `date=now−6h` забивалось старыми трейдами → `FINISHED(8)` не виден → заказы не закрывались.
Фикс (по доке — параметр `cursor`):
- Таблица `kv_state` (миграция `0009`) хранит `trades_cursor` (durable, переживает редеплой).
- `monitor_delivery.py` тянет события инкрементально по `cursor` (бутстрап по `date` при пустом курсоре), settle: `8→done+MARK_DELIVERED`, `6/7→failed`, `0–5` живые.
- Исправлен баг `status=5` (был ошибочно «expired»; убран опасный авто-`create_trade`, пересоздававший живые трейды).
- 400-шум friendship погашен через колонку `starpets_status` (шлём только пока статус ∈ {None,0}).
Монитор крутится (`[MonitorDelivery] events fetched=… cursor=…→…`). **Не проверено end-to-end:** свежий заказ → инициализация курсора → авто-закрытие на FINISHED.

---

## Открытые задачи (по приоритету)

1. **End-to-end тест курсорного монитора:** `test-webhook → trigger-deliver`, довести трейд до конца → ждём `events fetched>0`, `cursor=None→<id>`, `→ delivered, MARK_DELIVERED queued`, заказ `done`.
2. **Боевой тест доставки с телефона** — подтвердить серверный слой friendship при уходе в приложение Roblox.
3. **Старые зависшие dispatched-заказы** (16, 18–23) новый монитор НЕ закроет (их события вне 6ч-окна и ниже курсора) → закрыть вручную (psql ниже).
4. **Инструкция `/delivery`** (`api/__init__.py:565–569`): шаг 3 «телепортируйтесь» → Error 773. Заменить на «профиль бота → Join». Заодно таймер 5→8-10 мин (`:563/:571/:575`).
5. **Возвраты** покупателям — штатно через `DELETE /api/trades/ex-buyers/withdrawal` (`reasonType`); сейчас руками через ggsel. Это и «баг №5»: страница `failed` пишет «деньги вернутся автоматически» (`:551`), а кода возврата нет.
6. **Автозапуск SyncPrices** после рестарта (сейчас ручной `/sync-prices`, первый авто-прогон через 30 мин). Рекомендация: запускать на старте только если цены устарели — хранить `last_sync_prices_at` в `kv_state`, пропускать если прошло < N мин (умный вариант без прогона на каждый деплой). Простой вариант — `next_run_time=datetime.now()` в `jobs.py:183`, но тогда ~45-мин прогон на каждый деплой.
7. **Мелочи:** `task_runner.pop_task` не фильтрует по `scheduled_at` → backoff `[2,5,15,30]` мин не работает (`:14/:106`). `/test-trade-status` возвращает `Api_Key`/`Signature` в `request_headers` — прибрать/закрыть секретом.
8. **К Мухаммаду (остаток):** `NO_ACCESS (210)` при withdrawal — может ли возникать, если ник покупателя совпадает с аккаунтом продавца на маркете? (Вебхуков нет — подтверждено; пагинация/коды статусов/per-trade — закрыты докой.)

---

## Справка по API (из официальной доки)

**Коды статуса трейда:**
`0 CREATED` (ждёт заявки в друзья) · `1 DELAYED_START` · `2 PENDING_FRIEND` (проверка/принятие заявки) · `3 PENDING_START` · `4 STARTED` (готов к сделке) · `5 IN_PROGRESS` (идёт обмен) · `6 FAILED` · `7 CANCELED` · `8 FINISHED`.
`event:1` = обновление (есть `data.status`), `event:2` = завершён/отменён (без data).

**`GET /api/ex-buyers/trades/updates`** — `cursor` (id события, «после которого») ИЛИ `date` (взаимоисключающие), `limit` 1–50. Это штатный механизм инкрементального поллинга. Per-trade статус-эндпоинта НЕТ. Вебхуков НЕТ.

**`PUT /api/trades/ex-buyers/friendship`** — `tradeId`. Запускает принятие заявки в друзья ботом.

**`DELETE /api/trades/ex-buyers/withdrawal`** — отмена/возврат: `tradeId` + `reasonType` (есть `roblox_join_error_773`, `seller_left_game`, `change_mind_about_picking_up` и др.).

**Подпись:** строка `{k1}:{v1};{k2}:{v2};…`, объекты/массивы → JSON; `HMAC-SHA512(secret)`.

---

## Полезные эндпоинты

**Читающие (безопасны):** `/system-status`, `/order-info?order_id=X`, `/db-stats`, `/offer-errors`, `/cheapest-offers` (фильтрует по `draft` — пустой, надо `active`).

**Тестовые (тратят деньги):** `/test-buy`, `/test-trade?item_id=X&username=НИК`, `/test-friendship?trade_id=X`, `/test-deliver-dryrun?ggsel_offer_id=X&username=Y`, `/sync-prices`.

**Боевой сквозной тест (PowerShell, без curl):**
```powershell
$body = @{ ggsel_offer_id = <ID>; roblox_username = "<НИК>" } | ConvertTo-Json
$r = Invoke-RestMethod -Uri "https://starpets-production.up.railway.app/test-webhook" -Method Post -Body $body -ContentType "application/json"
Invoke-RestMethod "https://starpets-production.up.railway.app/trigger-deliver?order_id=$($r.order_id)"
Invoke-RestMethod "https://starpets-production.up.railway.app/order-info?order_id=$($r.order_id)" | ConvertTo-Json -Depth 5
# страница в браузере (id = ggsel_order_id, НЕ внутренний order_id):
# https://starpets-production.up.railway.app/delivery?id=<ggsel_order_id>
```
Примечание: `/test-webhook` сам доставку НЕ запускает (нужен `/trigger-deliver`). `/delivery?id=` принимает `ggsel_order_id`.

---

## Ручное закрытие зависшего заказа (psql в Railway)

```bash
psql $DATABASE_URL
UPDATE orders SET delivery_status = 'done' WHERE id = <order_id>;     -- закрыть как доставленный
UPDATE orders SET delivery_status = 'failed' WHERE id = <order_id>;   -- пометить failed
SELECT id, delivery_status, bot_name, starpets_custom_id, starpets_status, error_reason FROM orders WHERE id = <order_id>;
```

---

## Операционное состояние (проверить — могло устареть)

| Параметр | Значение (на момент прошлой сессии) |
|---|---|
| Офферы | ~13 626 на паузе, ~5 активных |
| Баланс токена StarPets | ~$16.76 |
| Maintenance mode | включён (`api/__init__.py:1042–1045`) — глушит только активацию офферов, не доставку |

Зависшие заказы на разбор: 16 (Winter Buck, dispatched), 17 (Cabbit, failed no_items), 21 (Cactus Friend), 22 (Sprout Snail, dispatched), 18/19/20/23 (dispatched, тестовые) — закрыть вручную.

---

## Окружение Cowork (на заметку)

Linux-песочница периодически падает с «Not enough disk space» — тогда `git push`/`psql`/PDF-рендер недоступны, делать из своего терминала; файловые правки (read/edit) работают всегда. Помогает перезапуск приложения / чистка места.
