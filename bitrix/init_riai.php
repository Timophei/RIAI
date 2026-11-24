<?php
/**
 * RIAI: вкладка «Подсказки снабжения» в карточке Закупки.
 * Вставить в конец /local/php_interface/init.php (или подключить этот файл оттуда:
 *   require_once __DIR__ . '/init_riai.php';  — тогда положите его в /local/php_interface/)
 */
require_once __DIR__ . '/lib/RiaiAdvice.php';

\Bitrix\Main\EventManager::getInstance()->addEventHandler(
    'crm',
    'onEntityDetailsTabsInitialized',
    function (\Bitrix\Main\Event $event) {
        $tabs = $event->getParameter('tabs');
        $entityTypeId = (int)$event->getParameter('entityTypeID');
        $entityId = (int)$event->getParameter('entityID');

        if ($entityTypeId === 132 && $entityId > 0) {   // 132 — смарт-процесс «Закупки»
            $tabs[] = [
                'id'     => 'tab_riai_advice',
                'name'   => 'Подсказки снабжения',
                'loader' => [
                    // Битрикс загрузит этот адрес, когда пользователь откроет вкладку
                    'serviceUrl'    => '/local/ajax/riai_advice.php?purchase=' . $entityId,
                    'componentData' => ['template' => ''],
                ],
            ];
        }
        return new \Bitrix\Main\EventResult(\Bitrix\Main\EventResult::SUCCESS, ['tabs' => $tabs]);
    }
);
