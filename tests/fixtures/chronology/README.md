# Chronology fixtures

`schema-v6.sql` is the unchanged schema from origin/main
`c376801cb2cbf59c30c1d7e305ae24ecc4cea0e5`.

`hmd-recovery.json` projects three products and all of their canonical
observation rows from the accepted recon's SQLite-safe Windows v6 snapshot
(SHA-256 `ba0509c1c66b0fd69b4a1447f307f8eb421db33d4d57a01d1a024c382a4d02a1`).
Each `current` Discovery is copied from the HMD recon result captured
2026-09-14 06:51–06:54 UTC (`result.json` SHA-256
`4167ec05c726f36e30e249cee043e318630438d86eb551184659cf9cfb294a00`).
These are real complete states matching older canonical hashes. The fixture
does not invent resighting history. Tests add only clearly synthetic event
and notification rows to exercise preservation.
