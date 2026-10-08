# Copyright (c) 2026, Upande and contributors
# For license information, please see license.txt
#
# Cold Room v2 (www/cold-room-v2.html). Rebuild of /cold-room on the shared
# packhouse v2 design system. Same APIs as the original page
# (upande_packhouse.api.coldroom.fetchColdroomData / getColdroomBuckets and
# upande_sensors.api.sensor_charts.get_sensor_chart_data).

from upande_packhouse.packhouse_v2 import page_context

no_cache = 1


def get_context(context):
	return page_context(context, "cold-room", "Cold Room")
