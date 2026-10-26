# Label Print Manager (backend)

Independent of `server_scripts/gen_label_id.py` and the Label Print doctype.
Page: `www/label-print-manager-v2.html`. API: `api/v2/label_manager.py`.

| File | Role |
|---|---|
| `types.py` | The six label types, their scanner payloads, default designs |
| `render.py` | Geometry solver + hand-written vector PDF writer (~4 ms/label) |
| `codes.py` | Code allocation and persistence (one transaction) |
| `batch.py` | Create (idempotent), chunked resumable render, reprint |

## Why duplicates cannot happen

1. `Label Batch.request_key` is unique: a retried request returns the same batch.
2. A batch is made in ONE transaction (counter bump, `Label Code` registry,
   `Bunch/Bucket/Shelf QR Code` rows, the batch). Any failure rolls back all of it.
3. The shared `QR Sequence` counter row is locked while a range is taken, and
   every candidate number is checked against existing `Bunch QR Code` names
   (live counters drift behind the real highest number); taken numbers are skipped.
4. `Label Code.name` is `type:code` - the primary key rejects a duplicate even if 1-3 fail.
5. Random bucket codes are checked against Bucket QR Code, Bunch QR Code, Box Label
   and the registry (they share the scanners' 6-character space).

## Resume

Codes are fixed at creation. Rendering writes numbered chunk files
(`private/files/label_print/<batch>/cNNNNN.pdf`) and records `chunks_done`.
`run_batch` is safe to run again at any time; it continues at the first
unfinished chunk. A batch whose heartbeat is older than 120 s can be resumed
from the History tab. Geometry is solved once at creation (stored in `layout._geo`)
so every chunk, and every resume, is identical to the preview.

## Scanner payloads (do not change - the apps parse these)

bunch `{"bunch_id":"BUNCH-n"}` | bucket `{"CODE":"bucket"}` | shelf / sea freight shelf
`{"shelf":"KPK-D21M"}` / `{"shelf":"SF-A1T"}` | trolley `{"KPK-TROLLEY-1":"trolley"}` |
staging `{"location":"NAME"}`. All must be JSON: the apps' ScanField treats valid JSON
ending in `}` as "scan complete".

## Templates

Stored in the `Label Template` doctype (design as JSON); the six `Standard ...` templates are created on first use.
