app_name = "upande_packhouse"
app_title = "Upande Packhouse"
app_publisher = "Upande"
app_description = "Upande Packhouse Customizations"
app_email = "dev@upande.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "upande_packhouse",
# 		"logo": "/assets/upande_packhouse/logo.png",
# 		"title": "Upande Packhouse",
# 		"route": "/upande_packhouse",
# 		"has_permission": "upande_packhouse.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/upande_packhouse/css/upande_packhouse.css"
# app_include_js = "/assets/upande_packhouse/js/upande_packhouse.js"

# include js, css files in header of web template
# web_include_css = "/assets/upande_packhouse/css/upande_packhouse.css"
# web_include_js = "/assets/upande_packhouse/js/upande_packhouse.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "upande_packhouse/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "upande_packhouse/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "upande_packhouse.utils.jinja_methods",
# 	"filters": "upande_packhouse.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "upande_packhouse.install.before_install"
# after_install = "upande_packhouse.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "upande_packhouse.uninstall.before_uninstall"
# after_uninstall = "upande_packhouse.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "upande_packhouse.utils.before_app_install"
# after_app_install = "upande_packhouse.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "upande_packhouse.utils.before_app_uninstall"
# after_app_uninstall = "upande_packhouse.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "upande_packhouse.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }
doc_events = {
	"Stock Entry": {"validate": [
		# No legacy custom_farm / custom_business_unit mirror here — Stock
		# Entry uses only the real accounting-dimension fields (farm,
		# business_unit), no legacy fields left to keep in sync.
		"upande_packhouse.stock_entry_cost_center.apply_greenhouse_cost_center",
	]},
	"Sales Order": {
		"before_validate": "upande_packhouse.sales_order_engine.sales_order_before_validate",
		"validate": [
			# ecommerce_integration (Floriday/Biflorica) still owns custom_farm /
			# custom_business_unit here — bridge into the real farm /
			# business_unit dimension fields this app actually reads. See
			# sync_sales_order_accounting_dimensions's own docstring.
			"upande_packhouse.roses_invoice.sync_sales_order_accounting_dimensions",
			"upande_packhouse.sales_order_engine.sales_order_price",
			"upande_packhouse.sales_order_engine.sales_order_validate",
		],
	},
	"Specifications": {"before_validate": "upande_packhouse.spec.ensure_spec_uoms_and_packrates"},
	"Delivery Note": {
		"on_submit": "upande_packhouse.roses_invoice.delivery_note_on_submit",
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"daily": [
		"upande_packhouse.spec.expire_temporary_specs",
	],
}

# scheduler_events = {
# 	"all": [
# 		"upande_packhouse.tasks.all"
# 	],
# 	"daily": [
# 		"upande_packhouse.tasks.daily"
# 	],
# 	"hourly": [
# 		"upande_packhouse.tasks.hourly"
# 	],
# 	"weekly": [
# 		"upande_packhouse.tasks.weekly"
# 	],
# 	"monthly": [
# 		"upande_packhouse.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "upande_packhouse.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "upande_packhouse.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "upande_packhouse.task.get_dashboard_data"
# }
override_doctype_dashboards = {
	"Stock Entry": "upande_packhouse.stock_entry_connections.get_dashboard_data"
}

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["upande_packhouse.utils.before_request"]
# after_request = ["upande_packhouse.utils.after_request"]

# Job Events
# ----------
# before_job = ["upande_packhouse.utils.before_job"]
# after_job = ["upande_packhouse.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"upande_packhouse.auth.validate"
# ]

# Fixtures
# --------
fixtures = [
    {"dt": "Workspace", "filters": [["name", "=", "Packhouse"]]},
    {"dt": "Custom HTML Block", "filters": [["name", "=", "Packhouse Navigation"]]},
]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

