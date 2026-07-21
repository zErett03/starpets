/**
 * Приём заказов из сервисов в общую Google-таблицу.
 *
 * Ставится ОДИН РАЗ в таблицу: Расширения → Apps Script → вставить этот код →
 * Развернуть → Новое развёртывание → тип «Веб-приложение»:
 *     Запуск от имени: Я
 *     Доступ:          Все, у кого есть ссылка
 * Полученный URL идёт в переменную SHEETS_WEBHOOK_URL всех трёх сервисов — скрипт
 * общий, а лист каждый сервис указывает сам в поле `sheet`.
 *
 * ПОЧЕМУ РАЗНЫЕ ЛИСТЫ, А НЕ ОДИН ОБЩИЙ: у каждого проекта свой баланс закупа
 * (StarPets Adopt Me, StarPets MM2, dd373). Общий лист смешал бы расходы с разных
 * балансов, и сверить выгрузку с реальным счётом стало бы невозможно.
 *
 * ВАЖНО про доступ «Все, у кого есть ссылка»: любой, кто узнает URL, сможет писать
 * в таблицу. Поэтому запросы проверяются по токену — задай его ниже и продублируй
 * в переменной SHEETS_WEBHOOK_TOKEN на стороне сервисов.
 *
 * Строки обновляются ПО НОМЕРУ ЗАКАЗА: повторная отправка того же заказа не создаёт
 * дубль, а перезаписывает строку. Благодаря этому сервис может слать последние N дней
 * целиком и не хранить отметку «выгружено» — статусы заказов всё равно меняются
 * задним числом, когда доставка закрывается.
 *
 * ЭТО КОПИЯ. Файл одинаков во всех проектах, чтобы код лежал рядом с сервисом,
 * который его использует. В саму таблицу вставляется только одна копия.
 */

const TOKEN = 'ЗАМЕНИ_НА_СВОЙ_ДЛИННЫЙ_ТОКЕН';

// Белый список листов. Сервис присылает имя листа сам, и без этой проверки опечатка
// или чужой запрос создали бы мусорный лист в таблице.
const ALLOWED_SHEETS = ['Заказы AM', 'Заказы MM2', 'Заказы PoE2'];

// Куда писать, если сервис не прислал имя листа (старая версия сервиса).
const DEFAULT_SHEET = 'Заказы AM';

// Строка-образец, с которой копируются форматы и выпадающий список статусов.
// Скрипт не наследует оформление сам: без этого новые строки приходят голыми числами
// вроде 0.1428927611 вместо 14.3%, и без знаков валют.
const TEMPLATE_ROW = 2;

// Сколько колонок занимает строка заказа (A..M).
const COLS = 13;

function doPost(e) {
  // Три сервиса шлют выгрузки по своему расписанию и могут совпасть по времени.
  // Без блокировки два одновременных вызова прочитали бы один и тот же getLastRow()
  // и записали заказы поверх друг друга.
  const lock = LockService.getScriptLock();
  try {
    lock.waitLock(30000);
  } catch (err) {
    return _json({ ok: false, error: 'занято другой выгрузкой, повтори позже' });
  }

  try {
    const payload = JSON.parse(e.postData.contents);

    if (payload.token !== TOKEN) {
      return _json({ ok: false, error: 'bad token' });
    }

    const sheetName = payload.sheet || DEFAULT_SHEET;
    if (ALLOWED_SHEETS.indexOf(sheetName) < 0) {
      return _json({ ok: false, error: 'лист не разрешён: ' + sheetName });
    }

    const sheet = SpreadsheetApp.getActive().getSheetByName(sheetName);
    if (!sheet) {
      return _json({ ok: false, error: 'нет листа ' + sheetName });
    }

    const orders = payload.orders || [];
    let added = 0, updated = 0;

    // Карта «номер заказа → номер строки», чтобы не искать каждый раз заново.
    const lastRow = sheet.getLastRow();
    const index = {};
    if (lastRow > 1) {
      const ids = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
      for (let i = 0; i < ids.length; i++) {
        const v = ids[i][0];
        if (v !== '' && v !== null) index[String(v)] = i + 2;
      }
    }

    orders.forEach(function (o) {
      const key = String(o.ggsel_order_id);
      let row = index[key];
      let isNew = false;
      if (!row) {
        row = sheet.getLastRow() + 1;
        index[key] = row;
        added++;
        isNew = true;
      } else {
        updated++;
      }
      _writeRow(sheet, row, o, isNew);
    });

    return _json({ ok: true, sheet: sheetName, added: added, updated: updated });
  } catch (err) {
    return _json({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

/**
 * Пишет исходные данные и восстанавливает формулы.
 *
 * Считаемые колонки (G–L) задаются формулами, а не готовыми числами: тогда правка
 * комиссии или курса на листе «Параметры» пересчитывает всю историю, как в исходной
 * ручной таблице. Лист «Параметры» общий для всех сервисов.
 */
function _writeRow(sheet, row, o, isNew) {
  // Новой строке сначала переносим оформление образца: денежные форматы, проценты
  // и выпадающий список в колонке статуса. Существующие строки не трогаем — вдруг
  // ты поправил формат вручную.
  if (isNew && row > TEMPLATE_ROW) {
    const tpl = sheet.getRange(TEMPLATE_ROW, 1, 1, COLS);
    const dst = sheet.getRange(row, 1, 1, COLS);
    tpl.copyTo(dst, SpreadsheetApp.CopyPasteType.PASTE_FORMAT, false);
    tpl.copyTo(dst, SpreadsheetApp.CopyPasteType.PASTE_DATA_VALIDATION, false);
  }

  sheet.getRange(row, 1, 1, 6).setValues([[
    o.ggsel_order_id,
    o.item_name,
    o.sale_rub,
    o.usd_rub,
    o.cost_usd,
    o.status
  ]]);

  sheet.getRange(row, 7, 1, 6).setFormulas([[
    '=E' + row + '*D' + row,
    "=C" + row + "*'Параметры'!$B$4",
    "=C" + row + "*'Параметры'!$B$5",
    '=C' + row + '-H' + row + '-I' + row,
    '=IF($F' + row + '="Возврат",-(H' + row + '+I' + row + '),J' + row + '-G' + row + ')',
    '=IF(C' + row + '=0,0,K' + row + '/C' + row + ')'
  ]]);

  sheet.getRange(row, 13).setValue(o.paid_at || '');
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

/** Проверка руками: запусти из редактора, в листе появится тестовая строка. */
function testWrite() {
  const sheet = SpreadsheetApp.getActive().getSheetByName(DEFAULT_SHEET);
  _writeRow(sheet, sheet.getLastRow() + 1, {
    ggsel_order_id: 999999999,
    item_name: 'ТЕСТ — удалить строку',
    sale_rub: 100,
    usd_rub: 78.5,
    cost_usd: 0.9,
    status: 'В работе',
    paid_at: '01.01.2026 00:00'
  }, true);
}
