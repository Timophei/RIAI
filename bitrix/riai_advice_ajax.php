<?php
/**
 * Содержимое вкладки «Подсказки снабжения»: /local/ajax/riai_advice.php?purchase=34187
 *   purchase — ID элемента закупки (из адреса карточки /crm/type/132/details/34187/), не номер в названии
 *   &refresh=1 — пересчитать без кэша;  &debug=1 — диагностика (какие подзаявки и позиции нашлись)
 * Вкладку в карточку добавляет обработчик из init_riai.php — он и вызывает этот файл.
 */
define('NO_KEEP_STATISTIC', true);
require $_SERVER['DOCUMENT_ROOT'] . '/bitrix/modules/main/include/prolog_before.php';

use Bitrix\Crm\Service\Container;
use Bitrix\Main\Loader;

const RIAI_PURCHASE_TYPE = 132;   // Закупки
const RIAI_REQUEST_TYPE  = 183;   // Запрос поставщику
const RIAI_COLUMNS       = 6;     // сколько поставщиков показывать столбцами в матрице
// Создавать ли вместе с запросом «Позиции предложения» (список 20) с пустой ценой по всем позициям закупки.
// Включите, если сейчас их заводят вручную; не включайте, если их уже создаёт бизнес-процесс.
const RIAI_CREATE_OFFER_ROWS = false;

header('Content-Type: text/html; charset=UTF-8');
global $USER;
$purchaseId = (int)($_GET['purchase'] ?? 0);
if (!$USER->IsAuthorized() || !Loader::includeModule('crm') || !Loader::includeModule('iblock') || $purchaseId <= 0) {
    http_response_code(403);
    die('Нет доступа');
}
// Ошибки — текстом во вкладку, а не пустой экран
set_exception_handler(function (\Throwable $e) {
    echo '<div style="color:#a0662b">Ошибка вкладки подсказок: ' . htmlspecialcharsbx($e->getMessage())
        . ' <span style="color:#80868e">(' . htmlspecialcharsbx(basename($e->getFile())) . ':' . $e->getLine() . ')</span></div>';
});
register_shutdown_function(function () {
    $e = error_get_last();
    if ($e && in_array($e['type'], [E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR], true)) {
        echo '<div style="color:#a0662b">Ошибка вкладки подсказок: ' . htmlspecialcharsbx($e['message'])
            . ' (' . htmlspecialcharsbx(basename($e['file'])) . ':' . $e['line'] . ')</div>';
    }
});

