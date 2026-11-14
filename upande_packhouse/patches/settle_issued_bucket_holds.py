def execute():
	"""Bucket Allocations rows still outstanding although their order's pick list issued that bucket, on a bucket
	label shelved again since: marked issued and their statuses' quantities recomputed (now also every 5 minutes,
	see api/v2/core/issued_holds.py)."""
	from upande_packhouse.api.v2.core.issued_holds import settle

	n = settle()
	if n:
		print(f"Settled {n} issued bucket holds")
