def execute():
	"""Date the transfer steps already done on open orders (the last 3 weeks)."""
	from upande_packhouse.api.remote_transfer import transfer_steps

	transfer_steps.reconcile(since_days=transfer_steps.CYCLE_DAYS, limit=200000)
