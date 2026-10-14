# REA API-view fixture

`rea-api-view.json` was generated with the published `rea-agents@6.5.0`
`projectWebNetworkApiSurfaceView` and `createEvidence` functions, and accepted
by that version's `parseEvidence`. It projects a synthetic one-entry HAR:
`GET https://example.invalid/api/items/42`, status 200, body `[]`.

The upstream projector infers `{numeric}` and supplies the pagination coverage.
The capture decoder is a fixture, not a claim that REA's external decoder ran.
This verifies the importer against producer-generated JSON and an authenticated
REA semantic ID. It does not qualify the external decoder or JavaScript worker.
Both CLI paths require a macOS ownership helper compiled with `swiftc`.

Upstream contract reference: `morluto/rea` revision
`a732c859ddd005a0179854897a645b531bf163d3`, files
`src/domain/evidence.ts`, `src/domain/javascript/javascriptApplicationGraphSchemas.ts`
and `src/domain/analysisView/webNetworkApiSurfaceView.ts`.
