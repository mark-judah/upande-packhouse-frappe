WITH l AS (
 SELECT i.parent, i.idx, i.name, i.custom_number_of_boxes b,
  CASE WHEN IFNULL(i.custom_line,'')<>'' THEN CONCAT('s|',i.custom_line,'|',IFNULL(i.custom_length,''))
       WHEN IFNULL(i.custom_bunch_group,'')<>'' THEN CONCAT('b|',i.custom_bunch_group)
       WHEN IFNULL(i.custom_mix_group,'')<>'' THEN CONCAT('m|',i.custom_mix_group)
       ELSE CONCAT('r|',i.name) END k,
  CASE WHEN i.custom_mixed_box=0 AND i.custom_mixed_bunch=0 THEN CONCAT('r|',i.name)
       WHEN IFNULL(i.custom_line,'')<>'' THEN CONCAT('s|',i.custom_line,'|',IFNULL(i.custom_length,''))
       WHEN IFNULL(i.custom_bunch_group,'')<>'' THEN CONCAT('b|',i.custom_bunch_group)
       WHEN IFNULL(i.custom_mix_group,'')<>'' THEN CONCAT('m|',i.custom_mix_group)
       ELSE CONCAT('r|',i.name) END k2
 FROM `tabSales Order Item` i JOIN `tabSales Order` so ON so.name=i.parent
 WHERE so.business_unit='Roses' AND so.docstatus<2 AND IFNULL(i.item_code,'')<>''
), r AS (SELECT l.*, ROW_NUMBER() OVER (PARTITION BY parent,k ORDER BY idx) rn, ROW_NUMBER() OVER (PARTITION BY parent,k2 ORDER BY idx) rn2 FROM l),
agg AS (SELECT parent, SUM(IF(rn=1,b,0)) canon, SUM(IF(rn2=1,b,0)) alt FROM r GROUP BY parent)
SELECT parent, canon, alt FROM agg WHERE canon<>alt;
