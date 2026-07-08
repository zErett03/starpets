# Гигиена кода: эндпоинты под чистку

Живой документ. По мере разработки помечаем, что можно удалять. Все не-публичные
эндпоинты живут в `app/api/__init__.py` (~2700 строк) и закрыты admin Basic Auth.
Публичны только `/`, `/delivery`, `/hooks/*`.

Легенда: 🔴 удалить · 🟡 удалить после того, как отслужит · 🟢 оставить (операционный/публичный)

---

## 🔴 Удалить — разовые тест/probe/диагностика (мусор)

Одноразовые пробы API, уже отслужили:

- `/test-sync`, `/test-sync-small`, `/test-products`, `/test-items`, `/test-top-item`
- `/test-buy`, `/test-trade`, `/test-trade-status`, `/test-friendship`
- `/test-categories`, `/test-categories-tree`, `/test-offers-format`, `/test-batch-pause`
- `/test-deliver-dryrun`, `/test-webhook`, `/test-starpets`
- `/ggsel-auth-probe`
- `/probe-image-sizes` — подбор размера картинки StarPets (done: только 110px)
- `/proto-cover`, `/proto-sku-card` — прототипы SKU (заменены боевой сборкой)
- `/debug-item-updates`, `/debug-options`
- `/myip` — тривиальная проба

## 🟡 Удалить после того, как отслужит

Ещё нужны в текущей работе над SKU / могут пригодиться до конца раскатки:

- `/probe-activate` — удалить, как только активация подтверждена (batch_activate async) ✅ отработал
- `/probe-matrix` — удалить после вердикта по матрице цен ✅ отработал (матрицы нет)
- `/probe-variant-patch`, `/probe-variant-update2`, `/probe-v2-put-option`, `/probe-options-bulk`,
  `/probe-upsert-correct`, `/probe-default-swap` — все отработали (нашли: обновление вариантов =
  `POST /variants` с id; смену дефолта делаем пересборкой опции). Удалить.
- `/sku-price-sync` — 🟢 ОСТАВИТЬ (ручной триггер Phase 3, полезно для отладки/ручного синка)
- `/sku-card-statuses` — оставить до конца раскатки SKU, потом удалить (или в 🟢, если удобно мониторить)
- `/reset-sku-cards` — нужен, пока меняется вёрстка/цены SKU; после стабилизации — удалить

## 🔴 Удалить — разовые миграции/фиксы (отработали)

- `/fix-consent-option`, `/add-consent-option`, `/remove-consent-option` — миграция consent-чекбокса
- `/fix-descriptions` — массовое обновление описаний (13 626 карточек, done)
- `/fix-order-username` — точечный фикс
- `/fix-paused-to-draft` — точечный фикс
- `/fix-post-payment-url` — точечный фикс

## 🟡 Миграции — возможно ещё понадобятся

- `/fix-webhooks` — перепрошивка вебхуков; нужна при ротации `WEBHOOK_SHARED_SECRET`
- `/create-offers`, `/create-all-offers` — массовое создание per-combo офферов (при заводе новых предметов)

---

## 🟢 Оставить — операционные инструменты SKU

- `/build-sku-card` — сборка одной SKU-карточки
- `/build-all-sku-cards` — массовая сборка партиями
- `/activate-sku-cards` — публикация драфтов SKU (batch_activate, async)
- `/cleanup-sku-card` — ретайр одной SKU-карточки (снять опцию + удалить строки)
- `/cleanup-sku-by-name` — чистка следов SKU по имени питомца (FK-safe)
- `/regenerate-cover` — перегенерация обложек (при смене стиля фона)
- `/sku-card-stock` — живое наличие по вариантам карточки
- `/sku-groups` — инспектор группировки (name × pumping)
- `/sync-sku-products` — наполнение каталога `sku_products` со StarPets

## 🟢 Оставить — операционные (доставка/цены/офферы)

- `/retry-delivery` — восстановление зависшего заказа (транзиентный сбой)
- `/close-order` — оператор закрывает подтверждённо-доставленный заказ
- `/trigger-deliver` — ручной запуск выдачи
- `/price-sync-once`, `/sync-prices` — ручной прогон синхронизации цен
- `/fast-forward-cursor` — перемотка курсора ценового фида
- `/seed-store-items` — сидирование store_items
- `/activate-batch`, `/activate-all-offers`, `/pause-all-offers` — управление статусом офферов
- `/retry-errors` — перезапуск офферов в статусе error

## 🟢 Оставить — чтение/мониторинг (низкий приоритет, можно проредить)

- `/system-status`, `/db-stats`, `/offer-errors`, `/cheapest-offers`
- `/check-offers-health`, `/order-info`
- `/debug-sku-order` — форензика заказа (полезно при инцидентах)

## 🟢 Оставить — публичные / критичные

- `/` — health
- `/delivery` — страница выдачи покупателю
- `/hooks/ggsel/precheck/{ggsel_offer_id}` — вебхук precheck
- `/hooks/ggsel/notification/{offer_id}` — вебхук notification

---

## Прочая гигиена (не эндпоинты)

- `app/clients/ggsel.py::activate_offers` — мёртвый метод (`/offers/batch/activate` → 404), удалить
- Разбить `app/api/__init__.py` (~2700 строк): вынести операционные SKU-эндпоинты в
  `app/api/sku.py`, диагностику в `app/api/diag.py`, оставив в `__init__.py` только
  публичное (`/`, `/delivery`) и сборку приложения
- Временные файлы в корне репо: `_cover_preview_*.png` (если остались) — удалить

---

## Итог (на момент создания)

Всего эндпоинтов: **66**. Под однозначное удаление (🔴): ~24. После раскатки (🟡): ~8.
Ядро (🟢 операционные + публичные): ~34. Основной «раздув» — именно 🔴-группа тестов
и отработавших миграций в `__init__.py`.
