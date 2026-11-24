<?php
/**
 * AJAX-точка вкладки: /local/ajax/riai_advice.php?purchase=23504[&refresh=1]
 * Собирает позиции закупки, спрашивает сервер RIAI, рисует ответ со ссылками.
 * Вкладка (ваш компонент) просто подгружает этот HTML после открытия — см. пример JS внизу файла.
 */
define('NO_KEEP_STATISTIC', true);
require $_SERVER['DOCUMENT_ROOT'] . '/bitrix/modules/main/include/prolog_before.php';

use Bitrix\Crm\Service\Container;
use Bitrix\Main\Loader;

const RIAI_PURCHASE_TYPE = 132;   // Закупки
const RIAI_REQUEST_TYPE  = 183;   // Запрос поставщику

header('Content-Type: text/html; charset=UTF-8');
global $USER;
$purchaseId = (int)($_GET['purchase'] ?? 0);
if (!$USER->IsAuthorized() || !Loader::includeModule('crm') || !Loader::includeModule('iblock') || $purchaseId <= 0) {
    http_response_code(403);
    die('Нет доступа');
}
if (!Container::getInstance()->getUserPermissions()->checkReadPermissions(RIAI_PURCHASE_TYPE, $purchaseId)) {
    http_response_code(403);
    die('Нет доступа к закупке');
}

/**
 * Позиции закупки: [['id'=>, 'name'=>, 'qty'=>, 'unit'=>], ...]
 * ЗАМЕНИТЕ на ту же выборку, что строит вкладку «Позиции закупок».
 * Ниже — пример: Закупка → её подзаявки (164) → позиции списка 15 по полю «Подзаявка» (PROPERTY_66).
 */
function riaiPurchasePositions(int $purchaseId): array
{
    $factory = Container::getInstance()->getFactory(164);
    $subIds = [];
    foreach ($factory->getItems(['select' => ['ID'], 'filter' => ['=PARENT_ID_' . RIAI_PURCHASE_TYPE => $purchaseId]]) as $item) {
        $subIds[] = $item->getId();
    }
    if (!$subIds) {
        return [];
    }
    $values = [];
    foreach ($subIds as $id) {                       // привязка к CRM может храниться в разных форматах
        $values[] = 'T' . dechex(164) . '_' . $id;
        $values[] = $id;
    }
    $res = CIBlockElement::GetList(['ID' => 'ASC'], ['IBLOCK_ID' => 15, 'PROPERTY_66' => $values], false, false,
        ['ID', 'NAME', 'PROPERTY_60', 'PROPERTY_89']);
    $out = [];
    while ($el = $res->Fetch()) {
        $out[$el['ID']] = ['id' => (int)$el['ID'], 'name' => $el['NAME'],
                           'qty' => $el['PROPERTY_60_VALUE'], 'unit' => $el['PROPERTY_89_VALUE']];
    }
    return array_values($out);
}

$positions = riaiPurchasePositions($purchaseId);
$r = RiaiAdvice::get($purchaseId, $positions, 8, !empty($_GET['refresh']));
if (empty($r['ok'])) {
    die('<div style="color:#a0662b">' . htmlspecialcharsbx($r['error'] ?? 'Ошибка') . '</div>');
}

