# StarPets — выжимка v3 (самодостаточная, для нового чата)

> Заменяет v2. Дата консолидации: 2026-07-01.
> Папка проекта прикреплена к чату (клон репо, Git на ПК, деплой на Railway).
> **Изменения в файлах — только после разрешения.**

---

## Что за проект

Автоарбитраж предметами Adopt Me: покупка на StarPets (ex-buyers API) → перепродажа на ggsel → доставка Roblox-трейдом.

- **Стек:** FastAPI + APScheduler + PostgreSQL на Railway
- **Репо:** `github.com/bebr2014/starpets`. Локально `…\StarPets\starpets\` (ветка `master`).
- **Прод:** `https://starpets-production.up.railway.app`
- **Деплой:** `git push origin HEAD:master` → Railway авто-деплой (рестарт; `entrypoint.sh` накатывает `alembic upgrade head`). **Не пушить во время живой доставки.**

---

## ГЛАВНЫЙ ВЫВОД СЕССИИ (тест №1 закрыт)

**StarPets НЕ присылает терминальные события трейда (`8 FINISHED`, `6/7 FAILED/CANCELED`, `event:2`) в `/ex-buyers/trades/updates`.** Поток любой выдачи обрывается на `5 IN_PROGRESS` и замолкает.

- Проверено на 3 трейдах (`59620726`, `59623775`, `59645773`), включая идеально чистый проход, датовым опросом (не курсорным), спустя часы. Максимальный статус, который мы вообще видели — `5`.
- **Мухаммад (вендор) подтвердил:** их обёртка «пока не отправляет запросы после status 5, скоро доделают».
- Следствие: **курсорный монитор корректен, но settle-на-8 никогда не срабатывает** → заказы висят `dispatched`, авто-закрытия нет. Пока чиним обходом (админка + ручное закрытие).
- Per-trade статус-эндпоинта нет (`/trade/status` → 404). Инвентарь-эндпоинта нет (`/items` → 404). `get_trade_updates`/`get_items` в клиенте — мёртвые заглушки (не `ex-buyers`-путь), почистить.

Статусы трейда: `0 CREATED · 1 DELAYED_START · 2 PENDING_FRIEND · 3 PENDING_START · 4 STARTED · 5 IN_PROGRESS · 6 FAILED · 7 CANCELED · 8 FINISHED`. `event:1` = апдейт (есть `data.status`), `event:2` = финиш/отмена (без data).

---

## Боевой флоу и таймеры

1. **precheck** (`/hooks/ggsel/precheck`) — проверка ника, qty=1, живой доступности (`get_top_item`). Создаёт precheck-заказ.
2. **Оплата** (`/hooks/ggsel/notification`) — ставит `paid_at`, `pending`, кладёт задачу `DELIVER` (idempotent по `id_i`).
3. **Редирект** покупателя на `/delivery?id=<ggsel_order_id>` (спиннер, авто-рефреш 5с пока нет бота).
4. **Worker** (поллинг 1с) → `deliver_order`: buy (ретрай на code=330) → `create_trade` → сохраняет `purchase_id`/`trade_id`/`bot_name` → friendship на T=0 → `dispatched`.
5. **/delivery** при dispatched: имя бота, таймер **5:00**, 4 шага, авто-рефреш 20с, friendship re-send при заходе (троттл 20с).
6. **Монитор** (30с): курсорный поллинг событий; friendship re-send ~30с первые 10 мин (пока `starpets_status ∈ {None,0}`); settle на 8→done+MARK_DELIVERED, 6/7→failed (НЕ срабатывает — терминала нет).
7. **MARK_DELIVERED** — отмечает доставку в ggsel (release оплаты). В бою сейчас только вручную из админки.

Фон: `starpets_sync` 10м · `sync_prices` 30м · `token_refresh` 20м (заглушка) · `reconcile` 1ч (dispatched>2ч → re-queue MONITOR) · `trade_protection` 1ч (не реализован).

Цепочка доставки: **вход покупателя — Join на профиле бота на roblox.com** (НЕ телепорт — Error 773). Бот принимает заявку в друзья ~1 мин после `/friendship`. Окно онлайна бота ~10 мин. Если бот вышел/удалил и трейд < 4 — доставка не удалась.

---

## Админка `/admin` (сделано в этой сессии)

Операторская панель для ручного закрытия (обход отсутствия терминала). Файлы: `app/api/admin.py` (роутер, подключён в `app/api/__init__.py`), поля/таблица в `app/db/models.py`, миграция `0010`.

- **Auth:** HTTP Basic, `ADMIN_USER`/`ADMIN_PASSWORD` в Railway Variables (fail-closed без пароля). В Railway переменные применяются через **Promote → Shared** (иначе staged, приложение не видит).
- **Действия:** сменить `delivery_status`; **«Новый трейд»** (синхронный redeliver = зонд+действие: успех → предмет был у нас/не доставлено, перезапуск; ошибка `130 NOT_FOUND` → предмет ушёл/доставлено; `210` → залочен/не доставлено); **«Отправить на ggsel»** (done + MARK_DELIVERED); правка ника; **«История доставки»** (модалка).
- **Аудит-трейл** (`trade_events`, миграция 0010): монитор персистит каждое статусное событие трейда по заказу (`order_id` штампуется в момент записи → переживает пересоздание трейда). Модалка «История доставки» показывает таймлайн + копируемую сводку-пруф для арбитража ggsel (прогресс до `5`, ник, бот, покупка). **Наполняется с новых заказов** после деплоя (старое за курсором не подтянуть).
- «Новый трейд» требует `purchase_id`. Если его нет (выдача не дошла до покупки) — нужен полный `DELIVER` через `/trigger-deliver?order_id=X`.

