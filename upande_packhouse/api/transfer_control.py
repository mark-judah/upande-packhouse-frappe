# Moved to upande_packhouse.api.remote_transfer — transfer_scheduling (Transfer
# Scheduling tab) and truck_routes (Truck Routes tab). This path stays an alias of
# transfer_scheduling, which also passes through every Truck Routes name, so existing
# callers (pages, the mobile apps, hooks, other apps) keep working unchanged.
import sys

from upande_packhouse.api.remote_transfer import transfer_scheduling as _module

sys.modules[__name__] = _module
