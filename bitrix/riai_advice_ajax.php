<?php
/**
 * Содержимое вкладки «Подсказки снабжения»: /local/ajax/riai_advice.php?purchase=23504
 *   &refresh=1 — пересчитать без кэша;  &debug=1 — диагностика (какие подзаявки и позиции нашлись)
 * Вкладку в карточку добавляет обработчик из init_riai.php — он и вызывает этот файл.
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
        ['ID', 'NAME', 'PROPERTY_60', 'PROPERTY_89', 'PROPERTY_111']);
    $out = [];
    while ($el = $res->Fetch()) {
        $out[$el['ID']] = ['id' => (int)$el['ID'], 'name' => $el['NAME'],
                           'qty' => $el['PROPERTY_60_VALUE'], 'unit' => $el['PROPERTY_89_VALUE'],
                           'weight' => $el['PROPERTY_111_VALUE']];   // «Вес за ед.» — для пересчёта в ₽/кг
    }
    return array_values($out);
}

$positions = riaiPurchasePositions($purchaseId);

if (!empty($_GET['debug'])) {   // диагностика: что нашлось на каждом шаге
    echo '<pre style="font-size:12px">';
    $factory = Container::getInstance()->getFactory(164);
    $subs = $factory->getItems(['select' => ['ID', 'TITLE'], 'filter' => ['=PARENT_ID_' . RIAI_PURCHASE_TYPE => $purchaseId]]);
    echo "Подзаявки закупки {$purchaseId} (по связи «родитель»): " . count($subs) . "\n";
    foreach ($subs as $it) echo '  ' . $it->getId() . ' ' . $it->getTitle() . "\n";
    echo "\nПоля-привязки подзаявок:\n";
    foreach ($factory->getFieldsCollection() as $f) {
        if (in_array($f->getType(), ['crm', 'crm_entity'], true) || strpos($f->getName(), 'PARENT_ID_') === 0) {
            echo '  ' . $f->getName() . ' (' . $f->getType() . ') ' . $f->getTitle() . "\n";
        }
    }
    echo "\nНайдено позиций: " . count($positions) . "\n";
    foreach (array_slice($positions, 0, 10) as $p) echo '  ' . $p['id'] . ' ' . $p['name'] . ' — ' . $p['qty'] . ' ' . $p['unit'] . "\n";
    echo "\nКак хранится «Подзаявка» (PROPERTY_66) у последних позиций списка 15:\n";
    $rs = CIBlockElement::GetList(['ID' => 'DESC'], ['IBLOCK_ID' => 15, '!PROPERTY_66' => false], false, ['nTopCount' => 5], ['ID', 'NAME', 'PROPERTY_66']);
    while ($el = $rs->Fetch()) echo '  ' . $el['ID'] . ' ' . $el['NAME'] . ' → «' . $el['PROPERTY_66_VALUE'] . "»\n";
    echo '</pre>';
    die();
}
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
    // цена за единицу той позиции + пересчёт за базовую (кг/л/м), если единицы отличаются
    $pr = $price($o['price']) . ($o['price'] && $o['unit'] ? '/' . $h($o['unit']) : '');
    if ($o['unit_price'] && $o['base_unit'] && $o['base_unit'] !== rtrim((string)$o['unit'], '.')) {
        $pr .= ' <span style="color:#80868e">≈ ' . number_format($o['unit_price'], 2, ',', ' ') . ' ₽/' . $h($o['base_unit']) . '</span>';
    }
    if (empty($o['comparable'])) {   // другая единица — цену напрямую не сравнивать
        $pr = '<span style="color:#80868e" title="Другая единица измерения — цены напрямую не сравнивать">' . $pr . '</span>';
    }
    return $c . ' ' . $pr . ($o['selected'] ? ' ✓' : '')
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
<div class="riai" id="riai-box-<?= $purchaseId ?>">
<div style="margin-bottom:8px">
  <button type="button" class="ui-btn ui-btn-light-border ui-btn-xs" onclick="(function(b){
      var box=document.getElementById('riai-box-<?= $purchaseId ?>'); b.disabled=true; b.textContent='Обновляю…';
      BX.ajax({url:'/local/ajax/riai_advice.php?purchase=<?= $purchaseId ?>&refresh=1', method:'GET', dataType:'html', timeout:120,
        onsuccess:function(h){ BX.html(box.parentNode, h); }, onfailure:function(){ b.disabled=false; b.textContent='Ошибка, ещё раз'; }});
  })(this)">Обновить подсказки</button>
</div>
<div class="note">Подсказки по истории закупок, данные на <?= $h($r['updated']) ?>. ✓ — предложение было выбрано.
«≈ … ₽/кг» — цена, пересчитанная по фасовке; серым — другая единица, напрямую не сравнивать.
«Лучшая цена» считается только среди сопоставимых единиц.</div>
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
<div class="note">Порядок: <?= $h($r['sort']['suppliers'] ?? '') ?>. Нажмите на заголовок столбца, чтобы пересортировать.
«Цена» — насколько поставщик дороже лучшей цены по тем же позициям (1,00 — самый дешёвый, 1,25 — на 25% дороже).</div>
<table class="riai-sort">
<tr>
  <th onclick="riaiSort(this,'text',true)">Поставщик</th>
  <th onclick="riaiSort(this,'num')">Позиции</th>
  <th onclick="riaiSort(this,'num')">Выбирали</th>
  <th onclick="riaiSort(this,'num')">Предложений</th>
  <th onclick="riaiSort(this,'num',true)">Цена</th>
  <th onclick="riaiSort(this,'num')">Последнее предложение</th>
  <th onclick="riaiSort(this,'num')">Закупки</th>
</tr>
<?php foreach ($r['suppliers'] as $s):
    $d = $s['last_date'] ? (int)implode('', array_reverse(explode('.', $s['last_date']))) : 0; ?>
<tr>
  <td data-v="<?= $h($s['company']) ?>"><a href="<?= $lnkCompany($s['company_id']) ?>" target="_blank"><?= $h($s['company']) ?></a></td>
  <td data-v="<?= count($s['position_ids']) ?>"><?= implode(', ', array_map(fn($id) => '№' . ($num[$id] ?? $id), $s['position_ids'])) ?></td>
  <td data-v="<?= (int)$s['wins'] ?>"><?= (int)$s['wins'] ?></td>
  <td data-v="<?= (int)$s['offers'] ?>"><?= (int)$s['offers'] ?></td>
  <td data-v="<?= $s['price_index'] ?? 999 ?>"><?= $s['price_index'] !== null
        ? ($s['price_index'] <= 1.0 ? '<b style="color:#2b8a3e">лучшая</b>' : number_format($s['price_index'], 2, ',', ' '))
        : '<span style="color:#80868e">нет цены</span>' ?></td>
  <td data-v="<?= $d ?>"><?= $s['last'] ? ($s['last']['price'] ? $offer($s['last'])
        : '<span style="color:#80868e">без цены · ' . $h($s['last']['date']) . '</span>'
          . ($s['last']['request_id'] ? ' · <a href="' . $lnkRequest($s['last']['request_id']) . '" target="_blank">запрос ' . (int)$s['last']['request_id'] . '</a>' : ''))
        : '—' ?></td>
  <td data-v="<?= count($s['purchase_ids']) ?>"><?= implode(', ', array_map(
        fn($id) => '<a href="' . $lnkPurchase($id) . '" target="_blank">' . (int)$id . '</a>', array_slice($s['purchase_ids'], 0, 5)))
        . (count($s['purchase_ids']) > 5 ? ' …' : '') ?></td>
</tr>
<?php endforeach; ?>
</table>
<?php endif; ?>

<?php if (!empty($r['purchases'])): ?>
<h3>Похожие прошлые закупки</h3>
<div class="note">Закупки, где уже были эти позиции. Порядок: <?= $h($r['sort']['purchases'] ?? '') ?>.
Если в одной прошлой закупке совпало несколько позиций — это сильный аналог, её поставщики — первые кандидаты.</div>
<table>
<tr><th>Закупка</th><th>Дата</th><th>Совпали позиции</th><th>Поставщики (✓ — выбран)</th></tr>
<?php foreach ($r['purchases'] as $g): ?>
<tr>
  <td><a href="<?= $lnkPurchase($g['purchase_id']) ?>" target="_blank">закупка <?= (int)$g['purchase_id'] ?></a></td>
  <td><?= $h($g['date']) ?></td>
  <td><?= implode('<br>', array_map(fn($id) => '№' . ($num[$id] ?? $id) . ' <span style="color:#80868e">как «'
        . $h(implode('», «', $g['matched'][$id] ?? [])) . '»</span>', $g['position_ids'])) ?></td>
  <td><?= implode('<br>', array_map(function ($x) use ($h, $lnkCompany, $price, $num) {
        $name = $x['company_id'] ? '<a href="' . $lnkCompany($x['company_id']) . '" target="_blank">' . $h($x['company']) . '</a>' : $h($x['company'] ?: '—');
        $ps = array_map(fn($o) => '№' . ($num[$o['for_position_id']] ?? '') . ' ' . $price($o['price'])
            . ($o['price'] && $o['unit'] ? '/' . $h($o['unit']) : '') . ($o['selected'] ? ' ✓' : ''), $x['offers']);
        return $name . ': ' . implode('; ', $ps);
    }, $g['suppliers'])) ?></td>
</tr>
<?php endforeach; ?>
</table>
<?php endif; ?>
<script>
window.riaiSort = window.riaiSort || function (th, type, firstAsc) {
    // первый клик: текст и цена — по возрастанию, остальное — по убыванию; повторный клик — наоборот
    var table = th.closest('table'), rows = Array.prototype.slice.call(table.rows, 1), i = th.cellIndex;
    var dir = th.dataset.dir ? (th.dataset.dir === 'asc' ? 'desc' : 'asc') : (firstAsc ? 'asc' : 'desc');
    Array.prototype.forEach.call(th.parentNode.cells, function (c) {
        delete c.dataset.dir; c.textContent = c.textContent.replace(/ [↑↓]$/, '');
    });
    th.dataset.dir = dir;
    th.textContent += dir === 'asc' ? ' ↑' : ' ↓';
    rows.sort(function (a, b) {
        var x = a.cells[i].dataset.v, y = b.cells[i].dataset.v;
        var r = type === 'num' ? parseFloat(x) - parseFloat(y) : String(x).localeCompare(String(y), 'ru');
        return dir === 'asc' ? r : -r;
    });
    rows.forEach(function (row) { row.parentNode.appendChild(row); });
};
</script>
<style>.riai table.riai-sort th{cursor:pointer;user-select:none}.riai table.riai-sort th:hover{color:#2067b0}</style>
</div>
<?php