// Права на чтение закупки (в разных версиях Битрикса метод называется по-разному)
$perms = Container::getInstance()->getUserPermissions();
if (method_exists($perms, 'item')) {
    $canRead = $perms->item()->canRead(RIAI_PURCHASE_TYPE, $purchaseId);
} elseif (method_exists($perms, 'checkReadPermissions')) {
    $canRead = $perms->checkReadPermissions(RIAI_PURCHASE_TYPE, $purchaseId);
} else {
    $canRead = true;
}
if (!$canRead) {
    http_response_code(403);
    die('Нет доступа к закупке');
}
if (!class_exists('RiaiAdvice')) {
    die('<div style="color:#a0662b">Не подключён RiaiAdvice.php — проверьте init_riai.php и папку lib/</div>');
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

// ---------- уже созданные запросы поставщикам этой закупки: компания => запрос ----------
function riaiExistingRequests(int $purchaseId): array
{
    $out = [];
    $f = Container::getInstance()->getFactory(RIAI_REQUEST_TYPE);
    foreach ($f->getItems(['select' => ['ID', 'TITLE', 'COMPANY_ID'],
                           'filter' => ['=PARENT_ID_' . RIAI_PURCHASE_TYPE => $purchaseId]]) as $it) {
        if ($it->getCompanyId()) {
            $out[(int)$it->getCompanyId()] = ['id' => $it->getId(), 'title' => $it->getTitle()];
        }
    }
    return $out;
}

// ---------- действие: создать запрос поставщику ----------
if (($_POST['action'] ?? '') === 'create_request') {
    header('Content-Type: application/json; charset=UTF-8');
    $companyId = (int)($_POST['company'] ?? 0);
    if (!check_bitrix_sessid() || $companyId <= 0) {
        die(json_encode(['ok' => false, 'error' => 'Сессия устарела, обновите страницу']));
    }
    $exists = riaiExistingRequests($purchaseId);
    if (isset($exists[$companyId])) {
        die(json_encode(['ok' => true, 'id' => $exists[$companyId]['id'], 'existed' => true,
                         'url' => '/crm/type/' . RIAI_REQUEST_TYPE . '/details/' . $exists[$companyId]['id'] . '/']));
    }
    $company = \Bitrix\Crm\CompanyTable::getById($companyId)->fetch();
    $factory = Container::getInstance()->getFactory(RIAI_REQUEST_TYPE);
    $item = $factory->createItem([
        'TITLE'          => 'Запрос: ' . ($company['TITLE'] ?? ('компания ' . $companyId)),
        'COMPANY_ID'     => $companyId,
        'PARENT_ID_' . RIAI_PURCHASE_TYPE => $purchaseId,
        'ASSIGNED_BY_ID' => (int)$USER->GetID(),
    ]);
    $res = $factory->getAddOperation($item)->launch();   // проверяет права и запускает роботов/БП как обычно
    if (!$res->isSuccess()) {
        die(json_encode(['ok' => false, 'error' => implode('; ', $res->getErrorMessages())]));
    }
    $reqId = $item->getId();
    if (RIAI_CREATE_OFFER_ROWS) {
        $el = new CIBlockElement();
        foreach ($positions as $p) {
            $el->Add(['IBLOCK_ID' => 20, 'NAME' => $p['name'], 'PROPERTY_VALUES' => [
                73 => 'T' . dechex(RIAI_REQUEST_TYPE) . '_' . $reqId,   // Запрос
                74 => $p['id'],                                          // Позиция
            ]]);
        }
    }
    die(json_encode(['ok' => true, 'id' => $reqId,
                     'url' => '/crm/type/' . RIAI_REQUEST_TYPE . '/details/' . $reqId . '/']));
}

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
$existing = riaiExistingRequests($purchaseId);

// ---------- помощники отрисовки ----------
$h = fn($s) => htmlspecialcharsbx((string)$s);
$lnkCompany  = fn($id) => "/crm/company/details/{$id}/";
$lnkRequest  = fn($id) => '/crm/type/' . RIAI_REQUEST_TYPE . "/details/{$id}/";
$lnkPurchase = fn($id) => '/crm/type/' . RIAI_PURCHASE_TYPE . "/details/{$id}/";
$money = fn($p, $d = 0) => number_format((float)$p, $d, ',', ' ');
$priceHtml = function ($o) use ($h, $money) {        // «950 ₽/меш. ≈ 38 ₽/кг»
    if (!$o || !$o['price']) return '<span class="riai-muted">без цены</span>';
    $d = $o['price'] < 100 ? 2 : 0;
    $s = $money($o['price'], $d) . ' ₽' . ($o['unit'] ? '/' . $h(rtrim($o['unit'], '.')) : '');
    if ($o['unit_price'] && $o['base_unit'] && $o['base_unit'] !== rtrim((string)$o['unit'], '.')) {
        $s .= ' <span class="riai-muted">≈ ' . $money($o['unit_price'], 2) . ' ₽/' . $h($o['base_unit']) . '</span>';
    }
    return $s;
};
$reqLink = fn($o) => $o['request_id']
    ? '<a href="' . $lnkRequest($o['request_id']) . '" target="_blank">' . $h($o['request_title'] ?: 'Запрос ' . $o['request_id']) . '</a>'
    : '—';
$num = [];
foreach ($r['positions'] as $i => $p) $num[$p['id']] = $i + 1;

$cols = array_slice($r['suppliers'], 0, RIAI_COLUMNS);
// лучшая цена в строке (за базовую единицу, только сопоставимые)
$rowBest = [];
foreach ($r['positions'] as $p) {
    foreach ($cols as $s) {
        $c = $s['cells'][$p['id']] ?? null;
        if ($c && $c['comparable'] && $c['unit_price'] !== null
            && (!isset($rowBest[$p['id']]) || $c['unit_price'] < $rowBest[$p['id']])) {
            $rowBest[$p['id']] = $c['unit_price'];
        }
    }
}
$box = 'riai-box-' . $purchaseId;
?>
<style>
.riai{font-size:13px;line-height:1.45;color:#1f2d3d}
.riai table{border-collapse:collapse;background:#fff;width:100%}
.riai th,.riai td{border:1px solid #e1e5ea;padding:6px 8px;vertical-align:top;text-align:left}
.riai th{background:#f5f7f9;font-weight:600;color:#525c69}
.riai .riai-matrix td.c,.riai .riai-matrix th.c{text-align:center;min-width:150px}
.riai .riai-matrix tr.sep td{background:#fafbfc}
.riai .riai-muted{color:#80868e}
.riai .riai-best{color:#2b8a3e;font-weight:700}
.riai .riai-sel{color:#2b8a3e}
.riai .riai-small{font-size:12px}
.riai h3{font-size:15px;margin:22px 0 8px}
.riai .note{background:#f5f7f9;padding:8px 10px;border-radius:4px;margin-bottom:10px;color:#525c69}
</style>
<div class="riai" id="<?= $box ?>">
<div style="margin-bottom:8px">
  <button type="button" class="ui-btn ui-btn-light-border ui-btn-xs" onclick="(function(b){
      var box=document.getElementById('<?= $box ?>'); b.disabled=true; b.textContent='Обновляю…';
      BX.ajax({url:'/local/ajax/riai_advice.php?purchase=<?= $purchaseId ?>&refresh=1', method:'GET', dataType:'html', timeout:120,
        onsuccess:function(html){ BX.html(box.parentNode, html); }, onfailure:function(){ b.disabled=false; b.textContent='Ошибка, ещё раз'; }});
  })(this)">Обновить подсказки</button>
  <span class="riai-muted riai-small">История закупок на <?= $h($r['updated']) ?>.
  Цены — из прошлых предложений; <span class="riai-best">зелёным</span> — лучшая в строке, ✓ — предложение выбирали.</span>
</div>

<?php if (!$cols): ?>
<div class="note">По истории предложить некого — позиции закупки раньше не встречались.</div>
<?php else: ?>
<table class="riai-matrix">
<tr>
  <th>Позиция</th><th>Кол-во</th><th>Ед.</th>
  <?php foreach ($cols as $s): ?>
  <th class="c"><a href="<?= $lnkCompany($s['company_id']) ?>" target="_blank"><?= $h($s['company']) ?></a></th>
  <?php endforeach; ?>
</tr>
<?php foreach ($r['positions'] as $p): ?>
<tr>
  <td><?= $num[$p['id']] ?>. <?= $h($p['name']) ?></td>
  <td><?= $h($p['qty']) ?></td>
  <td><?= $h($p['unit']) ?></td>
  <?php foreach ($cols as $s): $c = $s['cells'][$p['id']] ?? null;
      $isBest = $c && $c['comparable'] && $c['unit_price'] !== null && isset($rowBest[$p['id']]) && $c['unit_price'] == $rowBest[$p['id']]; ?>
  <td class="c">
    <?php if (!$c): ?><span class="riai-muted">—</span>
    <?php else: ?>
      <span class="<?= $isBest ? 'riai-best' : ($c['comparable'] ? '' : 'riai-muted') ?>"
            title="<?= $c['comparable'] ? '' : 'Другая единица — напрямую не сравнивать' ?>"><?= $priceHtml($c) ?></span><?= $c['selected'] ? ' <span class="riai-sel">✓</span>' : '' ?>
      <div class="riai-muted riai-small"><?= $h($c['date']) ?></div>
    <?php endif; ?>
  </td>
  <?php endforeach; ?>
</tr>
<?php endforeach; ?>
<tr>
  <td colspan="3">Где была эта цена</td>
  <?php foreach ($cols as $s): ?>
  <td class="c riai-small"><?php
      $lines = [];
      foreach ($r['positions'] as $p) {
          $c = $s['cells'][$p['id']] ?? null;
          if (!$c) continue;
          $lines[] = '№' . $num[$p['id']] . ': ' . $reqLink($c) . '<br><span class="riai-muted">' . $h($c['date']) . ' · '
              . ($c['selected'] ? '<span class="riai-sel">выбран</span>' : 'не выбран')
              . ($c['purchase_id'] ? ' · <a href="' . $lnkPurchase($c['purchase_id']) . '" target="_blank">закупка ' . (int)$c['purchase_id'] . '</a>' : '')
              . '</span>';
      }
      echo implode('<div style="height:6px"></div>', $lines);
  ?></td>
  <?php endforeach; ?>
</tr>
<tr>
  <td colspan="3">Надёжность</td>
  <?php foreach ($cols as $s): ?>
  <td class="c riai-small">
    Брали: <b><?= (int)$s['wins'] ?></b> из <?= (int)$s['offers'] ?> предложений<br>
    Лучшая цена: <b><?= (int)$s['best_count'] ?></b> из <?= (int)$s['priced_count'] ?> конкурсов
  </td>
  <?php endforeach; ?>
</tr>
<tr>
  <td colspan="3"></td>
  <?php foreach ($cols as $s): $ex = $existing[(int)$s['company_id']] ?? null; ?>
  <td class="c">
    <?php if ($ex): ?>
      <button type="button" class="ui-btn ui-btn-xs ui-btn-light" disabled>Запрос создан</button>
      <div class="riai-small"><a href="<?= $lnkRequest($ex['id']) ?>" target="_blank"><?= $h($ex['title'] ?: 'Запрос ' . $ex['id']) ?></a></div>
    <?php else: ?>
      <button type="button" class="ui-btn ui-btn-xs ui-btn-primary" onclick="(function(b){
          b.disabled=true; b.textContent='Создаю…';
          BX.ajax({url:'/local/ajax/riai_advice.php?purchase=<?= $purchaseId ?>', method:'POST', dataType:'json', timeout:60,
            data:{action:'create_request', company:<?= (int)$s['company_id'] ?>, sessid:BX.bitrix_sessid()},
            onsuccess:function(res){
              if(res && res.ok){ b.textContent='Запрос создан'; b.className='ui-btn ui-btn-xs ui-btn-light';
                var a=document.createElement('div'); a.className='riai-small';
                a.innerHTML='<a href=&quot;'+res.url+'&quot; target=&quot;_blank&quot;>открыть запрос</a>'; b.parentNode.appendChild(a); }
              else { b.disabled=false; b.textContent='Создать запрос'; alert((res && res.error) || 'Не удалось создать запрос'); }
            },
            onfailure:function(){ b.disabled=false; b.textContent='Создать запрос'; alert('Не удалось создать запрос'); }});
      })(this)">Создать запрос</button>
    <?php endif; ?>
  </td>
  <?php endforeach; ?>
</tr>
</table>
<?php if (count($r['suppliers']) > RIAI_COLUMNS): ?>
<div class="riai-muted riai-small" style="margin-top:4px">Ещё поставщиков в истории: <?= count($r['suppliers']) - RIAI_COLUMNS ?> — см. таблицы ниже.</div>
<?php endif; ?>
<?php endif; ?>

<?php foreach ($r['positions'] as $p):
    $rows = [];
    foreach ($p['matches'] as $m) foreach ($m['offers'] as $o) $rows[] = $o + ['hist' => $m['name'], 'hist_qty' => $m['qty'], 'hist_unit' => $m['unit']];
    usort($rows, fn($a, $b) => strcmp(implode('', array_reverse(explode('.', $b['date']))), implode('', array_reverse(explode('.', $a['date'])))));
?>
<h3><?= $num[$p['id']] ?>. <?= $h($p['name']) ?> <span class="riai-muted">— <?= $h($p['qty']) ?> <?= $h($p['unit']) ?></span></h3>
<?php if (!$rows): ?>
<div class="note">Раньше не закупали.</div>
<?php else: ?>
<table>
<tr><th>Дата</th><th>Поставщик</th><th>Цена</th><th>Выбран</th><th>Запрос</th><th>Закупка</th><th>Как называлась позиция</th></tr>
<?php foreach (array_slice($rows, 0, 40) as $o): ?>
<tr>
  <td><?= $h($o['date']) ?></td>
  <td><?= $o['company_id'] ? '<a href="' . $lnkCompany($o['company_id']) . '" target="_blank">' . $h($o['company']) . '</a>' : $h($o['company'] ?: '—') ?></td>
  <td class="<?= $o['comparable'] ? '' : 'riai-muted' ?>"><?= $priceHtml($o) ?></td>
  <td><?= $o['selected'] ? '<span class="riai-sel">✓ выбран</span>' : '<span class="riai-muted">нет</span>' ?></td>
  <td><?= $reqLink($o) ?></td>
  <td><?= $o['purchase_id'] ? '<a href="' . $lnkPurchase($o['purchase_id']) . '" target="_blank">' . (int)$o['purchase_id'] . '</a>' : '—' ?></td>
  <td class="riai-muted"><?= $h($o['hist']) ?> (<?= $h($o['hist_qty']) ?> <?= $h($o['hist_unit']) ?>)</td>
</tr>
<?php endforeach; ?>
</table>
<?php endif; ?>
<?php endforeach; ?>
</div>
