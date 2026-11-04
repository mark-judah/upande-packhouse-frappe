# Order import sources

Every customer order file is imported through the same four steps on the Sales Order v2 page
(Import): **1 File, 2 Source, 3 Mapping, 4 Import.**

1. **File** - choose the CSV or Excel file.
2. **Source** - one picker lists every source. The wizard suggests the one the file looks like. A source
   decides its customer (or, for DFG Cloud, the Buyer column does).
3. **Mapping** - every column of the file is listed and the user maps each one (or leaves it as "Don't import");
   nothing is assumed to matter. File rules sit below it. A wide-matrix source (Flamingo Cloud) also shows its layout
   (which row holds the PO numbers, where the items start ...) next to the file's first rows, so the positions can be read off
   and edited here; its mapping works on the columns that layout produces.
4. **Import** - one card per order found. An order with nothing unresolved is **ready** and has an
   **Import this order** button; "Import all ready orders" does the same for all of them. A line whose
   Specification is not recognised shows a search to pick which Specification it really is (remembered for the
   next file). An order that already exists (same PO and customer) is shown as "already exists".

### Before an order is saved: one dialog for everything missing

When the Import page opens, every order is checked (`checkImportOrder`, `order_import_checks.py`) for everything that
would stop it saving, so nothing fails one error at a time. Ready orders are listed first. An order with something
missing shows "needs info" and its button reads **Fill in & import**. One dialog asks for all of it, even when it lives in
another record:

| What is missing | What the dialog asks for | Where it is saved |
|---|---|---|
| Order name, delivery point, shipping agent, consignee, S number, delivery date, Set Warehouse (always in the dialog, blank for the person to choose; it is written onto every line) ... | the value (searchable for records) | on the order |
| A consignee / delivery point / shipping agent / stem length / box type that does not exist | create it, or pick an existing one | the record, then the order |
| No Item Price for a variety and stem length | the price per stem; the dialog shows the active price list (the customer's Default Price List) | an Item Price in that price list |
| Customer has no Default Price List | a price list | the Customer |
| Item without a Sales UOM / a disabled item | a UOM / switch it back on | the Item |
| Line without stem length, box type or number of boxes | the values | the order line |
| Specification not recognised | which Specification it is (remembered) | the source's mapping |

**Skip** (in the dialog, or in the "could not be created" window) only means *not now*: the run moves on to the next order so
that orders which are fine are not held up by one that needs fixing. The skipped order is not set aside or marked; it is still
waiting, still has to be imported, and the next **Import all** (or its own button) asks about it again. When a run ends it says
how many orders were passed over.

**Next** applies it and checks again. If more is needed the dialog stays; when the order is clean it is created. "Import all
orders" does this order by order, pausing at the first dialog, and carries on after Next. A save the server still refuses
shows its message with **Try again**. Problems that cannot be fixed in a form (packrate, bunch size, colour limits,
inconsistent box counts) are listed read-only and go to Review & fix.

### Every import is saved, and can be resumed

When the preview is built the import is saved as an **Order Import** record (Desk: Order Import): the uploaded file
(attached privately and also stored parsed), the orders found in it with each one's state, the source and customer, and
the progress (orders created, already existing, percent done). The wizard keeps it up to date as orders are created, fixed
or paused. The upload page lists unfinished imports under **Resume an import**; **Resume** reopens the queue exactly as it
was, with no upload, and **Remove** takes it off the list. An import becomes Completed once every order is created or
already existed.

Every Sales Order the import creates has **Order Import** (`custom_order_import`) filled in, and the Order Import lists
its orders with a link to each Sales Order.

### History and updating an import

The **History** tab (in front of the steps) lists every past import with a progress bar (percent, orders done, orders
left). Opening one shows its details: who and when, the status counts, and every order with its Sales Order and any error. 
**Update** re-reads a newer file for a past import, because an order on a cloud platform can change. The steps are
File, Compare, Import:

1. **File** - upload the new file. It is read with the same source and mapping as the original.
2. **Compare** - two separate sections. **Orders already imported** are the Sales Orders an import created (this one, or
   any import - their Order Import link is filled in); each is shown only as *changed* (with the header fields and
   lines that differ, old to new: boxes, packrate, unit, delivery date, lines added or removed), *same*, or *submitted /
   cancelled* (cannot be updated here), and the changed ones can be ticked. **Not imported** lists what is in the new file
   but was never imported (including an order that exists for that PO but no import created); an update does not touch
   those - bring them in with a normal import. Orders from the earlier import that the new file no longer contains are
   listed and left alone.
3. **Import** - the ticked changed orders go through the same pre-check and "fill in" dialog as a normal import. An update
   replaces the order's lines and changes only the header fields the file itself carries (never the PO number,
   customer, order date or price list). The order gets a note saying which import updated it.

**Only one unfinished import per source is kept** (and one unfinished update per import), so Resume never lists duplicates.
Building a preview while one exists asks whether to resume it or replace it with the new file (the replaced one is
discarded; its orders stay). Closing that window resumes the most recent one, so nothing is lost.

An update is stored as its own Order Import (kind Update, linked to the one it updates).

## The three sources

| Source | File | Customer | Orders split by | Quantity |
|---|---|---|---|---|
| XPOL Cloud | one flat Excel sheet | Xpol | Harvest date | Box quantity |
| Flamingo Cloud | wide matrix workbook (a column group per PO) | FLAMINGO  UK | PO number | Bunches, converted to boxes through the Specification |
| DFG Cloud | CSV | several, by the Buyer column | PO nr | Present boxes |

A source is an **Order Import Platform** record plus one **Customer Order Mapping** per customer. They are
created by `patches/seed_standard_order_import_platforms.py` (only when missing) and can be edited in Desk or in the
wizard's mapping step.

## File rules (on the Order Import Platform)

- **Skip rows where column is one of values** - XPOL Cloud leaves out `Status = CANCELLED`.
- **File date format** - day first by default. `09/10/2026` is 9 October, whatever the site's own date setting.
- **PO number when the file has none** - e.g. `XPOL {delivery_date}`. It names each order and lets a re-imported
  file be recognised as already imported.

## Finding the Specification

A line's own wording is matched to a Specification first (and to anything remembered from an earlier import).
Failing that, the **Line / Article Code** column (`_line_code`) is used: a Specification whose name contains the
code (ignoring punctuation, case and O/0) is taken when it is the only one, or when the line's colour words pick
exactly one of several. Anything unclear goes to the import queue, which asks once and remembers the answer.

Each order card on the Import page has a **Details** button that opens what the import holds for that order: its header values
(marking which came from the file and which the importer set), and every line with its file row, Specification, item,
length, boxes, packrate, unit and line code. A line held for a box count shows the file's value (e.g. **0 in file**), and its
prompt says what the import file contained and that the number of boxes must be filled in.

### Rolling an import back

An import that has not been submitted can be undone. Open it in **History** and press **Roll back import** (or **Roll back** on a
single order, or **Roll back** on a created order's card on the Import page): the draft Sales Orders it created are deleted
and marked Rolled Back on the import. Submitted or cancelled orders are never touched; they are listed and stay. When every
order is gone the import becomes **Rolled Back**. An update cannot be rolled back (it changed orders another import created).

### Packrates

A straight-box line links to a Packrate record named by its stems per box. A missing one is now created when the line is built, and lines saved without one are healed when the order is checked or saved.
