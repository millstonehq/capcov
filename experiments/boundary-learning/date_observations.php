<?php
// Exhaust the printable ASCII two-character formats at a fixed instant.
// Byte observations stay inside JSON strings, including control characters.
$date = new DateTimeImmutable('2026-07-15 12:00:00', new DateTimeZone('UTC'));
for ($first = 32; $first <= 126; $first++) {
    for ($second = 32; $second <= 126; $second++) {
        $format = chr($first) . chr($second);
        echo json_encode([$format, $date->format($format)], JSON_THROW_ON_ERROR), "\n";
    }
}
