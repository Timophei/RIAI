<?php
/**
 * Клиент API подсказок снабжения (RIAI).
 * Положить: /local/php_interface/lib/RiaiAdvice.php и подключить в init.php:
 *   require_once __DIR__ . '/lib/RiaiAdvice.php';
 *
 * Настройки (один раз, например в командной PHP-строке админки):
 *   \Bitrix\Main\Config\Option::set('main', 'riai_api_url', 'https://b-integration.ru/supply-ai/api/advice');
 *   \Bitrix\Main\Config\Option::set('main', 'riai_api_key', 'КЛЮЧ_ИЗ_.env_API_KEY');
 */

use Bitrix\Main\Config\Option;
use Bitrix\Main\Web\HttpClient;
use Bitrix\Main\Web\Json;

class RiaiAdvice
{
    /**
     * @param int   $purchaseId ID элемента смарт-процесса «Закупки»
     * @param array $positions  необязательно: [['id'=>..,'name'=>..,'qty'=>..,'unit'=>..], ...]
     *                          если пусто — сервер сам найдёт позиции через подзаявки закупки
     * @param string $format    'html' | 'json' | 'both'
     * @return array ['ok'=>bool, 'html'=>string, 'positions'=>[...], 'suppliers'=>[...], 'error'=>string]
     */
    public static function get(int $purchaseId, array $positions = [], string $format = 'html', bool $refresh = false): array
    {
        $url = Option::get('main', 'riai_api_url', 'https://b-integration.ru/supply-ai/api/advice');
        $key = Option::get('main', 'riai_api_key', '');
        if ($key === '') {
            return ['ok' => false, 'error' => 'Не задан ключ riai_api_key'];
        }
        $http = new HttpClient(['socketTimeout' => 10, 'streamTimeout' => 60]);
        $http->setHeader('Content-Type', 'application/json');
        $http->setHeader('X-Api-Key', $key);
        $body = $http->post($url, Json::encode([
            'purchase_id' => $purchaseId,
            'positions'   => $positions,
            'format'      => $format,
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
