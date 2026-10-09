# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
"""Shared definitions for every packhouse v2 number.

    units     stems / bunches / stems-per-box for a Sales Order line
    boxes     the one box-dedup key (how many physical boxes an order holds)
    rose      Spray / Standard classification through the Item Group tree
    pipeline  per order line: ordered -> allocated -> issued -> planned ->
              packed -> staged -> loaded -> dispatched, all in stems
    stock     per live bucket: shelved = discard-held + allocated + free
"""