// ---------- ссылки на сущности портала ----------
$h = fn($s) => htmlspecialcharsbx((string)$s);
$lnkCompany  = fn($id) => "/crm/company/details/{$id}/";
$lnkRequest  = fn($id) => '/crm/type/' . RIAI_REQUEST_TYPE . "/details/{$id}/";
$lnkPurchase = fn($id) => '/crm/type/' . RIAI_PURCHASE_TYPE . "/details/{$id}/";
$price = fn($p) => $p ? number_format($p, 0, ',', ' ') . ' ₽' : '—';
$offer = function ($o, $bold = false) use ($h, $lnkCompany, $lnkRequest, $lnkPurchase, $price) {
    if (!$o) return '—';
    $c = $o['company_id']
        ? '<a href="' . $lnkCompany($o['company_id']) . '" target="_blank"' . ($bold ? ' style="font-weight:600;color:#2b8a3e"' : '') . '>' . $h($o['company']) . '</a>'
        : $h($o['company'] ?: '—');
    $refs = [];
    if ($o['purchase_id']) $refs[] = '<a href="' . $lnkPurchase($o['purchase_id']) . '" target="_blank">закупка ' . (int)$o['purchase_id'] . '</a>';
    if ($o['request_id'])  $refs[] = '<a href="' . $lnkRequest($o['request_id']) . '" target="_blank">запрос ' . (int)$o['request_id'] . '</a>';
    return $c . ' ' . $price($o['price']) . ($o['selected'] ? ' ✓' : '')
        . ' <span style="color:#80868e">· ' . $h($o['date']) . ($refs ? ' · ' . implode(' · ', $refs) : '') . '</span>';
};
$num = [];
?>
<style>
.riai{font-size:13px;line-height:1.45;color:#1f2d3d}
.riai table{border-collapse:collapse;width:100%;background:#fff}
.riai th,.riai td{border-bottom:1px solid #e5e9ee;padding:6px 8px;text-align:left;vertical-align:top}
.riai th{background:#f5f7f9;font-weight:600;color:#525c69}
.riai h3{font-size:15px;margin:18px 0 8px}
.riai details summary{cursor:pointer;color:#2067b0}
.riai .note{background:#f5f7f9;padding:8px 10px;border-radius:4px;margin-bottom:10px;color:#525c69}
</style>
<div class="riai">
<div class="note">Подсказки по истории закупок, данные на <?= $h($r['updated']) ?>. ✓ — предложение было выбрано. Цены — за единицу из прошлой закупки.</div>
<h3>Позиции</h3>
<table>
<tr><th>№</th><th>Позиция</th><th>Кол-во</th><th>Последний раз выбрали</th><th>Лучшая цена в истории</th></tr>
<?php foreach ($r['positions'] as $i => $p): $num[$p['id']] = $i + 1; ?>
<tr>
  <td><?= $i + 1 ?></td>
  <td><?= $h($p['name']) ?>
    <?php if ($p['matches']): ?>
    <details><summary>похожие позиции в истории: <?= count($p['matches']) ?></summary>
      <table>
      <tr><th>Похож.</th><th>Позиция</th><th>Предложения</th></tr>
      <?php foreach ($p['matches'] as $m): ?>
      <tr><td><?= $h($m['score']) ?></td>
          <td><?= $h($m['name']) ?> <span style="color:#80868e">(<?= $h($m['qty']) ?> <?= $h($m['unit']) ?>)</span></td>
          <td><?= implode('<br>', array_map(fn($o) => $offer($o), $m['offers'])) ?></td></tr>
      <?php endforeach; ?>
      </table>
    </details>
    <?php endif; ?>
  </td>
  <td><?= $h($p['qty']) ?> <?= $h($p['unit']) ?></td>
  <td><?= $p['last_selected'] ? $offer($p['last_selected'], true)
        : ($p['status'] === 'new' ? '<span style="color:#a0662b">не закупали</span>' : '<span style="color:#80868e">запрашивали, без выбора</span>') ?></td>
  <td><?= $offer($p['best_price']) ?></td>
</tr>
<?php endforeach; ?>
</table>

<h3>Кому отправить запросы</h3>
<?php if (!$r['suppliers']): ?>
<div class="note">По истории предложить некого — позиции новые.</div>
<?php else: ?>
<table>
<tr><th>Поставщик</th><th>По позициям</th><th>Предложений</th><th>Выбирали</th><th>Последнее предложение</th></tr>
<?php foreach ($r['suppliers'] as $s): ?>
<tr>
  <td><a href="<?= $lnkCompany($s['company_id']) ?>" target="_blank"><?= $h($s['company']) ?></a></td>
  <td><?= implode(', ', array_map(fn($id) => '№' . ($num[$id] ?? $id), $s['position_ids'])) ?></td>
  <td><?= (int)$s['offers'] ?></td>
  <td><?= (int)$s['wins'] ?></td>
  <td><?= $offer($s['last']) ?></td>
</tr>
<?php endforeach; ?>
</table>
<?php endif; ?>
</div>
<?php
/* ---------- Во вкладке-компоненте (шаблон) достаточно такого кода ----------
<button type="button" class="ui-btn ui-btn-light-border ui-btn-xs" onclick="riaiLoad(true)">Обновить подсказки</button>
<div id="riai-box">Загружаю подсказки…</div>
<script>
function riaiLoad(refresh) {
  var box = document.getElementById('riai-box');
  box.innerHTML = 'Загружаю подсказки…';
  BX.ajax({url: '/local/ajax/riai_advice.php?purchase=<?= (int)$purchaseId ?>' + (refresh ? '&refresh=1' : ''),
           method: 'GET', dataType: 'html', timeout: 120,
           onsuccess: function (html) { box.innerHTML = html; },
           onfailure: function () { box.innerHTML = 'Сервис подсказок недоступен'; }});
}
riaiLoad(false);
</script>
*/
