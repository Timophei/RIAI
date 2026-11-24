<?php
/**
 * AJAX-точка для вкладки: /local/ajax/riai_advice.php?purchase=23504[&refresh=1]
 * Отдаёт HTML-фрагмент подсказок. Карточка не ждёт сервис — вкладка грузит его после открытия.
 */
define('NO_KEEP_STATISTIC', true);
define('NOT_CHECK_PERMISSIONS', false);
require $_SERVER['DOCUMENT_ROOT'] . '/bitrix/modules/main/include/prolog_before.php';

use Bitrix\Crm\Service\Container;
use Bitrix\Main\Loader;

const RIAI_PURCHASE_TYPE = 132; // смарт-процесс «Закупки»

header('Content-Type: text/html; charset=UTF-8');
global $USER;
$purchaseId = (int)($_GET['purchase'] ?? 0);

if (!$USER->IsAuthorized() || !Loader::includeModule('crm') || $purchaseId <= 0) {
    http_response_code(403);
    die('Нет доступа');
}
// Только те, кто может читать эту закупку
if (!Container::getInstance()->getUserPermissions()->checkReadPermissions(RIAI_PURCHASE_TYPE, $purchaseId)) {
    http_response_code(403);
    die('Нет доступа к закупке');
}

// Вариант 1 (по умолчанию): сервер сам находит позиции закупки через подзаявки.
// Вариант 2: если у вас уже есть функция, собирающая позиции для вкладки «Позиции закупок»,
//            передайте их: $positions = [['id'=>..., 'name'=>..., 'qty'=>..., 'unit'=>...], ...];
$positions = [];

$res = RiaiAdvice::get($purchaseId, $positions, 'html', !empty($_GET['refresh']));
echo $res['ok'] ? $res['html'] : '<div style="color:#a0662b">' . htmlspecialcharsbx($res['error'] ?? 'Ошибка') . '</div>';
