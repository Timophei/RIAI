# RIAI — помощник снабжения для Битрикс24

Подсказки по позициям закупки: у кого уже брали, по какой цене, кто был конкурентом и кому отправить запрос.
Работает на отдельном сервере, с порталом общается через входящий вебхук.

## Этап 1 (сейчас): данные и отчёт

| Команда | Что делает |
|---|---|
| `discover` | Находит ID списков «Позиция» и «Позиция предложения», смарт-процессов «Запрос поставщику» и «Закупка», сопоставляет поля по названиям → `data/config.json` |
| `sync` | Забирает изменения с прошлого запуска (`sync --full` — всё заново) → `data/supply.db` (SQLite) |
| `report [позиции…]` | Отчёт по позициям → `data/report_*.md` |

### Запуск на сервере

```bash
git clone https://github.com/Timophei/RIAI.git /opt/riai && cd /opt/riai
cp .env.example .env && nano .env        # вставить B24_WEBHOOK
docker compose build
docker compose run --rm app discover     # проверить, что поля найдены верно
nohup docker compose run --rm -T app sync --full > data/sync_full.log 2>&1 &   # первая загрузка, в фоне
tail -f data/sync_full.log               # ход загрузки (Ctrl+C — выйти из просмотра, загрузка продолжится)
docker compose run --rm app report       # отчёт по 4 позициям из примера
docker compose run --rm app report "Электроды УОНИ 13/55 4мм" "Перчатки х/б"
```

Если `discover` ошибся с полем или списком — поправьте `data/config.json` вручную
(или задайте ID в `.env`) и запустите `sync --full`.

Загрузка идёт окнами по 500 записей и сразу пишет их в базу (≈35 МБ памяти).
Если она прервалась — просто запустите `sync --full` ещё раз: продолжит с места остановки.
Начать заново: `sync --restart`. Контейнер ограничен 700 МБ памяти и 1 ядром (`RIAI_MEM` в `.env`).

### Синхронизация по расписанию

```cron
0 * * * * cd /opt/riai && docker compose run --rm app sync >> data/sync.log 2>&1
```

Раз в час подтягиваются только изменённые записи (по `TIMESTAMP_X` / `updatedTime`).
Если фильтр по дате на портале не сработает, скрипт сам скачает всё целиком.

### Вебхук

Права: `crm`, `lists`. Создайте его от отдельного технического пользователя с доступом только к снабжению.
Вебхук хранится только в `.env` на сервере; `.env` и `data/` в git не попадают.

## Этап 2: API подсказок + вкладка-компонент на портале

Портал собирает позиции закупки сам и отправляет их серверу; сервер возвращает историю с ID —
ссылки на поставщиков, запросы и закупки строит портал.

```
вкладка (компонент) → /local/ajax/riai_advice.php (права на закупку, сбор позиций, отрисовка)
                     → POST https://b-integration.ru/supply-ai/api/advice  (X-Api-Key)
```

Запрос: `{"purchase_id": 23504, "positions": [{"id": 1, "name": "Соль техническая", "qty": 100, "unit": "кг"}]}`
Ответ (по каждой позиции): `status` (found / asked / new), `last_selected`, `best_price`,
`matches[]` — похожие позиции истории со всеми предложениями. Предложение:
`{offer_id, position_id, position, company_id, company, request_id, purchase_id, price, selected, date}`.
Плюс `suppliers[]` — кому отправить запросы (`company_id`, по каким позициям, сколько раз выбирали, последнее предложение).
Предложения текущей закупки и сами переданные позиции в историю не попадают. Полный контракт — в `bitrix/RiaiAdvice.php`.

### Сервер
```bash
git pull && docker compose build
echo "API_KEY=$(openssl rand -hex 16)" >> .env
docker compose up -d web
curl http://127.0.0.1:8090/        # «RIAI: сервис подсказок снабжения работает»
```
nginx (в `server { … }` сайта b-integration.ru, тот, что слушает 443):
```nginx
location /supply-ai/ {
    proxy_pass http://127.0.0.1:8090/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 120s;
}
```
Проверка в браузере: `https://b-integration.ru/supply-ai/preview?key=<API_KEY>&q=Соль техническая&q=Швеллер 20`

### Портал (папка `bitrix/`)
| Файл из репозитория | Куда на портале |
|---|---|
| `bitrix/RiaiAdvice.php` | `/local/php_interface/lib/RiaiAdvice.php` |
| `bitrix/init_riai.php` | `/local/php_interface/init_riai.php` + строка `require_once __DIR__ . '/init_riai.php';` в `init.php` |
| `bitrix/riai_advice_ajax.php` | `/local/ajax/riai_advice.php` |

Ключ: «Настройки → Инструменты → Командная PHP-строка»:
`\Bitrix\Main\Config\Option::set('main', 'riai_api_key', 'API_KEY');`

`init_riai.php` добавляет вкладку «Подсказки снабжения» в карточку Закупки (событие `crm:onEntityDetailsTabsInitialized`),
вкладка при открытии грузит `/local/ajax/riai_advice.php?purchase=ID`.
Диагностика: `/local/ajax/riai_advice.php?purchase=ID&debug=1` — какие подзаявки и позиции нашлись.

## Этап 3: дерево типов номенклатуры (модель OpenAI или совместимая)

В `.env`:
```
LLM_API_KEY=sk-...
LLM_MODEL=gpt-4o-mini            # недорогая модель для разметки
# LLM_BASE_URL=https://api.openai.com/v1   # для GigaChat/другого провайдера — свой адрес
# LLM_PROXY=http://user:pass@host:port     # если провайдер недоступен напрямую
```
```bash
docker compose run --rm app llm-check            # модель отвечает?
docker compose run --rm app taxonomy-propose     # модель предлагает дерево → data/taxonomy.txt
nano data/taxonomy.txt                            # поправить: # Раздел / ## Группа / - Тип
docker compose run --rm app taxonomy-classify    # разложить все названия истории (продолжает после обрыва)
docker compose restart web
```
Что даёт: находки другого типа отсеиваются (патрон для ламп ≠ патрон для перфоратора), у каждой позиции —
«Аналоги того же типа» (кто возил другие марки), а если по самой позиции поставщиков мало — аналоги попадают в матрицу.
Новые названия раскладываются при первом открытии вкладки и ночью:
```cron
0 * * * * cd /opt/riai && docker compose run --rm app sync >> data/sync.log 2>&1 && docker compose run --rm app taxonomy-classify >> data/sync.log 2>&1
```

## Дальше
- Кнопка «Создать запросы»: по запросу на поставщика + «Позиции предложения» с пустой ценой.
- Разбор параметров (диаметр, объём, марка) и проверка совпадений моделью: «то же / аналог / не то».
- Приведение цен к одной единице через вес.
