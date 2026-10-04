# Moved to upande_packhouse.api.remote_transfer.bucket_journey (the Remote Transfers Bucket Journey tab).
# This path stays an alias of that module — the same functions — so existing callers
# (pages, the mobile apps, hooks) keep working.
import sys

from upande_packhouse.api.remote_transfer import bucket_journey as _module

sys.modules[__name__] = _module
