WITH l AS (
 SELECT i.parent, i.idx, i.name, i.custom_number_of_boxes b,
  CASE WHEN i.custom_mixed_box=1 OR i.custom_mixed_bunch=1 THEN IFNULL(i.custom_packrate_mixed_box,0) ELSE IFNULL(CAST(i.custom_packrate AS DECIMAL(10,0)),0) END pr,
  CASE WHEN IFNULL(i.custom_line,'')<>'' THEN CONCAT('s|',i.custom_line,'|',IFNULL(i.custom_length,''))
       WHEN IFNULL(i.custom_bunch_group,'')<>'' THEN CONCAT('b|',i.custom_bunch_group)
       WHEN IFNULL(i.custom_mix_group,'')<>'' THEN CONCAT('m|',i.custom_mix_group)
       ELSE CONCAT('r|',i.name) END k,
  i.stock_qty, i.qty, i.custom_ordered_quantity
 FROM `tabSales Order Item` i JOIN `tabSales Order` so ON so.name=i.parent
 WHERE so.business_unit='Roses' AND IFNULL(i.item_code,'')<>''
), r AS (SELECT l.*, ROW_NUMBER() OVER (PARTITION BY parent,k ORDER BY idx) rn FROM l),
agg AS (SELECT parent, SUM(pr*b) stems, SUM(CASE WHEN rn=1 THEN b ELSE 0 END) boxes, SUM(b) naive_boxes, SUM(stock_qty) sq, SUM(qty) q, COUNT(*) n FROM r GROUP BY parent)
SELECT so.docstatus,
 COUNT(*) orders,
 SUM(so.custom_total_boxes<>agg.boxes) box_mismatch,
 SUM(so.custom_total_stems<>agg.stems) stem_mismatch,
 SUM(so.custom_total_boxes<>agg.boxes OR so.custom_total_stems<>agg.stems) any_mismatch,
 SUM(agg.naive_boxes<>agg.boxes) naive_overcount_orders,
 SUM(agg.sq<>agg.stems) stockqty_vs_canon
FROM `tabSales Order` so JOIN agg ON agg.parent=so.name GROUP BY so.docstatus;
