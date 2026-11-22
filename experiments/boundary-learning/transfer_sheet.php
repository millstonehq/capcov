<?php
// Run learned support and proposed probes through a different public API.
if ($argc !== 2) {
    fwrite(STDERR, "Usage: php transfer_sheet.php /path/to/vendor/autoload.php < rules.jsonl\n");
    exit(2);
}
require $argv[1];

use PhpOffice\PhpSpreadsheet\Style\NumberFormat;

if (Composer\InstalledVersions::getVersion('phpoffice/phpspreadsheet') !== '1.30.7.0') {
    throw new RuntimeException('Expected PhpSpreadsheet 1.30.7');
}
while (($line = fgets(STDIN)) !== false) {
    $rule = json_decode($line, true, 512, JSON_THROW_ON_ERROR);
    if ($rule['rule'] !== 'prefix + character -> character') {
        throw new RuntimeException('Unknown learned rule');
    }
    foreach (array_unique(array_merge($rule['support'], $rule['probes'])) as $operand) {
        $mask = 'yyyy' . $operand;
        $actual = NumberFormat::toFormattedString(45000.5, $mask);
        echo json_encode([$mask, $actual], JSON_THROW_ON_ERROR), "\n";
    }
}
