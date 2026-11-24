<?php
/**
 * Клиент API подсказок снабжения (RIAI).
 * Положить: /local/php_interface/lib/RiaiAdvice.php, подключить в init.php:
 *   require_once __DIR__ . '/lib/RiaiAdvice.php';
 * Настройки (один раз, командная PHP-строка админки):
 *   \Bitrix\Main\Config\Option::set('main', 'riai_api_url', 'https://b-integration.ru/supply-ai/api/advice');
 *   \Bitrix\Main\Config\Option::set('main', 'riai_api_key', 'API_KEY из .env на сервере RIAI');
 *
 * Контракт:
 *   запрос:  {purchase_id, positions:[{id, name, qty, unit, weight?}], matches?:8, refresh?:false}
 *            weight — «Вес за ед.» (кг), помогает пересчитать цену в ₽/кг
 *   ответ:   {ok, updated, purchase_id,
 *             positions:[{id, name, qty, unit, base_unit, status: found|asked|new,
 *                         last_selected: OFFER|null, best_price: OFFER|null,
 *                         matches:[{position_id, name, qty, unit, score, date, purchase_ids:[], offers:[OFFER...]}]}],
 *             suppliers:[{company_id, company, position_ids:[], offers, wins, last: OFFER|null}]}
 *   OFFER:   {offer_id, position_id, position, company_id, company, request_id, purchase_id,
 *             price, unit, unit_price, base_unit, comparable, selected, date}
 *            price — за единицу той позиции (unit); unit_price — за базовую единицу (base_unit: кг/л/м/…);
 *            comparable — та же базовая единица, что у запрошенной позиции; best_price — только среди comparable
 *   Предложения текущей закупки и переданные позиции в историю не попадают.
 */

use Bitrix\Main\Config\Option;
use Bitrix\Main\Web\HttpClient;
use Bitrix\Main\Web\Json;

class RiaiAdvice
{
    public static function get(int $purchaseId, array $positions, int $matches = 8, bool $refresh = false): array
    {
        if (!$positions) {
            return ['ok' => false, 'error' => 'В закупке нет позиций'];
        }
        $url = Option::get('main', 'riai_api_url', 'https://b-integration.ru/supply-ai/api/advice');
        $key = Option::get('main', 'riai_api_key', '');
        if ($key === '') {
            return ['ok' => false, 'error' => 'Не задан ключ riai_api_key'];
        }
        $http = new HttpClient(['socketTimeout' => 10, 'streamTimeout' => 90]);
        $http->setHeader('Content-Type', 'application/json');
        $http->setHeader('X-Api-Key', $key);
        $body = $http->post($url, Json::encode([
            'purchase_id' => $purchaseId,
            'positions'   => array_values($positions),
            'matches'     => $matches,
            'refresh'     => $refresh,
        ]));
        if ($body === false || $http->getStatus() != 200) {
            return ['ok' => false, 'error' => 'Сервис подсказок недоступен (HTTP ' . $http->getStatus() . ')'];
        }
        try {
            return Json::decode($body);
        } catch (\Throwable $e) {
            return ['ok' => false, 'error' => 'Некорректный ответ сервиса подсказок'];
        }
    }
}