---

## Прямой опрос StarPets из PowerShell (полезно для дебага)

Подпись HMAC-SHA512: каноничная строка `k:v;...;` в **порядке вставки** params, hex. Заголовки `Api_Key`(shared_key), `X-Api-Key`(account_id), `Signature`. Секрет `starpets_secret` — в Railway Variables. Пример (updates по дате):

```powershell
$secret='<STARPETS_SECRET>'; $ts=[int64]([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()); $dateMs=[int64]([DateTimeOffset]::Parse("2026-06-30T11:48:00Z").ToUnixTimeMilliseconds()); $canon="timestamp:$ts;recvWindow:5000;limit:50;date:$dateMs;"; $h=New-Object System.Security.Cryptography.HMACSHA512; $h.Key=[Text.Encoding]::UTF8.GetBytes($secret); $sig=-join($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($canon))|%{$_.ToString('x2')}); $headers=@{'Api_Key'='<SHARED_KEY>';'X-Api-Key'='<ACCOUNT_ID>';'Signature'=$sig;'Content-Type'='application/json'}; Invoke-RestMethod -Uri "https://market.neuralgeneration.com/api/ex-buyers/trades/updates?timestamp=$ts&recvWindow=5000&limit=50&date=$dateMs" -Headers $headers -Method Get | ConvertTo-Json -Depth 6
```
Секрет — в ОДИНАРНЫХ кавычках (в нём есть `$`). Порядок в `$canon` = порядок в URL, иначе `120 INVALID_SIGNATURE`.

---

## РЕКОМЕНДАЦИИ ПЕРЕД ЗАПУСКОМ В ПРОД

### Критично (ломает реальную доставку/деньги)
1. **`/delivery` шаг 3 «телепортируйтесь» → Error 773.** Заменить на «профиль бота → Join». (`api/__init__.py` ~599)
2. **Таймер `5:00` нереалистичен** → 8–10 мин (заявка+заход+обмен), синхронно с 10-мин окном friendship. (`api/__init__.py` ~594/602/606)
3. **`/delivery` шлёт friendship без гейта по `starpets_status`** → 400-спам после принятия. Добавить гейт `∈ {None,0}` как в мониторе. (`api/__init__.py` ~547)
4. **failed-страница врёт** «деньги вернутся автоматически», а возврата в коде НЕТ. Либо реализовать возврат, либо убрать текст. (`api/__init__.py` ~582)
5. **Нет авто-закрытия** (терминала нет). Обход: эвристика в мониторе «дошёл до `5` + тишина N мин → `needs_attention`» (оператор закрывает в админке); «застрял <4 + 15 мин → needs_attention» (no-show). Авто-`done` пока опасен (5 не отличает успех от провала). Либо ждать фикс Мухаммада.

### Важно
6. **Возвраты покупателям** — реализовать `DELETE /api/trades/ex-buyers/withdrawal` (`reasonType`, есть `roblox_join_error_773` и др.). Сейчас руками через ggsel. «Отмена» в админке ставит только `failed`.
7. **Backoff ретраев не работает** — `pop_task` не фильтрует `scheduled_at`, 3 попытки `DELIVER` уходят подряд. (`task_runner.py`)
8. **Автозапуск SyncPrices** после рестарта (сейчас первый авто-прогон через 30 мин; хранить `last_sync_prices_at` в `kv_state`, запускать на старте если устарело).

### Безопасность (СРОЧНО — засветилось в чате)
9. **Проротировать `starpets_secret`, `Api_Key`/`account_id`, `ADMIN_PASSWORD`** — все светились открытым текстом в диалоге.
10. **Тест-эндпоинты утекают секреты:** `/test-trade-status` и `/test-friendship` возвращают `Api_Key`/`Signature` в ответе. Удалить/закрыть на проде. Прибрать прочие `/test-*` от публичного доступа.

### Чистка/проверка
11. Удалить мёртвые `get_trade_updates`/`get_items` (404-заглушки) из `app/clients/starpets.py`.
12. Закрыть вручную старые зависшие `dispatched`-заказы (их монитор не закроет).
13. После деплоя проверить: миграция `0010` создала `trade_events`; `/admin` открывается; аудит пишется на новом заказе; `/admin-auth-debug` уже удалён.

---

## Окружение (на заметку)
- Песочница Cowork периодически **усекает файлы** при записи через file-tools — правки надёжнее делать через bash/свой терминал; читать ground-truth через bash.
- `.git/index.lock` иногда застревает — удалить файл вручную перед коммитом.
- Railway: новые переменные применяются через **Promote → Shared** / редеплой, иначе не попадают в рантайм.

## Операционное состояние (проверить — могло устареть)
- ~13 626 офферов на паузе, ~5 активных; баланс токена ~$16; maintenance mode включён (глушит активацию офферов, не доставку).
- Заказы на скрине: 24/25 доставлены (SP статус 5), у 25 сработал redeliver-зонд `code=130` = предмет ушёл.
