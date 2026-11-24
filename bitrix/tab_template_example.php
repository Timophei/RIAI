<?php
/**
 * Пример шаблона вкладки (компонент-вкладка в карточке Закупки, как «Аналитическая справка»).
 * $purchaseId — ID текущей закупки, который ваш компонент уже получает.
 */
if (!defined('B_PROLOG_INCLUDED') || B_PROLOG_INCLUDED !== true) die();
$purchaseId = (int)$arResult['ENTITY_ID'];   // поправьте под ваш компонент
$box = 'riai-advice-' . $purchaseId;
?>
<div style="margin:0 0 8px">
    <button type="button" class="ui-btn ui-btn-light-border ui-btn-xs" id="<?= $box ?>-refresh">Обновить подсказки</button>
</div>
<div id="<?= $box ?>">Загружаю подсказки по истории закупок…</div>
<script>
(function () {
    var box = document.getElementById('<?= $box ?>');
    function load(refresh) {
        box.innerHTML = 'Загружаю подсказки по истории закупок…';
        BX.ajax({
            url: '/local/ajax/riai_advice.php?purchase=<?= $purchaseId ?>' + (refresh ? '&refresh=1' : ''),
            method: 'GET', dataType: 'html', timeout: 90,
            onsuccess: function (html) { box.innerHTML = html; },
            onfailure: function () { box.innerHTML = '<span style="color:#a0662b">Сервис подсказок недоступен</span>'; }
        });
    }
    document.getElementById('<?= $box ?>-refresh').onclick = function () { load(true); };
    load(false);
})();
</script>
